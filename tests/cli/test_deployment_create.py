from pathlib import Path

import pytest

from prefect.testing.cli import invoke_and_assert

EXAMPLES = Path(__file__).parent.parent / "deployments" / "examples"

pytestmark = pytest.mark.skip(reason="Will revisit later as tech debt.")


@pytest.mark.parametrize(
    "path",
    [
        EXAMPLES / "single_deployment_with_flow.py",
        EXAMPLES / "single_deployment_with_flow_script.py",
    ],
)
def test_create_deployment_from_script(path: Path):
    invoke_and_assert(
        ["deployment", "create", str(path)],
        expected_output_contains=[
            "deployments from python script",
            "Created 1 deployment:",
        ],
    )


def test_create_deployment_from_script_with_invalid_deployment():
    path = EXAMPLES / "invalid_deployment.py"
    invoke_and_assert(
        ["deployment", "create", str(path)],
        expected_output_contains=[
            "deployments from python script",
            "1 invalid deployment",
        ],
        expected_code=1,
    )


@pytest.mark.parametrize(
    "path",
    [
        EXAMPLES / "multiple_invalid_deployments.py",
        EXAMPLES / "multiple_invalid_deployments.yaml",
    ],
)
def test_create_deployment_with_multiple_invalid_deployments(path):
    invoke_and_assert(
        ["deployment", "create", str(path)],
        expected_output_contains="2 invalid deployments",
        expected_code=1,
    )


@pytest.mark.parametrize(
    "path",
    [
        EXAMPLES / "mixed_valid_invalid_deployments.py",
        EXAMPLES / "mixed_valid_invalid_deployments.yaml",
    ],
)
def test_create_deployment_with_mixed_valid_invalid_deployments(path):
    invoke_and_assert(
        ["deployment", "create", str(path)],
        expected_output_contains="1 invalid deployment",
        expected_code=1,
    )


@pytest.mark.parametrize(
    "path",
    [EXAMPLES / "single_deployment.yaml"],
)
def test_create_deployment_from_yaml(path: Path):
    invoke_and_assert(
        ["deployment", "create", str(path)],
        expected_output_contains=[
            "deployments from yaml file",
            "flow from script",
            "Created 1 deployment:",
        ],
    )
