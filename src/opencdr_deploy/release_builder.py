"""Maintainer-side export from an audited compiled template to a portable release."""

import json
import re
import shutil
import zipfile
from pathlib import Path

from .artifacts import PRODUCTS, sha256
from .fleet_api import FleetManagerError


def export_release(
    template_path, runtime_path, destination, *, product, version, stage, region, account_id
):
    if product not in PRODUCTS or not re.fullmatch(r"\d{12}", account_id):
        raise FleetManagerError("Invalid build product or account")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", stage):
        raise FleetManagerError("Invalid build stage")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?", version):
        raise FleetManagerError("Invalid release version")
    if not zipfile.is_zipfile(runtime_path):
        raise FleetManagerError("Release runtime must be a Lambda ZIP archive")
    template = json.loads(Path(template_path).read_text())
    code_keys = {
        json.dumps(resource["Properties"]["Code"], sort_keys=True)
        for resource in template["Resources"].values()
        if resource["Type"] == "AWS::Lambda::Function" and "S3Key" in resource["Properties"]["Code"]
    }
    if len(code_keys) != 1:
        raise FleetManagerError(
            "Portable releases require one audited runtime ZIP; "
            "multiple code archives need a new manifest schema"
        )

    def portable(value, *, substitution=False):
        if isinstance(value, dict):
            if set(value) == {"Fn::Sub"}:
                content = value["Fn::Sub"]
                if isinstance(content, str):
                    return {"Fn::Sub": portable(content, substitution=True)}
                return {"Fn::Sub": [portable(content[0], substitution=True), portable(content[1])]}
            return {key: portable(item) for key, item in value.items()}
        if isinstance(value, list):
            return [portable(item) for item in value]
        if not isinstance(value, str):
            return value
        changed = value.replace(product + "-" + stage, product + "-${Stage}")
        changed = changed.replace(
            product.replace("-", "_") + "_" + stage,
            product.replace("-", "_") + "_${Stage}",
        )
        if value == stage + "-" + product:
            changed = "${Stage}-" + product
        if value == product + "-opencdrPlan-" + stage:
            changed = product + "-opencdrPlan-${Stage}"
        changed = changed.replace(product + " " + stage + " stage", product + " ${Stage} stage")
        changed = changed.replace(account_id, "${AWS::AccountId}").replace(region, "${AWS::Region}")
        changed = changed.replace("arn:aws:", "arn:${AWS::Partition}:")
        changed = changed.replace("amazonaws.com", "${AWS::URLSuffix}")
        changed = re.sub(rf"(?<=/){re.escape(stage)}(?=/|$)", "${Stage}", changed)
        if changed == stage:
            return "${Stage}" if substitution else {"Ref": "Stage"}
        return changed if substitution or changed == value else {"Fn::Sub": changed}

    # Keep logical IDs stable across upgrades; transform values only.
    template = portable(template)
    detection_feed = product == "opencdr" and any(
        "DETECTION_FEED_RETENTION_DAYS"
        in resource.get("Properties", {}).get("Environment", {}).get("Variables", {})
        for resource in template["Resources"].values()
    )
    fleet_detections = product == "opencdr-fleet-manager" and any(
        "DETECTION_POLL_INTERVAL_MINUTES"
        in resource.get("Properties", {}).get("Environment", {}).get("Variables", {})
        for resource in template["Resources"].values()
    )
    fleet_polling = product == "opencdr-fleet-manager" and any(
        "POLL_INTERVAL_MINUTES"
        in resource.get("Properties", {}).get("Environment", {}).get("Variables", {})
        for resource in template["Resources"].values()
    )
    runtime_digest = sha256(Path(runtime_path).read_bytes())
    from .ownership_contract import KEY, runtime_contract

    ownership = runtime_contract(Path(runtime_path).read_bytes(), product)
    if ownership:
        template.setdefault("Metadata", {})[KEY] = ownership
    else:
        template.get("Metadata", {}).pop(KEY, None)

    for resource in template["Resources"].values():
        if resource["Type"] == "AWS::IAM::Role" and "RoleName" in resource["Properties"]:
            # Serverless shortens names at build time. A different install stage
            # can exceed IAM's 64-character limit. New stacks use CF-generated
            # names; migrations explicitly preserve existing physical names.
            resource["Properties"]["RoleName"] = {
                "Fn::If": [
                    "PreserveRoleNames",
                    resource["Properties"]["RoleName"],
                    {"Ref": "AWS::NoValue"},
                ]
            }
        if resource["Type"] == "AWS::Lambda::Function":
            properties = resource["Properties"]
            if "S3Key" in properties["Code"]:
                properties["Code"] = {
                    "S3Bucket": {"Ref": "ArtifactBucket"},
                    "S3Key": {"Ref": "ArtifactKey"},
                }
            environment = properties.get("Environment", {}).get("Variables", {})
            if environment.get("DETECTION_ACTIVATION_REQUIRED") == "true":
                environment["DETECTION_RUNTIME_ID"] = runtime_digest
            if "OPENCDR_LEGACY_API_ENABLED" in environment:
                environment["OPENCDR_LEGACY_API_ENABLED"] = {"Ref": "LegacyApiEnabled"}
            if detection_feed and "DETECTION_FEED_RETENTION_DAYS" in environment:
                if (
                    not str(environment["DETECTION_FEED_RETENTION_DAYS"]).isdigit()
                    or not 7 <= int(environment["DETECTION_FEED_RETENTION_DAYS"]) <= 30
                ):
                    raise FleetManagerError("Detection replay retention must be 7–30 days")
                environment["DETECTION_FEED_RETENTION_DAYS"] = {"Ref": "DetectionReplayDays"}
            if fleet_detections and "DETECTION_POLL_INTERVAL_MINUTES" in environment:
                for env_key, parameter in (
                    ("DETECTION_POLL_INTERVAL_MINUTES", "DetectionPollIntervalMinutes"),
                    ("DETECTION_RETENTION_DAYS", "DetectionRetentionDays"),
                    ("DETECTION_POLLING_ENABLED", "DetectionPollingEnabled"),
                ):
                    environment[env_key] = {"Ref": parameter}
            if fleet_polling and "POLL_INTERVAL_MINUTES" in environment:
                environment["POLL_INTERVAL_MINUTES"] = {"Ref": "PollIntervalMinutes"}
                environment["FULL_REFRESH_MINUTES"] = {"Ref": "FullRefreshMinutes"}
        if resource["Type"] == "AWS::ApiGateway::RestApi":
            policy = resource["Properties"].get("Policy") or {}
            for statement in policy.get("Statement", []) if isinstance(policy, dict) else []:
                arn_equals = statement.get("Condition", {}).get("ArnEquals", {})
                if "aws:PrincipalArn" in arn_equals:
                    arn_equals["aws:PrincipalArn"] = {
                        "Fn::Split": [",", {"Ref": "ManagementPrincipalArns"}]
                    }
    template.setdefault("Parameters", {}).update(
        {
            "Stage": {"Type": "String", "AllowedPattern": "[a-z][a-z0-9-]{0,17}"},
            "ArtifactBucket": {"Type": "String"},
            "ArtifactKey": {"Type": "String"},
            "LegacyApiEnabled": {
                "Type": "String",
                "Default": "false",
                "AllowedValues": ["true", "false"],
            },
            "ManagementPrincipalArns": {"Type": "String", "Default": ""},
            "PreserveRoleNames": {
                "Type": "String",
                "Default": "false",
                "AllowedValues": ["true", "false"],
            },
        }
    )
    template.setdefault("Conditions", {})["PreserveRoleNames"] = {
        "Fn::Equals": [{"Ref": "PreserveRoleNames"}, "true"]
    }
    if detection_feed:
        template["Parameters"]["DetectionReplayDays"] = {
            "Type": "Number",
            "Default": 7,
            "MinValue": 7,
            "MaxValue": 30,
        }
    if fleet_polling:
        template["Parameters"].update(
            {
                "PollIntervalMinutes": {
                    "Type": "Number",
                    "Default": 1,
                    "MinValue": 1,
                    "MaxValue": 60,
                },
                "FullRefreshMinutes": {
                    "Type": "Number",
                    "Default": 60,
                    "MinValue": 1,
                    "MaxValue": 1440,
                },
            }
        )
        template["Conditions"]["PollEveryMinute"] = {
            "Fn::Equals": [{"Ref": "PollIntervalMinutes"}, "1"]
        }
        template["Resources"]["ScheduleEventsRuleSchedule1"]["Properties"]["ScheduleExpression"] = {
            "Fn::If": [
                "PollEveryMinute",
                "rate(1 minute)",
                {"Fn::Sub": "rate(${PollIntervalMinutes} minutes)"},
            ]
        }
    if fleet_detections:
        template["Parameters"]["DetectionEvidenceBucketName"] = {
            "Type": "String",
            "Default": "",
            "AllowedPattern": "^$|^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$",
        }
        template["Conditions"]["HasPinnedDetectionBucket"] = {
            "Fn::Not": [{"Fn::Equals": [{"Ref": "DetectionEvidenceBucketName"}, ""]}]
        }
        template["Resources"]["DetectionBucket"]["Properties"]["BucketName"] = {
            "Fn::If": [
                "HasPinnedDetectionBucket",
                {"Ref": "DetectionEvidenceBucketName"},
                {"Ref": "AWS::NoValue"},
            ]
        }
        for key, default, low, high in (
            ("DetectionPollIntervalMinutes", 1, 1, 60),
            ("DetectionRetentionDays", 90, 7, 365),
            ("DetectionPollConcurrency", 2, 2, 1000),
        ):
            template["Parameters"][key] = {
                "Type": "Number",
                "Default": default,
                "MinValue": low,
                "MaxValue": high,
            }
        template["Parameters"]["DetectionPollingEnabled"] = {
            "Type": "String",
            "Default": "false",
            "AllowedValues": ["true", "false"],
        }
        template["Conditions"]["DetectionPollEveryMinute"] = {
            "Fn::Equals": [{"Ref": "DetectionPollIntervalMinutes"}, "1"]
        }
        template["Conditions"]["DetectionPollingEnabled"] = {
            "Fn::Equals": [{"Ref": "DetectionPollingEnabled"}, "true"]
        }
        schedule = template["Resources"]["DetectionScheduleEventsRuleSchedule1"]["Properties"]
        schedule["ScheduleExpression"] = {
            "Fn::If": [
                "DetectionPollEveryMinute",
                "rate(1 minute)",
                {"Fn::Sub": "rate(${DetectionPollIntervalMinutes} minutes)"},
            ]
        }
        schedule["State"] = {"Fn::If": ["DetectionPollingEnabled", "ENABLED", "DISABLED"]}
        lifecycle = template["Resources"]["DetectionBucket"]["Properties"]["LifecycleConfiguration"]
        lifecycle["Rules"][0]["ExpirationInDays"] = {"Ref": "DetectionRetentionDays"}
        for resource in template["Resources"].values():
            if resource[
                "Type"
            ] == "AWS::Lambda::EventSourceMapping" and "DetectionPollLambdaFunction" in json.dumps(
                resource["Properties"].get("FunctionName")
            ):
                resource["Properties"]["ScalingConfig"]["MaximumConcurrency"] = {
                    "Ref": "DetectionPollConcurrency"
                }
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "template.json").write_text(json.dumps(template, indent=2) + "\n")
    shutil.copyfile(runtime_path, destination / "runtime.zip")
    manifest = {
        "schema": "opencdr.deployment/1",
        "product": product,
        "version": version,
        "management_contract_major": 2 if product == "opencdr" else 1,
        "files": {
            name: sha256((destination / name).read_bytes())
            for name in ("template.json", "runtime.zip")
        },
    }
    raw = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    (destination / "release.json").write_bytes(raw)
    return {"directory": str(destination), "sha256": sha256(raw), "manifest": manifest}
