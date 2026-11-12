"""Deterministic installation of a pinned template with its reviewed parameters.

API Gateway resource-policy edits require a new stage deployment. CloudFormation
does not infer that requirement from a changed RestApi policy. The only template
transformation here renames Deployment resources and their references; all
resource properties remain pinned by the release.
"""

import hashlib
import json


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def materialize(template, parameters, *, binding=None):
    if binding is not None:
        from .storage_bindings import apply_binding

        template = apply_binding(template, binding, parameters=parameters)
    resolved = {
        key: str(parameters.get(key, spec.get("Default", "")))
        for key, spec in template.get("Parameters", {}).items()
    }
    revision = digest({"template": template, "parameters": resolved})
    replacements = {
        name: "OpencdrApiDeployment" + digest([name, revision])[:40]
        for name, resource in template.get("Resources", {}).items()
        if resource.get("Type") == "AWS::ApiGateway::Deployment"
    }
    if set(replacements.values()) & (set(template.get("Resources", {})) - set(replacements)):
        raise ValueError("API deployment logical ID collision")

    def replace(value):
        if isinstance(value, dict):
            return {replacements.get(k, k): replace(v) for k, v in value.items()}
        if isinstance(value, list):
            return [replace(v) for v in value]
        if isinstance(value, str):
            if value in replacements:
                return replacements[value]
            for before, after in replacements.items():
                value = value.replace("${" + before + "}", "${" + after + "}")
                value = value.replace("${" + before + ".", "${" + after + ".")
        return value

    return replace(template)
