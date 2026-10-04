"""End-to-end through the MCP protocol, using an in-process client."""

import json
from pathlib import Path

import pytest
from mcp import Client

from bim_ifc_mcp.server import mcp

SAMPLE = str(Path(__file__).resolve().parent.parent / "models" / "sample_house.ifc")

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def test_tools_are_listed():
    async with Client(mcp) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert names == {
        "model_summary", "list_spaces", "door_schedule", "check_door_widths", "run_rules", "export_issues",
    }


async def test_only_export_writes():
    async with Client(mcp) as client:
        tools = {t.name: t.annotations for t in (await client.list_tools()).tools}
    assert all(a.read_only_hint for name, a in tools.items() if name != "export_issues")
    export = tools["export_issues"]
    assert (export.read_only_hint, export.destructive_hint, export.idempotent_hint) == (False, False, True)


async def test_check_door_widths_over_mcp():
    async with Client(mcp) as client:
        result = await client.call_tool("check_door_widths", {"ifc_path": SAMPLE, "min_width_mm": 850})
    assert not result.is_error
    assert json.loads(result.content[0].text)["summary"] == {"PASS": 2, "FAIL": 2, "UNKNOWN": 1}


@pytest.mark.parametrize("tool", ["model_summary", "run_rules", "export_issues"])
async def test_bad_path_is_reported_as_tool_error(tmp_path, tool):
    out = tmp_path / "issues.csv"
    args = {"out_path": str(out), "format": "csv"} if tool == "export_issues" else {}
    async with Client(mcp) as client:
        result = await client.call_tool(tool, {"ifc_path": "/nope.ifc", **args})
    assert result.is_error
    assert "IFC file not found: /nope.ifc" in result.content[0].text  # not a bare "Error executing tool"
    assert not out.exists()


@pytest.mark.parametrize("content", ["", "not a STEP file\n"])
async def test_non_ifc_file_is_reported_as_tool_error(tmp_path, content):
    path = tmp_path / "notes.ifc"
    path.write_text(content)
    async with Client(mcp) as client:
        result = await client.call_tool("model_summary", {"ifc_path": str(path)})
    assert result.is_error
    assert f"Could not read {path} as IFC" in result.content[0].text


async def test_export_write_failure_is_reported_to_the_model(tmp_path):
    out = tmp_path / "issues.csv"
    out.mkdir()  # a directory where the file should go
    async with Client(mcp) as client:
        result = await client.call_tool("export_issues", {"ifc_path": SAMPLE, "out_path": str(out), "format": "csv"})
    assert result.is_error
    assert f"Could not write {out}" in result.content[0].text


@pytest.mark.parametrize(("fmt", "name"), [("csv", "issues.bcf"), ("bcf", "issues.csv"), ("bcf", "issues")])
async def test_export_refuses_suffix_that_does_not_match_format(tmp_path, fmt, name):
    out = tmp_path / name
    async with Client(mcp) as client:
        result = await client.call_tool("export_issues", {"ifc_path": SAMPLE, "out_path": str(out), "format": fmt})
    assert result.is_error
    assert "must end in" in result.content[0].text  # the model sees why, not just "error"
    assert not out.exists()


async def test_invalid_rules_file_is_reported_to_the_model(tmp_path):
    rules = tmp_path / "rules.toml"
    rules.write_text('name = "Bad"\n[[rules]]\nid = "R1"\ntype = "door_min_width"\ntitle = "x"\n')
    async with Client(mcp) as client:
        result = await client.call_tool("run_rules", {"ifc_path": SAMPLE, "rules_path": str(rules)})
    assert result.is_error
    assert "rule R1: 'min_width_mm' must be a number" in result.content[0].text


async def test_issue_report_prompt():
    async with Client(mcp) as client:
        prompts = {p.name for p in (await client.list_prompts()).prompts}
        default = await client.get_prompt("issue_report", {"ifc_path": SAMPLE})
        custom = await client.get_prompt("issue_report", {"ifc_path": SAMPLE, "rules_path": "/p/rules.toml"})
    assert prompts == {"door_issue_list", "issue_report"}
    assert "run_rules" in default.messages[0].content.text
    assert "default rule set" in default.messages[0].content.text
    assert "rules_path=/p/rules.toml" in custom.messages[0].content.text
