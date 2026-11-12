"""AWS bindings for the reviewed native-ownership transfer controller.

Only engines with the exact installed ownership-fence contract are eligible.
The existing Deploy recovery lease excludes deployment and physical recovery.
Retained plans, archives and completion receipts are immutable; no cleanup here
removes operational data, permanent claims or historical consumers.
"""

import base64
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from . import ownership_contract as contract
from . import ownership_transfer as transfer
from .artifacts import load_release
from .detection_activation import resource_ids, template_body
from .fleet_api import aws_client, verify_account
from .migration_archive import MAX_BYTES, canonical, read, require, sha, verify
from .storage_bindings import bound_ids
from .storage_quarantine import _get, require_unlocked
from .storage_snapshot import sha256 as object_sha


def inventory(session, account, region, stack, family, directory, release_sha256):
    verify_account(session, account)
    manifest, _ = load_release(directory, release_sha256)
    expected = contract.runtime_contract(
        (Path(directory) / "runtime.zip").read_bytes(), manifest["product"], required=True
    )
    cf = aws_client(session, "cloudformation")
    current = cf.describe_stacks(StackName=stack)["Stacks"][0]
    require(
        current["StackStatus"] in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}, "Engine stack unsettled"
    )
    require(
        current["StackId"].startswith(f"arn:aws:cloudformation:{region}:{account}:stack/"),
        "Engine account or region differs",
    )
    require_unlocked(session, current)
    template = template_body(cf, StackName=stack)
    require(
        template.get("Metadata", {}).get(contract.KEY) == expected,
        "Deploy the pinned ownership-capable runtime before migration",
    )
    ids = bound_ids(resource_ids(cf, stack, template), template)
    table, bucket = (
        (ids["CaseTable"], ids["CaseBucket"])
        if family == "cases"
        else (ids["LibraryTable"], ids["LibraryBucket"])
    )
    desc = aws_client(session, "dynamodb").describe_table(TableName=table)["Table"]
    require(desc["TableStatus"] == "ACTIVE", "Source table unavailable")
    require(
        desc.get("KeySchema")
        == [
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        "Unexpected native key schema",
    )
    params = {p["ParameterKey"]: p["ParameterValue"] for p in current["Parameters"]}
    pin = {
        "product": manifest["product"],
        "stage": params["Stage"],
        "account_id": account,
        "region": region,
        "stack_id": current["StackId"],
        "change_set_id": current.get("ChangeSetId"),
        "template_sha256": sha(canonical(template)),
        "table": table,
        "table_id": desc["TableId"],
        "bucket": bucket,
        "artifact_bucket": params["ArtifactBucket"],
        "release_sha256": release_sha256,
        "runtime_sha256": expected["runtime_sha256"],
        "contract": expected,
        "functions": {
            name: ids[name]
            for name, r in template["Resources"].items()
            if r["Type"] == "AWS::Lambda::Function"
        },
    }
    if manifest["product"] == "opencdr-fleet-manager":
        registry = aws_client(session, "dynamodb").describe_table(TableName=ids["RegistryTable"])[
            "Table"
        ]
        require(registry["TableStatus"] == "ACTIVE", "Fleet registry unavailable")
        pin["registry_table"] = ids["RegistryTable"]
        pin["registry_table_id"] = registry["TableId"]
    pin["scope_bindings"] = {}
    verify_functions(session, pin)
    for name in [bucket, params["ArtifactBucket"]]:
        aws_client(session, "s3").head_bucket(Bucket=name, ExpectedBucketOwner=account)
    return pin


def validate_scope_map(source, target, family, mapping):
    require(
        isinstance(mapping, dict)
        and all(isinstance(k, str) and isinstance(v, str) for k, v in mapping.items()),
        "Explicit scope map required",
    )
    if family == "library":
        require(not mapping, "Library identity is owner-wide")
        return
    required = set()
    for item in source.rows("cases"):
        row = transfer.decoded(item)
        if row["pk"] == "CASE":
            required.update(row["agent_ids"])
    require(
        set(mapping) == required and len(set(mapping.values())) == len(mapping),
        "Scope map must cover exactly the source cases without collisions",
    )

    def scope(store, name):
        pin = store.pin
        if pin["product"] == "opencdr":
            expected = (pin["account_id"], pin["region"], pin["stage"])
            require(name == ":".join(expected), "Case scope is not this Core installation")
            return expected
        require(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", name), "Invalid Fleet case scope")
        row = store.ddb.get_item(
            TableName=pin["registry_table"],
            Key=transfer.wire({"id": "AGENT#" + name}),
            ConsistentRead=True,
        ).get("Item")
        require(row is not None, "Register the exact Core identity before case transfer")
        value = transfer.decoded(row)
        pin["scope_bindings"][name] = sha(canonical(row))
        return tuple(value[k] for k in ["account_id", "region", "stage"])

    for old, new in mapping.items():
        require(
            scope(source, old) == scope(target, new), "Mapping changes the underlying Core identity"
        )


def verify_functions(session, pin):
    lambdas = aws_client(session, "lambda")
    for name in pin["functions"].values():
        c = lambdas.get_function_configuration(FunctionName=name)
        require(
            c.get("State") == "Active"
            and c.get("LastUpdateStatus") == "Successful"
            and base64.b64decode(c["CodeSha256"]).hex() == pin["runtime_sha256"],
            "An engine function differs from its pinned ownership runtime",
        )


def lock_key(pin):
    return "opencdr-recovery/locks/" + object_sha(pin["stack_id"]) + ".json"


def lock_value(digest):
    return {"plan_sha256": digest, "kind": "native-ownership"}


def lease(session, pin, digest):
    s3 = aws_client(session, "s3")
    bucket = pin["artifact_bucket"]
    account = pin["account_id"]
    old, etag = _get(s3, bucket, lock_key(pin), account)
    if old == lock_value(digest):
        return
    require(
        old is None
        or (set(old) == {"released_plan_sha256"} and old["released_plan_sha256"] != digest),
        "Another recovery owns this engine or this transfer is already released",
    )
    s3.put_object(
        Bucket=bucket,
        Key=lock_key(pin),
        Body=canonical(lock_value(digest)),
        ExpectedBucketOwner=account,
        ServerSideEncryption="AES256",
        **({"IfMatch": etag} if old is not None else {"IfNoneMatch": "*"}),
    )


def guard(session, pin, digest=None):
    verify_account(session, pin["account_id"])
    cf = aws_client(session, "cloudformation")
    current = cf.describe_stacks(StackName=pin["stack_id"])["Stacks"][0]
    require(
        current["StackStatus"] in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}
        and current.get("ChangeSetId") == pin["change_set_id"],
        "Engine deployment changed",
    )
    ddb = aws_client(session, "dynamodb")
    desc = ddb.describe_table(TableName=pin["table"])["Table"]
    require(
        desc["TableId"] == pin["table_id"] and desc["TableStatus"] == "ACTIVE",
        "Native storage physical identity changed",
    )
    if pin.get("scope_bindings"):
        registry = ddb.describe_table(TableName=pin["registry_table"])["Table"]
        require(
            registry["TableId"] == pin["registry_table_id"] and registry["TableStatus"] == "ACTIVE",
            "Fleet scope registry physical identity changed",
        )
        for agent, expected in pin["scope_bindings"].items():
            row = ddb.get_item(
                TableName=pin["registry_table"],
                Key=transfer.wire({"id": "AGENT#" + agent}),
                ConsistentRead=True,
            ).get("Item")
            require(
                row is not None and sha(canonical(row)) == expected,
                "Case migration registration changed; reconcile the reviewed plan",
            )
    global_state = ddb.get_item(
        TableName=pin["table"],
        Key=transfer.wire({"pk": "OWNERSHIP", "sk": "STATE"}),
        ConsistentRead=True,
    ).get("Item")
    if global_state is not None:
        transfer._initial(global_state, "active")
    if digest is None:
        require_unlocked(session, current)
    else:
        held, _ = _get(
            aws_client(session, "s3"), pin["artifact_bucket"], lock_key(pin), pin["account_id"]
        )
        require(held == lock_value(digest), "Reviewed migration lease is not held")


def store(session, pin, digest=None):
    return transfer.Store(
        aws_client(session, "dynamodb"),
        aws_client(session, "s3"),
        pin,
        lambda selected: guard(session, selected, digest),
    )


def retain(session, pin, digest, name, raw):
    require(len(raw) <= MAX_BYTES, "Retained migration object byte limit")
    s3 = aws_client(session, "s3")
    key = "opencdr-migrations/" + digest + "/" + name
    try:
        s3.put_object(
            Bucket=pin["artifact_bucket"],
            Key=key,
            Body=raw,
            ExpectedBucketOwner=pin["account_id"],
            IfNoneMatch="*",
            ServerSideEncryption="AES256",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] not in {"PreconditionFailed", "412"}:
            raise
        response = s3.get_object(
            Bucket=pin["artifact_bucket"], Key=key, ExpectedBucketOwner=pin["account_id"]
        )
        try:
            existing = response["Body"].read(MAX_BYTES + 1)
        finally:
            response["Body"].close()
        require(existing == raw, "Retained migration object differs")
    return {"bucket": pin["artifact_bucket"], "key": key, "sha256": sha(raw)}


def load_plan(path, digest):
    raw = read(Path(path))
    doc = json.loads(raw)
    require(sha(canonical(doc)) == digest, "Reviewed plan digest mismatch")
    require(doc.get("schema") == transfer.SCHEMA, "Unsupported ownership plan")
    return doc


def sessions(args, plan):
    return [
        boto3.Session(
            profile_name=getattr(args, side + "_profile"), region_name=plan[side]["region"]
        )
        for side in ["source", "target"]
    ]


def retained_archive(session, pin, digest, directory, manifest_sha256):
    manifest, _ = verify(directory, manifest_sha256)
    for file in ["manifest.json", *[c["file"] for c in manifest["chunks"]]]:
        retain(
            session,
            pin,
            digest,
            "archives/" + manifest_sha256 + "/" + file,
            read(Path(directory) / file),
        )


def cancel_unstarted(session, plan, digest):
    """Fence a never-started plan in its journal before releasing partial leases.

    This writes only a cancellation receipt, never an operational ownership row.
    A concurrent freeze either wins journal creation or sees this terminal phase.
    """
    pin = plan["source"]
    verify_account(session, pin["account_id"])
    held, _ = _get(
        aws_client(session, "s3"), pin["artifact_bucket"], lock_key(pin), pin["account_id"]
    )
    require(held == lock_value(digest), "Unstarted cancellation requires its source lease")
    ddb = aws_client(session, "dynamodb")
    description = ddb.describe_table(TableName=pin["table"])["Table"]
    require(description["TableId"] == pin["table_id"], "Cancellation storage identity changed")
    row = transfer.wire(
        {
            "pk": "OWNERSHIP",
            "sk": "TRANSFER#" + digest,
            "plan_sha256": digest,
            "phase": "cancelled_unstarted",
        }
    )
    ddb.put_item(TableName=pin["table"], Item=row, ConditionExpression="attribute_not_exists(pk)")
    return transfer.plain(transfer.decoded(row))


def complete(sessions_, plan, digest):
    source_session, target_session = sessions_
    source = store(source_session, plan["source"], digest)
    target = store(target_session, plan["target"], digest)
    c = transfer.Transfer(source, target, plan, digest)
    j = c.journal()
    require(
        j and j["phase"] in {"complete", "cancelled", "cancelled_unstarted"},
        "Finish or cancel before releasing leases",
    )
    if j["phase"] == "cancelled_unstarted":
        expected = []
    elif j["phase"] == "complete":
        expected = [c.sr, c.ta]
    else:
        expected = [
            transfer.state(c.family, "active", plan["source_revision"] + 2, digest),
            transfer.state(
                c.family,
                "retired" if plan["previous"] else "active",
                plan["target_revision"] + 2,
                digest,
            ),
        ]
        for side, value in zip(["source", "target"], expected, strict=True):
            value["owner_transfer"] = (plan[side + "_initial"] or {}).get(
                "owner_transfer", {"NULL": True}
            )
    for client, value in zip([source, target] if expected else [], expected, strict=True):
        require(client.get("OWNERSHIP", c.family) == value, "Final ownership epoch changed")
    receipt = {
        "schema": "opencdr.ownership-completion/1",
        "plan_sha256": digest,
        "journal": j,
        "retained_until": plan["retain_until"],
        "data_deleted": False,
    }
    # The same durable bytes make a response-loss retry idempotent.
    for session, side in zip(sessions_, ["source", "target"], strict=True):
        pin = plan[side]
        verify_account(session, pin["account_id"])
        s3 = aws_client(session, "s3")
        old, etag = _get(s3, pin["artifact_bucket"], lock_key(pin), pin["account_id"])
        released = {"released_plan_sha256": digest}
        if j["phase"] == "cancelled_unstarted" and old != lock_value(digest):
            continue  # No ownership changes; never touch another controller's lease.
        require(old in [lock_value(digest), released], "Migration lease changed before completion")
        if old == released:
            continue
        if j["phase"] != "cancelled_unstarted":
            guard(session, pin, digest)
            verify_functions(session, pin)
        retain(session, pin, digest, "completion.json", canonical(receipt))
        s3.put_object(
            Bucket=pin["artifact_bucket"],
            Key=lock_key(pin),
            Body=canonical(released),
            ExpectedBucketOwner=pin["account_id"],
            IfMatch=etag,
            ServerSideEncryption="AES256",
        )
    return receipt


def retrieve(session, account, bucket, digest, directory, manifest_sha256=None):
    """Recover pinned operator files from retained evidence; no AWS mutations."""
    verify_account(session, account)
    require(re.fullmatch(r"[a-f0-9]{64}", digest or ""), "Invalid transfer digest")
    require(not directory.exists(), "Retrieve into a new directory")
    s3 = aws_client(session, "s3")
    total = 0

    def get(name):
        nonlocal total
        response = s3.get_object(
            Bucket=bucket,
            Key="opencdr-migrations/" + digest + "/" + name,
            ExpectedBucketOwner=account,
        )
        try:
            raw = response["Body"].read(MAX_BYTES + 1)
        finally:
            response["Body"].close()
        total += len(raw)
        require(total <= 2 * MAX_BYTES and len(raw) <= MAX_BYTES, "Retained transfer byte limit")
        return raw

    raw = get("plan.json")
    plan = json.loads(raw)
    require(
        sha(canonical(plan)) == digest and plan.get("schema") == transfer.SCHEMA,
        "Retained plan digest or schema differs",
    )
    require(
        any(
            plan[side]["account_id"] == account and plan[side]["artifact_bucket"] == bucket
            for side in ["source", "target"]
        ),
        "Retained plan belongs to another owner",
    )
    directory.mkdir(parents=True)
    with (directory / "plan.json").open("xb") as output:
        output.write(raw)
    if manifest_sha256:
        require(re.fullmatch(r"[a-f0-9]{64}", manifest_sha256), "Invalid archive digest")
        require(
            plan["source"]["account_id"] == account and plan["source"]["artifact_bucket"] == bucket,
            "Final-delta archives are retained in the source artifact bucket",
        )
        prefix = "archives/" + manifest_sha256 + "/"
        raw = get(prefix + "manifest.json")
        require(sha(raw) == manifest_sha256, "Retained manifest digest differs")
        manifest = json.loads(raw)
        archive = directory / "archive"
        archive.mkdir()
        with (archive / "manifest.json").open("xb") as output:
            output.write(raw)
        require(
            isinstance(manifest.get("chunks"), list) and len(manifest["chunks"]) <= 1000,
            "Retained chunk count limit",
        )
        for index, chunk in enumerate(manifest["chunks"]):
            value = chunk["sha256"]
            require(
                re.fullmatch(r"[a-f0-9]{64}", value)
                and chunk["file"] == f"chunk-{index:06d}-{value}.json",
                "Unsafe retained chunk path",
            )
            raw = get(prefix + chunk["file"])
            require(sha(raw) == value, "Retained chunk digest differs")
            with (archive / chunk["file"]).open("xb") as output:
                output.write(raw)
        verify(archive, manifest_sha256)
    return {
        "plan_sha256": digest,
        "manifest_sha256": manifest_sha256,
        "directory": str(directory),
        "cloud_mutations": False,
    }


def configure(commands):
    root = commands.add_parser("ownership", help="Reviewed native Core/Fleet ownership transfer")
    actions = root.add_subparsers(dest="ownership_action", required=True)
    p = actions.add_parser(
        "retrieve", help="Recover pinned plan/archive files from retained evidence"
    )
    for name in ["profile", "account-id", "region", "artifact-bucket", "sha256"]:
        p.add_argument("--" + name, required=True)
    p.add_argument("--directory", type=Path, required=True)
    p.add_argument("--manifest-sha256")
    p.add_argument("--output", type=Path, required=True)
    p = actions.add_parser("plan", help="Read-only pinned source/empty-target plan; no fences")
    for side in ["source", "target"]:
        for name in ["profile", "account-id", "region", "stack", "release", "release-sha256"]:
            p.add_argument("--" + side + "-" + name, required=True)
    p.add_argument("--family", choices=["cases", "library"], required=True)
    p.add_argument("--agent-map", type=Path, required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--retention-days", type=int, default=30)
    p.add_argument("--previous-plan", type=Path)
    p.add_argument("--previous-sha256")
    p.add_argument("--output", type=Path, required=True)
    for action in ["freeze", "capture", "stage", "activate", "cancel", "complete", "status"]:
        p = actions.add_parser(action)
        for side in ["source", "target"]:
            p.add_argument("--" + side + "-profile", required=True)
        p.add_argument("--plan", type=Path, required=True)
        p.add_argument("--sha256", required=True)
        p.add_argument("--output", type=Path, required=True)
        if action in ["capture", "stage", "activate"]:
            p.add_argument("--directory", type=Path, required=True)
        if action in ["stage", "activate"]:
            p.add_argument("--manifest-sha256", required=True)


def run(args):
    require(not args.output.exists(), "Use a new output file; preserve prior evidence")
    action = args.ownership_action
    if action == "retrieve":
        session = boto3.Session(profile_name=args.profile, region_name=args.region)
        result = retrieve(
            session,
            args.account_id,
            args.artifact_bucket,
            args.sha256,
            args.directory,
            args.manifest_sha256,
        )
        with args.output.open("xb") as output:
            output.write(canonical(result))
        print(json.dumps(result))
        return 0
    if action == "plan":
        require(7 <= args.retention_days <= 365, "Recovery retention must be7–365days")
        require(
            bool(args.previous_plan) == bool(args.previous_sha256),
            "Pin the previous plan for reverse recovery",
        )
        pairs = []
        for side in ["source", "target"]:

            def arg(k, selected_side=side):
                return getattr(args, selected_side + "_" + k)

            session = boto3.Session(profile_name=arg("profile"), region_name=arg("region"))
            pin = inventory(
                session,
                arg("account_id"),
                arg("region"),
                arg("stack"),
                args.family,
                arg("release"),
                arg("release_sha256"),
            )
            pairs.append(store(session, pin))
        mapping = json.loads(read(args.agent_map))
        validate_scope_map(*pairs, args.family, mapping)
        previous = None
        if args.previous_plan:
            parent = load_plan(args.previous_plan, args.previous_sha256)
            row = pairs[1].get("OWNERSHIP", "TRANSFER#" + args.previous_sha256)
            require(row is not None, "Previous completion is absent")
            previous = {"plan": parent, "completion": transfer.plain(transfer.decoded(row))}
        plan = transfer.plan(
            *pairs,
            args.family,
            mapping,
            reason=args.reason,
            retain_until=int((datetime.now(UTC) + timedelta(days=args.retention_days)).timestamp()),
            previous=previous,
        )
        # The output contains the exact plan, not a wrapper. Hash canonical bytes.
        args.output.open("xb").write(canonical(plan))
        print(json.dumps({"plan_sha256": sha(canonical(plan)), "output": str(args.output)}))
        return 0
    plan = load_plan(args.plan, args.sha256)
    sess = sessions(args, plan)
    for session, side in zip(sess, ["source", "target"], strict=True):
        verify_account(session, plan[side]["account_id"])
    c = transfer.Transfer(
        *[
            store(s, plan[side], args.sha256)
            for s, side in zip(sess, ["source", "target"], strict=True)
        ],
        plan,
        args.sha256,
    )
    if action == "freeze":
        for s, side in zip(sess, ["source", "target"], strict=True):
            verify_account(s, plan[side]["account_id"])
            retain(s, plan[side], args.sha256, "plan.json", canonical(plan))
            lease(s, plan[side], args.sha256)
        for s, side in zip(sess, ["source", "target"], strict=True):
            verify_functions(s, plan[side])
        result = c.freeze()
    elif action == "capture":
        c.frozen()
        from .library_archive import capture

        result = capture(
            sess[0],
            plan["source"]["account_id"],
            plan["source"]["region"],
            plan["source"]["stack_id"],
            args.directory,
            family=plan["family"],
            migration_guard=c.source.check,
        )
        c.material(args.directory, result["manifest_sha256"])
        retained_archive(
            sess[0], plan["source"], args.sha256, args.directory, result["manifest_sha256"]
        )
    elif action in ["stage", "activate"]:
        retained_archive(sess[0], plan["source"], args.sha256, args.directory, args.manifest_sha256)
        if action == "activate":
            for s, side in zip(sess, ["source", "target"], strict=True):
                verify_functions(s, plan[side])
        result = getattr(c, action)(args.directory, args.manifest_sha256)
    elif action == "cancel":
        journal = c.journal()
        if journal is None:
            result = cancel_unstarted(sess[0], plan, args.sha256)
        elif journal["phase"] == "cancelled_unstarted":
            result = journal
        else:
            result = c.cancel_before_import()
    elif action == "complete":
        result = complete(sess, plan, args.sha256)
    else:
        result = {
            "journal": c.journal(),
            "source": c.source.get("OWNERSHIP", plan["family"]),
            "target": c.target.get("OWNERSHIP", plan["family"]),
        }
    args.output.open("xb").write(canonical(result))
    print(json.dumps({"output": str(args.output), "result": result}, default=str))
    return 0
