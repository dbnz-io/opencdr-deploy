"""Pinned ownership-fence contract for native engine release transitions."""

import io
import zipfile

from .migration_archive import require, sha

SCHEMA = "opencdr.native-ownership/1"
MODULE_SHA256 = "129998b631b9a7345b066581064b40aaf149e50b27ba93815ed8f80178842051"
PATHS = {
    "opencdr": "src/management/case_store.py",
    "opencdr-fleet-manager": "opencdr_fleet/_contracts/case_store.py",
}
KEY = "OpenCDROwnership"


def runtime_contract(raw, product, *, required=False):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        path = PATHS[product]
        names = archive.namelist()
        if path not in names:
            require(not required, "Runtime lacks ownership fence module")
            return None
        require(names.count(path) == 1, "Ambiguous ownership module in runtime")
        info = archive.getinfo(path)
        require(info.file_size <= 200000, "Ownership module exceeds byte bound")
        if sha(archive.read(path)) != MODULE_SHA256:
            require(not required, "Runtime ownership fence contract is unsupported")
            return None
    return {"schema": SCHEMA, "module_sha256": MODULE_SHA256, "runtime_sha256": sha(raw)}


def verify_template(template, raw, product):
    marker = template.get("Metadata", {}).get(KEY)
    if marker is None:
        return
    require(
        marker == runtime_contract(raw, product, required=True),
        "Ownership marker differs from actual pinned runtime",
    )
    for resource in template.get("Resources", {}).values():
        if resource.get("Type") == "AWS::Lambda::Function":
            require(
                resource["Properties"]["Code"]
                == {"S3Bucket": {"Ref": "ArtifactBucket"}, "S3Key": {"Ref": "ArtifactKey"}},
                "Ownership release contains an unpinned function",
            )


def require_compatible(previous, proposed):
    old = previous.get("Metadata", {}).get(KEY)
    if old is None:
        return
    new = proposed.get("Metadata", {}).get(KEY)
    require(
        old.get("schema") == SCHEMA
        and new
        and new.get("schema") == SCHEMA
        and new.get("module_sha256") == MODULE_SHA256,
        "Ownership-aware engine cannot downgrade to an unfenced runtime",
    )


def verify_change_runtime(session, review, proposed):
    marker = proposed.get("Metadata", {}).get(KEY)
    if marker is None:
        return
    from .fleet_api import aws_client

    parameters = {p["ParameterKey"]: p.get("ParameterValue") for p in review["Parameters"]}
    if not parameters.get("ArtifactBucket") or not parameters.get("ArtifactKey"):
        live = aws_client(session, "cloudformation").describe_stacks(StackName=review["StackId"])[
            "Stacks"
        ][0]
        previous = {p["ParameterKey"]: p["ParameterValue"] for p in live.get("Parameters", [])}
        for entry in review["Parameters"]:
            if entry.get("UsePreviousValue"):
                parameters[entry["ParameterKey"]] = previous.get(entry["ParameterKey"])
    require(
        parameters.get("ArtifactBucket") and parameters.get("ArtifactKey"),
        "Ownership runtime parameters are unresolved",
    )
    account = review["StackId"].split(":")[4]
    response = aws_client(session, "s3").get_object(
        Bucket=parameters["ArtifactBucket"],
        Key=parameters["ArtifactKey"],
        ExpectedBucketOwner=account,
    )
    try:
        raw = response["Body"].read(250000001)
    finally:
        response["Body"].close()
    require(len(raw) <= 250000000, "Ownership runtime byte limit")
    product = (
        "opencdr-fleet-manager"
        if review["StackName"].startswith("opencdr-fleet-manager-")
        else "opencdr"
    )
    verify_template(proposed, raw, product)


def guard_legacy_deployment(session, stack):
    """Legacy source-driven deploys cannot remove a newer ownership/feed guard."""
    from botocore.exceptions import ClientError

    from .detection_activation import template_body
    from .fleet_api import aws_client
    from .storage_quarantine import require_unlocked

    cf = aws_client(session, "cloudformation")
    try:
        current = cf.describe_stacks(StackName=stack)["Stacks"][0]
    except ClientError as error:
        if error.response["Error"].get("Code") == "ValidationError" and "does not exist" in str(
            error
        ):
            return
        raise
    require_unlocked(session, current)
    template = template_body(cf, StackName=stack)
    require(
        not template.get("Metadata", {}).get(KEY)
        and not (
            {"DetectionFeedTable", "CaseTable", "LibraryTable"} & set(template.get("Resources", {}))
        ),
        "Operational engine storage requires guarded release plan/execute; "
        "legacy deployment is forbidden",
    )
