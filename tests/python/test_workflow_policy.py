"""Public jobs use disposable hosted runners, regardless of repository variables.

This prevents accidental routing regressions in the committed workflows. It is
not a sandbox for a contributor who edits a workflow: repository-scoped private
runners must be unregistered before making the repository public.
"""

import ast
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = sorted((ROOT / ".github/workflows").glob("*.yml"))


def runner_expressions(path):
    expressions = re.findall(r"^\s+runs-on:\s*(.+)$", path.read_text(), re.MULTILINE)
    assert expressions, f"{path.name} has no runner policy"
    return expressions


def evaluate(expression, *, private, variables):
    """Evaluate only the expression subset used for runner selection.

    Reading the actual expression catches inverted conditions, eager variable
    parsing, and fallback mistakes. Unsupported syntax fails closed in this
    regression check; arbitrary Python or workflow code is never executed.
    """
    assert expression.startswith("${{") and expression.endswith("}}")
    source = expression[3:-2].strip().replace("&&", " and ").replace("||", " or ")
    context = {"github.event.repository.private": private, **variables}

    def visit(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, bool)):
            return node.value
        if isinstance(node, (ast.Name, ast.Attribute)):
            name = ast.unparse(node)
            assert name.startswith("vars.") or name == "github.event.repository.private"
            return context.get(name, "")
        if isinstance(node, ast.BoolOp):
            values = iter(node.values)
            result = visit(next(values))
            for value in values:
                if (isinstance(node.op, ast.And) and not result) or (
                    isinstance(node.op, ast.Or) and result
                ):
                    return result
                result = visit(value)
            return result
        if isinstance(node, ast.Call):
            assert isinstance(node.func, ast.Name) and node.func.id == "fromJSON"
            assert len(node.args) == 1 and not node.keywords
            return json.loads(visit(node.args[0]))
        raise AssertionError(f"Unsupported runner expression: {type(node).__name__}")

    return visit(ast.parse(source, mode="eval").body)


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda path: path.name)
@pytest.mark.parametrize(
    "variables",
    [
        {},
        {
            "vars.MEERKAT_COMPUTER_RUNNER": '["self-hosted","private-general"]',
            "vars.MEERKAT_COMPUTER_RUST_RUNNER": '["self-hosted","private-rust"]',
        },
        {
            "vars.MEERKAT_COMPUTER_RUNNER": '"self-hosted"',
            "vars.MEERKAT_COMPUTER_RUST_RUNNER": "not valid JSON",
        },
    ],
    ids=["unset", "private-labels", "malformed-private-routing"],
)
def test_public_workflows_always_use_hosted_runner(path, variables):
    for expression in runner_expressions(path):
        assert evaluate(expression, private=False, variables=variables) == "ubuntu-24.04"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda path: path.name)
def test_private_workflows_retain_explicit_runner_routing(path):
    for expression in runner_expressions(path):
        default = evaluate(expression, private=True, variables={})
        assert isinstance(default, list) and "self-hosted" in default
        configured = evaluate(
            expression,
            private=True,
            variables={
                "vars.MEERKAT_COMPUTER_RUNNER": '["self-hosted","private-general"]',
                "vars.MEERKAT_COMPUTER_RUST_RUNNER": '["self-hosted","private-rust"]',
            },
        )
        expected = "private-rust" if "RUST_RUNNER" in expression else "private-general"
        assert configured == ["self-hosted", expected]


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda path: path.name)
def test_workflows_have_no_privileged_pull_request_path(path):
    source = path.read_text()
    assert not re.search(r"\b(?:pull_request_target|workflow_run)\s*:", source)
    assert not re.search(r"\bsecrets\s*[:.]", source)
    assert re.findall(r"^\s*permissions:\s*\n((?:[ \t]+[^\n]+\n)+)", source, re.MULTILINE) == [
        "  contents: read\n"
    ]
    checkouts = re.findall(r"uses: actions/checkout@", source)
    assert checkouts
    protected = re.findall(
        r"uses: actions/checkout@[^\n]+\n\s+with:\n\s+persist-credentials: false\n",
        source,
    )
    assert len(protected) == len(checkouts)
