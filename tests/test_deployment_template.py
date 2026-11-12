import copy

from opencdr_deploy.deployment_template import materialize


def template():
    return {
        "Parameters": {"ManagementPrincipalArns": {"Default": "old"}, "Stage": {"Default": "dev"}},
        "Resources": {
            "Api": {
                "Type": "AWS::ApiGateway::RestApi",
                "Properties": {"Policy": {"Ref": "ManagementPrincipalArns"}},
            },
            "ApiDeployment123": {
                "Type": "AWS::ApiGateway::Deployment",
                "DependsOn": ["ApiMethod"],
                "Properties": {"RestApiId": {"Ref": "Api"}, "StageName": {"Ref": "Stage"}},
            },
            "ApiMethod": {
                "Type": "AWS::ApiGateway::Method",
                "Properties": {"RestApiId": {"Ref": "Api"}},
            },
            "Data": {"Type": "AWS::DynamoDB::Table", "DeletionPolicy": "Retain"},
        },
        "Outputs": {
            "Deployment": {"Value": {"Ref": "ApiDeployment123"}},
            "Attribute": {"Value": {"Fn::GetAtt": ["ApiDeployment123", "X"]}},
            "String": {"Value": {"Fn::Sub": "${ApiDeployment123}:${ApiDeployment123.X}"}},
        },
    }


def test_policy_change_redeploys_and_preserves_every_other_resource():
    source = template()
    original = copy.deepcopy(source)
    before = materialize(source, {"ManagementPrincipalArns": "old"})
    after = materialize(source, {"ManagementPrincipalArns": "old,new"})
    old_id = before["Outputs"]["Deployment"]["Value"]["Ref"]
    new_id = after["Outputs"]["Deployment"]["Value"]["Ref"]
    assert new_id != old_id and old_id not in after["Resources"]
    for name in ["Data", "Api", "ApiMethod"]:
        assert after["Resources"][name] == original["Resources"][name]
    assert after["Resources"][new_id] == original["Resources"]["ApiDeployment123"]
    assert after["Outputs"]["Attribute"]["Value"]["Fn::GetAtt"] == [new_id, "X"]
    assert after["Outputs"]["String"]["Value"]["Fn::Sub"] == "${" + new_id + "}:${" + new_id + ".X}"
    assert source == original


def test_retry_default_and_parameter_order_produce_identical_deployment():
    source = template()
    assert materialize(source, {}) == materialize(
        source, {"Stage": "dev", "ManagementPrincipalArns": "old"}
    )
    assert materialize(source, {"Stage": "dev", "ManagementPrincipalArns": "new"}) == materialize(
        source, {"ManagementPrincipalArns": "new", "Stage": "dev"}
    )


def test_runtime_or_route_release_change_creates_a_new_deployment():
    first = template()
    second = copy.deepcopy(first)
    second["Resources"]["ApiMethod"]["Properties"]["HttpMethod"] = "POST"
    assert materialize(first, {})["Outputs"] != materialize(second, {})["Outputs"]
