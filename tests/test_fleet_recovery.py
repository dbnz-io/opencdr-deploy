from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError, WaiterError

from opencdr_deploy import fleet_recovery as recovery
from opencdr_deploy.fleet_api import FleetManagerError

ACCOUNT = "111111111111"
REGION = "us-east-1"
STACK = "opencdr-fleet-manager-prod"
ORIGIN = f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/{STACK}/old"
NEW = f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/{STACK}/new"
BUCKET = "original-fleet-evidence"


def error(code, message="failure"):
    return ClientError({"Error": {"Code": code, "Message": message}}, "Test")


def tags(logical):
    return {
        "aws:cloudformation:stack-id": ORIGIN,
        "aws:cloudformation:stack-name": STACK,
        "aws:cloudformation:logical-id": logical,
        "Project": "opencdr-fleet-manager",
    }


@pytest.fixture
def environment(monkeypatch):
    ddb, s3, cf, sts, kms = [Mock() for _ in range(5)]
    sts.get_caller_identity.return_value = {"Account": ACCOUNT}
    kms.describe_key.return_value = {
        "KeyMetadata": {
            "KeyManager": "AWS",
            "Arn": "arn:aws:kms:us-east-1:111111111111:key/default",
        }
    }
    session = SimpleNamespace(region_name=REGION)
    session.client = lambda name, **kw: {
        "dynamodb": ddb,
        "s3": s3,
        "cloudformation": cf,
        "sts": sts,
        "kms": kms,
    }[name]
    monkeypatch.setattr(
        recovery,
        "aws_client",
        lambda s, name: {"dynamodb": ddb, "s3": s3, "cloudformation": cf, "sts": sts, "kms": kms}[
            name
        ],
    )
    tables = {}
    table_tags = {}
    for logical, suffix in recovery.TABLES.items():
        name = STACK + "-" + suffix
        table = {
            "TableName": name,
            "TableArn": f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{name}",
            "TableId": logical + "-uuid",
            "TableStatus": "ACTIVE",
            "SSEDescription": {
                "Status": "ENABLED",
                "SSEType": "KMS",
                "KMSMasterKeyArn": "arn:aws:kms:us-east-1:111111111111:key/default",
            },
            "BillingModeSummary": {"BillingMode": "PAY_PER_REQUEST"},
            "AttributeDefinitions": [
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            "KeySchema": [
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
        }
        tables[name] = table
        table_tags[table["TableArn"]] = tags(logical)
    ddb.describe_table.side_effect = lambda TableName: {"Table": copy.deepcopy(tables[TableName])}
    ddb.list_tags_of_resource.side_effect = lambda ResourceArn: {
        "Tags": [{"Key": k, "Value": v} for k, v in table_tags[ResourceArn].items()]
    }
    ddb.describe_time_to_live.return_value = {
        "TimeToLiveDescription": {"TimeToLiveStatus": "ENABLED", "AttributeName": "expires_at"}
    }
    ddb.describe_continuous_backups.return_value = {
        "ContinuousBackupsDescription": {
            "PointInTimeRecoveryDescription": {"PointInTimeRecoveryStatus": "ENABLED"}
        }
    }
    s3.get_bucket_location.return_value = {"LocationConstraint": None}
    s3.get_bucket_encryption.return_value = {
        "ServerSideEncryptionConfiguration": {
            "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
        }
    }
    s3.get_public_access_block.return_value = {
        "PublicAccessBlockConfiguration": dict.fromkeys(
            ("BlockPublicAcls", "BlockPublicPolicy", "IgnorePublicAcls", "RestrictPublicBuckets"),
            True,
        )
    }
    s3.get_bucket_ownership_controls.return_value = {
        "OwnershipControls": {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]}
    }
    s3.get_bucket_versioning.return_value = {}
    s3.get_bucket_replication.side_effect = error("ReplicationConfigurationNotFoundError")
    s3.get_bucket_lifecycle_configuration.return_value = {
        "Rules": [
            {
                "ID": "DetectionEvidenceRetention",
                "Status": "Enabled",
                "Prefix": "detections/",
                "Expiration": {"Days": 90},
                "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
            }
        ]
    }
    s3.get_bucket_tagging.return_value = {
        "TagSet": [{"Key": k, "Value": v} for k, v in tags("DetectionBucket").items()]
    }
    s3.get_bucket_policy.side_effect = error("NoSuchBucketPolicy")
    state = {"change": None, "template": None, "stack_status": None}

    def describe_stacks(**kwargs):
        if state["stack_status"] is None:
            raise error("ValidationError", "stack does not exist")
        return {"Stacks": [{"StackId": NEW, "StackStatus": state["stack_status"]}]}

    def describe_change_set(**kwargs):
        if state["change"] is None:
            raise error("ChangeSetNotFound", "change set does not exist")
        return copy.deepcopy(state["change"])

    def create_change_set(**kwargs):
        state["template"] = json.loads(kwargs["TemplateBody"])
        state["imports"] = kwargs["ResourcesToImport"]
        state["stack_status"] = "REVIEW_IN_PROGRESS"
        state["change"] = {
            "StackId": NEW,
            "ChangeSetId": "change-id",
            "Status": "CREATE_COMPLETE",
            "ExecutionStatus": "AVAILABLE",
            "Description": kwargs["Description"],
            "Changes": [
                {
                    "ResourceChange": {
                        "Action": "Import",
                        "LogicalResourceId": item["LogicalResourceId"],
                        "ResourceType": item["ResourceType"],
                        "PhysicalResourceId": next(iter(item["ResourceIdentifier"].values())),
                    }
                }
                for item in kwargs["ResourcesToImport"]
            ],
        }
        return {"StackId": NEW, "Id": "change-id"}

    def execute_change_set(**kwargs):
        state["stack_status"] = "IMPORT_COMPLETE"
        state["change"]["ExecutionStatus"] = "EXECUTE_COMPLETE"

    cf.describe_stacks.side_effect = describe_stacks
    cf.describe_change_set.side_effect = describe_change_set
    cf.describe_stack_resource.return_value = {
        "StackResourceDetail": {"PhysicalResourceId": BUCKET}
    }
    cf.create_change_set.side_effect = create_change_set
    cf.execute_change_set.side_effect = execute_change_set
    cf.get_template.side_effect = lambda **kw: {"TemplateBody": state["template"]}
    cf.describe_stack_resources.side_effect = lambda **kw: {
        "StackResources": [
            {
                "LogicalResourceId": item["LogicalResourceId"],
                "ResourceType": item["ResourceType"],
                "PhysicalResourceId": next(iter(item["ResourceIdentifier"].values())),
            }
            for item in state["imports"]
        ]
    }
    return SimpleNamespace(
        ddb=ddb,
        kms=kms,
        s3=s3,
        cf=cf,
        sts=sts,
        session=session,
        state=state,
        tables=tables,
        table_tags=table_tags,
    )


def plan(env, **kwargs):
    return recovery.recovery_plan(env.session, "prod", account_id=ACCOUNT, region=REGION, **kwargs)


def apply(env, value):
    return recovery.apply_recovery(env.session, value, expected_digest=value["plan_digest"])


def test_read_only_plan_pairs_all_tables_and_original_bucket(environment):
    env = environment
    value = plan(env)
    assert len(value["resources_to_import"]) == 6
    assert value["plan_digest"] == recovery.digest(value)
    assert value["physical_identities"]["DetectionTable"]["table_id"] == "DetectionTable-uuid"
    bucket = value["template"]["Resources"]["DetectionBucket"]
    assert bucket["Properties"]["BucketName"] == BUCKET
    assert bucket["DeletionPolicy"] == bucket["UpdateReplacePolicy"] == "Retain"
    assert value["data_completeness_verified"] is False
    assert value["runtime_polling_enabled"] is False
    env.s3.head_bucket.assert_called_once_with(Bucket=BUCKET, ExpectedBucketOwner=ACCOUNT)
    env.ddb.scan.assert_not_called()
    env.s3.get_object.assert_not_called()
    env.cf.create_change_set.assert_not_called()


def test_success_and_lost_response_resume_same_import_without_reexecution(environment):
    env = environment
    value = plan(env)
    execute = env.cf.execute_change_set.side_effect

    def lost(**kwargs):
        execute(**kwargs)
        raise OSError("response lost after AWS commit")

    env.cf.execute_change_set.side_effect = lost
    with pytest.raises(OSError):
        apply(env, value)
    env.cf.execute_change_set.side_effect = execute
    result = apply(env, value)
    assert result["status"] == "IMPORT_COMPLETE" and result["polling_enabled"] is False
    assert apply(env, value) == result
    env.cf.create_change_set.assert_called_once()
    env.cf.execute_change_set.assert_called_once()
    for client in (env.cf, env.ddb, env.s3):
        client.delete_stack.assert_not_called()
        client.delete_table.assert_not_called()
        client.delete_bucket.assert_not_called()
        client.put_object.assert_not_called()


def test_lost_change_set_creation_response_resumes_saved_plan(environment):
    env = environment
    value = plan(env)
    create = env.cf.create_change_set.side_effect

    def lost(**kwargs):
        create(**kwargs)
        raise OSError("created but response lost")

    env.cf.create_change_set.side_effect = lost
    with pytest.raises(OSError):
        apply(env, value)
    env.cf.create_change_set.side_effect = create
    assert apply(env, value)["status"] == "IMPORT_COMPLETE"
    env.cf.create_change_set.assert_called_once()


@pytest.mark.parametrize(
    "change", ["table_id", "retention", "lineage", "account", "region", "digest"]
)
def test_drift_or_wrong_identity_never_creates_an_import(environment, change):
    env = environment
    value = plan(env)
    if change == "table_id":
        env.tables[STACK + "-detections"]["TableId"] = "replacement"
    elif change == "retention":
        env.s3.get_bucket_lifecycle_configuration.return_value["Rules"][0]["Expiration"]["Days"] = 7
    elif change == "lineage":
        env.s3.get_bucket_tagging.return_value["TagSet"][0]["Value"] = ORIGIN + "-other"
    elif change == "account":
        env.sts.get_caller_identity.return_value = {"Account": "999999999999"}
    elif change == "region":
        env.session.region_name = "eu-west-1"
    elif change == "digest":
        value["target"]["stage"] = "other"
    with pytest.raises(FleetManagerError):
        apply(env, value)
    env.cf.create_change_set.assert_not_called()
    env.cf.execute_change_set.assert_not_called()


@pytest.mark.parametrize("broken", ["non_import", "missing", "extra", "physical", "template"])
def test_only_exact_reviewed_import_actions_can_execute(environment, broken):
    env = environment
    value = plan(env)
    create = env.cf.create_change_set.side_effect

    def changed(**kwargs):
        result = create(**kwargs)
        if broken == "non_import":
            env.state["change"]["Changes"][0]["ResourceChange"]["Action"] = "Modify"
        elif broken == "missing":
            env.state["change"]["Changes"].pop()
        elif broken == "extra":
            env.state["change"]["Changes"].append(env.state["change"]["Changes"][0])
        elif broken == "physical":
            env.state["change"]["Changes"][0]["ResourceChange"]["PhysicalResourceId"] = "unreviewed"
        else:
            env.state["template"]["Description"] = "changed"
        return result

    env.cf.create_change_set.side_effect = changed
    with pytest.raises(FleetManagerError):
        apply(env, value)
    env.cf.execute_change_set.assert_not_called()


@pytest.mark.parametrize(
    "method,code", [("head_bucket", "AccessDenied"), ("get_bucket_tagging", "NoSuchTagSet")]
)
def test_unverifiable_bucket_is_not_treated_as_absent(environment, method, code):
    env = environment
    getattr(env.s3, method).side_effect = error(code)
    with pytest.raises(ClientError):
        plan(env)
    env.cf.create_change_set.assert_not_called()


def test_no_bucket_name_fallback_to_an_unrelated_store(environment):
    env = environment
    env.cf.describe_stack_resource.side_effect = error("ValidationError", "deleted stack not found")
    with pytest.raises(FleetManagerError, match="--evidence-bucket"):
        plan(env)
    assert (
        plan(env, evidence_bucket=BUCKET)["physical_identities"]["DetectionBucket"]["bucket_name"]
        == BUCKET
    )
    env.s3.get_bucket_tagging.return_value["TagSet"][0]["Value"] = ORIGIN + "-foreign"
    with pytest.raises(FleetManagerError, match="different original stacks"):
        plan(env, evidence_bucket="unrelated-evidence-bucket")


def test_pending_import_is_resumable_and_not_reported_complete(environment):
    env = environment
    value = plan(env)

    def executing(**kwargs):
        env.state["stack_status"] = "IMPORT_IN_PROGRESS"
        env.state["change"]["ExecutionStatus"] = "EXECUTE_IN_PROGRESS"

    env.cf.execute_change_set.side_effect = executing
    waiter = Mock()
    waiter.wait.side_effect = WaiterError(
        name="stack_import_complete", reason="Max attempts exceeded", last_response={}
    )
    env.cf.get_waiter.return_value = waiter
    result = apply(env, value)
    assert result["status"] == "IMPORT_IN_PROGRESS"
    assert apply(env, value) == result
    env.cf.execute_change_set.assert_called_once()


def runtime_template(value):
    template = copy.deepcopy(value["template"])
    for name in ["ScheduleEventsRuleSchedule1", "DetectionScheduleEventsRuleSchedule1"]:
        template["Resources"][name] = {"Properties": {"State": "DISABLED"}}
    template["Conditions"] = {
        "HasPinnedDetectionBucket": {"Fn::Not": [{"Fn::Equals": [BUCKET, ""]}]}
    }
    template["Resources"]["DetectionBucket"]["Properties"]["BucketName"] = {
        "Fn::If": ["HasPinnedDetectionBucket", BUCKET, {"Ref": "AWS::NoValue"}]
    }
    return template


def test_runtime_update_preserves_bucket_binding_properties_and_paused_schedules(environment):
    value = plan(environment)
    config = {
        "security_account_id": ACCOUNT,
        "region": REGION,
        "recovery_storage": value["template"]["Resources"],
    }
    template = runtime_template(value)
    recovery.verify_recovery_template(template, config)
    for logical, change in [
        ("DetectionBucket", lambda r: r["Properties"].update(BucketName="different")),
        (
            "DetectionBucket",
            lambda r: r["Properties"]["LifecycleConfiguration"]["Rules"][0].update(
                ExpirationInDays=7
            ),
        ),
        ("DetectionTable", lambda r: r.update(DeletionPolicy="Delete")),
        ("DetectionScheduleEventsRuleSchedule1", lambda r: r["Properties"].update(State="ENABLED")),
    ]:
        candidate = copy.deepcopy(template)
        change(candidate["Resources"][logical])
        with pytest.raises(FleetManagerError):
            recovery.verify_recovery_template(candidate, config)


@pytest.mark.parametrize(
    "custom", ["kms", "encryption", "stream", "replica", "class", "capacity", "pitr", "policy"]
)
def test_custom_storage_cannot_be_silently_reconfigured(environment, custom):
    env = environment
    table = env.tables[STACK + "-detections"]
    if custom == "kms":
        env.kms.describe_key.return_value["KeyMetadata"]["KeyManager"] = "CUSTOMER"
    elif custom == "encryption":
        table["SSEDescription"] = {"Status": "ENABLING", "SSEType": "AES256"}
    elif custom == "stream":
        table["StreamSpecification"] = {"StreamEnabled": True, "StreamViewType": "NEW_IMAGE"}
    elif custom == "replica":
        table["Replicas"] = [{"RegionName": "eu-west-1"}]
    elif custom == "class":
        table["TableClassSummary"] = {"TableClass": "STANDARD_INFREQUENT_ACCESS"}
    elif custom == "capacity":
        table["OnDemandThroughput"] = {"MaxReadRequestUnits": 100}
    elif custom == "pitr":
        env.ddb.describe_continuous_backups.return_value["ContinuousBackupsDescription"][
            "PointInTimeRecoveryDescription"
        ]["RecoveryPeriodInDays"] = 7
    else:
        env.s3.get_bucket_policy.side_effect = None
        env.s3.get_bucket_policy.return_value = {
            "Policy": json.dumps({"Statement": [{"Effect": "Allow"}]})
        }
    with pytest.raises(FleetManagerError):
        plan(env)
    env.cf.create_change_set.assert_not_called()


def test_storage_drift_between_creation_and_execution_is_rejected(environment):
    env = environment
    value = plan(env)
    create = env.cf.create_change_set.side_effect

    def drift(**kwargs):
        result = create(**kwargs)
        env.tables[STACK + "-detections"]["TableId"] = "replacement"
        return result

    env.cf.create_change_set.side_effect = drift
    with pytest.raises(FleetManagerError, match="before import execution"):
        apply(env, value)
    env.cf.execute_change_set.assert_not_called()


def test_post_import_identity_drift_is_not_success(environment):
    env = environment
    value = plan(env)
    execute = env.cf.execute_change_set.side_effect

    def drift(**kwargs):
        execute(**kwargs)
        env.tables[STACK + "-detections"]["TableId"] = "replacement"

    env.cf.execute_change_set.side_effect = drift
    with pytest.raises(FleetManagerError, match="keep polling paused"):
        apply(env, value)


def test_runtime_guard_preserves_customer_tags_that_may_control_access(environment):
    value = plan(environment)
    tags = value["template"]["Resources"]["DetectionBucket"]["Properties"]["Tags"]
    tags.append({"Key": "Tenant", "Value": "customer-scope"})
    config = {
        "security_account_id": ACCOUNT,
        "region": REGION,
        "stage": "prod",
        "recovery_storage": value["template"]["Resources"],
    }
    candidate = runtime_template(value)
    recovery.verify_recovery_template(candidate, config)
    candidate["Resources"]["DetectionBucket"]["Properties"].pop("Tags")
    with pytest.raises(FleetManagerError, match="changes retained DetectionBucket"):
        recovery.verify_recovery_template(candidate, config)


@pytest.mark.parametrize("status", ["CREATE_IN_PROGRESS", "FAILED"])
def test_wait_timeout_distinguishes_pending_from_failed_change_set(environment, status):
    env = environment
    value = plan(env)
    create = env.cf.create_change_set.side_effect

    def pending(**kwargs):
        result = create(**kwargs)
        env.state["change"]["Status"] = "CREATE_IN_PROGRESS"
        return result

    def timeout(**kwargs):
        env.state["change"]["Status"] = status
        raise WaiterError(name="change_set_create_complete", reason="timeout", last_response={})

    env.cf.create_change_set.side_effect = pending
    env.cf.get_waiter.return_value.wait.side_effect = timeout
    if status == "FAILED":
        with pytest.raises(FleetManagerError, match="change set failed"):
            apply(env, value)
    else:
        assert apply(env, value)["status"] == "CHANGE_SET_PENDING"
    env.cf.execute_change_set.assert_not_called()


@pytest.mark.parametrize("status", ["IMPORT_COMPLETE", "IMPORT_IN_PROGRESS"])
def test_cli_saves_paused_identity_only_after_verified_import(
    environment, tmp_path, monkeypatch, status
):
    from opencdr_deploy import cli, fleet_manager

    env = environment
    # Detection and rollout PITR alone must not upgrade standard to hardened.
    env.ddb.describe_continuous_backups.side_effect = lambda TableName: {
        "ContinuousBackupsDescription": {
            "PointInTimeRecoveryDescription": {
                "PointInTimeRecoveryStatus": "ENABLED"
                if TableName.endswith(("-detections", "-rollouts"))
                else "DISABLED"
            }
        }
    }
    value = plan(env)
    path, registry_path = tmp_path / "plan.json", tmp_path / "registry.json"
    path.write_text(json.dumps(value))
    config = {
        "aws_profile": "test",
        "security_account_id": ACCOUNT,
        "region": REGION,
        "stage": "prod",
        "security_profile": "standard",
        "snapshot_retention_hours": 24,
        "allowed_agent_hosts": "",
        "api_url": "https://oldapi1234.execute-api.us-east-1.amazonaws.com/prod",
        "last_artifact": "obsolete",
    }
    registry = {"version": 1, "deployments": {}, "fleet_managers": {"recovered": config}}
    cli.save_registry(registry_path, registry)
    monkeypatch.setattr(fleet_manager, "aws_session", lambda config: env.session)
    mock_apply = Mock(return_value={"status": status})
    monkeypatch.setattr(fleet_manager, "apply_recovery", mock_apply)
    command = [
        "--registry",
        str(registry_path),
        "fleet",
        "recover",
        "recovered",
        "--execute",
        "--plan",
        str(path),
    ]
    assert cli.main(command) == 1
    mock_apply.assert_not_called()
    assert cli.main([*command, "--expected-plan-digest", value["plan_digest"]]) == 0
    saved = cli.load_registry(registry_path)["fleet_managers"]["recovered"]
    if status == "IMPORT_IN_PROGRESS":
        assert saved == config
    else:
        assert saved["recovered_detection_bucket"] == BUCKET
        assert saved["detection_retention_days"] == 90
        assert saved["polling_enabled"] is saved["detection_polling_enabled"] is False
        assert saved["security_profile"] == "standard"
        assert saved["recovery_storage"] == value["template"]["Resources"]
        assert "api_url" not in saved and "last_artifact" not in saved
        assert "--param=detectionEvidenceBucketName=" + BUCKET in fleet_manager._parameters(saved)


def test_recovered_cli_build_and_first_runtime_keep_storage_and_pause(
    environment, tmp_path, monkeypatch
):
    from opencdr_deploy import cli, fleet_manager

    env = environment
    value = plan(env)
    config = {
        "name": "recovered",
        "aws_profile": "test",
        "security_account_id": ACCOUNT,
        "region": REGION,
        "stage": "prod",
        "security_profile": "hardened",
        "polling_enabled": False,
        "detection_polling_enabled": False,
        "recovered_detection_bucket": BUCKET,
        "recovery_storage": value["template"]["Resources"],
        "last_recovery": {"status": "IMPORT_COMPLETE"},
    }
    path = tmp_path / "registry.json"
    cli.save_registry(
        path, {"version": 1, "deployments": {}, "fleet_managers": {"recovered": config}}
    )
    repo = tmp_path / "source"
    (repo / "build").mkdir(parents=True)
    (repo / "build/fleet-manager.zip").write_bytes(b"reviewed zip")
    (repo / "serverless.yml").write_text("detectionEvidenceBucketName: ''")
    monkeypatch.setattr(fleet_manager, "_source", lambda *a: (repo, {"commit": "a" * 40}))
    monkeypatch.setattr(fleet_manager, "_prepare", lambda *a: repo / ".venv/bin/python")
    monkeypatch.setattr(fleet_manager, "aws_session", lambda config: env.session)
    monkeypatch.setattr(fleet_manager, "aws_client", lambda *a: env.cf)
    env.state.update(stack_status="IMPORT_COMPLETE", template=value["template"])
    candidate = runtime_template(value)
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        if command[1] == "package":
            output = Path(command[command.index("--package") + 1])
            output.mkdir()
            (output / "serverless-state.json").write_text(
                json.dumps(
                    {
                        "service": {
                            "initialServerlessConfig": {
                                "package": {"artifact": "build/fleet-manager.zip"}
                            }
                        }
                    }
                )
            )
            (output / "cloudformation-template-update-stack.json").write_text(json.dumps(candidate))
        elif command[1] == "deploy":
            env.state["stack_status"] = "UPDATE_COMPLETE"

    monkeypatch.setattr(cli, "run", runner)
    output = tmp_path / "reviewed"
    base = ["--registry", str(path), "fleet"]
    assert cli.main([*base, "package", "recovered", "--output", str(output)]) == 0
    assert "recovery_storage" in cli.load_registry(path)["fleet_managers"]["recovered"]
    assert cli.main([*base, "deploy", "recovered", "--artifact", str(output)]) == 0
    saved = cli.load_registry(path)["fleet_managers"]["recovered"]
    assert "recovery_storage" not in saved
    assert saved["recovered_detection_bucket"] == BUCKET
    assert saved["last_recovery"] == config["last_recovery"]
    assert saved["polling_enabled"] is saved["detection_polling_enabled"] is False
    for command in (cmd for cmd in calls if cmd[1] in {"package", "deploy"}):
        assert "--param=pollingEnabled=false" in command
        assert "--param=detectionPollingEnabled=false" in command
        assert "--param=detectionEvidenceBucketName=" + BUCKET in command
