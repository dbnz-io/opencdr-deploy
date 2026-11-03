"""Import retained fleet tables after a failed first deployment, without deleting data."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from botocore.exceptions import ClientError

from .fleet_api import FleetManagerError, aws_client

TABLES = {"RegistryTable": "agents", "StatusTable": "status", "ConfigurationTable": "configuration"}


def existing_stack(session, stage):
    try:
        return aws_client(session, "cloudformation").describe_stacks(
            StackName=f"opencdr-fleet-manager-{stage}"
        )["Stacks"][0]
    except ClientError as error:
        if error.response["Error"].get("Code") == "ValidationError" and "does not exist" in str(
            error
        ):
            return None
        raise


def retained_tables(session, stage):
    result = {}
    dynamodb = aws_client(session, "dynamodb")
    for logical, suffix in TABLES.items():
        name = f"opencdr-fleet-manager-{stage}-{suffix}"
        try:
            result[logical] = dynamodb.describe_table(TableName=name)["Table"]
        except ClientError as error:
            if error.response["Error"].get("Code") != "ResourceNotFoundException":
                raise
    return result


def recovery_plan(session, stage):
    if existing_stack(session, stage):
        raise FleetManagerError(
            "A fleet stack already exists; inspect it before attempting recovery"
        )
    tables = retained_tables(session, stage)
    if not tables:
        raise FleetManagerError("No retained fleet tables found; use 'fleet deploy' normally")
    resources, imports = {}, []
    dynamodb = aws_client(session, "dynamodb")
    for logical, table in tables.items():
        name = table["TableName"]
        if table["TableStatus"] != "ACTIVE":
            raise FleetManagerError(f"Retained table {name} is not ACTIVE; wait before importing")
        properties = {key: table[key] for key in ("TableName", "AttributeDefinitions", "KeySchema")}
        mode = table.get("BillingModeSummary", {}).get("BillingMode", "PROVISIONED")
        properties["BillingMode"] = mode
        properties["SSESpecification"] = {"SSEEnabled": True}
        for kind in ("GlobalSecondaryIndexes", "LocalSecondaryIndexes"):
            if table.get(kind):
                properties[kind] = [
                    {key: index[key] for key in ("IndexName", "KeySchema", "Projection")}
                    for index in table[kind]
                ]
        if mode != "PAY_PER_REQUEST":
            raise FleetManagerError("Recovery supports the fleet's on-demand tables only")
        backup = dynamodb.describe_continuous_backups(TableName=name)[
            "ContinuousBackupsDescription"
        ]
        properties["PointInTimeRecoverySpecification"] = {
            "PointInTimeRecoveryEnabled": backup.get("PointInTimeRecoveryDescription", {}).get(
                "PointInTimeRecoveryStatus"
            )
            == "ENABLED"
        }
        ttl = dynamodb.describe_time_to_live(TableName=name)["TimeToLiveDescription"]
        if ttl.get("TimeToLiveStatus") in {"ENABLED", "ENABLING"}:
            properties["TimeToLiveSpecification"] = {
                "Enabled": True,
                "AttributeName": ttl["AttributeName"],
            }
        resources[logical] = {
            "Type": "AWS::DynamoDB::Table",
            "DeletionPolicy": "Retain",
            "UpdateReplacePolicy": "Retain",
            "Properties": properties,
        }
        imports.append(
            {
                "ResourceType": "AWS::DynamoDB::Table",
                "LogicalResourceId": logical,
                "ResourceIdentifier": {"TableName": name},
            }
        )
    template = {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "Reattach retained OpenCDR fleet tables before Serverless deployment",
        "Resources": resources,
    }
    return {
        "stack_name": f"opencdr-fleet-manager-{stage}",
        "template": template,
        "resources_to_import": imports,
        "deletes_data": False,
    }


def apply_recovery(session, plan):
    cloudformation = aws_client(session, "cloudformation")
    name = "opencdr-recover-" + datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
    result = cloudformation.create_change_set(
        StackName=plan["stack_name"],
        ChangeSetName=name,
        ChangeSetType="IMPORT",
        TemplateBody=json.dumps(plan["template"]),
        ResourcesToImport=plan["resources_to_import"],
    )
    cloudformation.get_waiter("change_set_create_complete").wait(
        StackName=result["StackId"],
        ChangeSetName=result["Id"],
        WaiterConfig={"Delay": 3, "MaxAttempts": 100},
    )
    changes = cloudformation.describe_change_set(ChangeSetName=result["Id"])
    if any(
        row.get("ResourceChange", {}).get("Action") != "Import"
        for row in changes.get("Changes", [])
    ):
        raise FleetManagerError("Recovery change set contains a non-import action; not executing")
    cloudformation.execute_change_set(ChangeSetName=result["Id"])
    cloudformation.get_waiter("stack_import_complete").wait(
        StackName=result["StackId"],
        WaiterConfig={"Delay": 5, "MaxAttempts": 120},
    )
    return {"stack_id": result["StackId"], "status": "IMPORT_COMPLETE"}
