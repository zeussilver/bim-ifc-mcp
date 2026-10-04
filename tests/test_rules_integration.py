"""Default rule set end to end on both fixtures: rules engine, CSV and BCF export over MCP.

The IFC4 file carries area quantities, the IFC2X3 one (Revit-like) does not, so the same
six issues must come out whichever schema and area source the model uses.
"""

import csv
import json
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest
from mcp import Client

from bim_ifc_mcp import ifc_queries as q
from bim_ifc_mcp import rules as r
from bim_ifc_mcp.export import CSV_COLUMNS
from bim_ifc_mcp.server import mcp

MODELS = Path(__file__).resolve().parent.parent / "models"
FIXTURES = [MODELS / "sample_house.ifc", MODELS / "sample_house_ifc2x3.ifc"]

EXPECTED_ISSUES = [
    ("DOOR-WIDTH-01", "D03", "FAIL"),
    ("DOOR-WIDTH-01", "D04", "FAIL"),
    ("DOOR-WIDTH-01", "D05", "UNKNOWN"),
    ("SPACE-AREA-01", "102", "FAIL"),
    ("FIRE-DOOR-01", "D04", "FAIL"),
    ("FIRE-DOOR-02", "D03", "FAIL"),
]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(params=FIXTURES, ids=["IFC4", "IFC2X3"])
def ifc_path(request) -> str:
    return str(request.param)


def triples(issues: list[dict]) -> list[tuple]:
    return [(i["rule_id"], i["element"], i["status"]) for i in issues]


async def call(name: str, args: dict):
    async with Client(mcp) as client:
        result = await client.call_tool(name, args)
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


def test_default_rules_on_fixture(ifc_path):
    report = r.run_rules(q.open_model(ifc_path), r.load_rules())
    assert triples(report["issues"]) == EXPECTED_ISSUES
    assert report["summary"] == {"PASS": 3, "FAIL": 5, "UNKNOWN": 1}
    by_id = {rule["id"]: rule for rule in report["rules"]}
    assert by_id["SPACE-AREA-01"]["checked"] == 2  # 101 Bedroom 1 PASS, 102 Bedroom 2 FAIL
    assert by_id["SPACE-AREA-01"]["summary"] == {"PASS": 1, "FAIL": 1, "UNKNOWN": 0}

    issues = {(i["rule_id"], i["element"]): i for i in report["issues"]}
    assert issues["SPACE-AREA-01", "102"]["message"] == "8.4 m2 < 9.0 m2 (short by 0.6 m2) [area from geometry]"
    assert issues["FIRE-DOOR-02", "D03"]["message"] == (
        "Door sits in fire-rated wall (FRL 60/60/60) but has no FireRating"
    )
    assert issues["FIRE-DOOR-01", "D04"]["severity"] == "critical"


def test_custom_rules_file(ifc_path, tmp_path):
    # A German-style name pattern matches no room: checked == 0 stays visible instead of looking like a pass.
    rules = tmp_path / "rules.toml"
    rules.write_text(
        'name = "Custom"\n'
        "[[rules]]\n"
        'id = "SCHLAF-01"\ntype = "space_min_area"\ntitle = "Schlafzimmer >= 9 m2"\n'
        'min_area_m2 = 9.0\nname_pattern = "schlaf"\n'
        "[[rules]]\n"
        'id = "DOOR-800"\ntype = "door_min_width"\ntitle = "Door width >= 800 mm"\nmin_width_mm = 800\n',
        encoding="utf-8",
    )
    report = r.run_rules(q.open_model(ifc_path), r.load_rules(rules))
    assert report["ruleset"] == "Custom"
    assert [(rule["id"], rule["checked"]) for rule in report["rules"]] == [("SCHLAF-01", 0), ("DOOR-800", 5)]
    assert triples(report["issues"]) == [("DOOR-800", "D04", "FAIL"), ("DOOR-800", "D05", "UNKNOWN")]


@pytest.mark.anyio
async def test_run_rules_over_mcp(ifc_path):
    report = await call("run_rules", {"ifc_path": ifc_path})
    assert triples(report["issues"]) == EXPECTED_ISSUES


@pytest.mark.anyio
async def test_export_csv_over_mcp(ifc_path, tmp_path):
    out = tmp_path / "exports" / "issues.csv"
    result = await call("export_issues", {"ifc_path": ifc_path, "out_path": str(out), "format": "csv"})
    assert result == {"path": str(out.resolve()), "format": "csv", "issue_count": 6,
                      "summary": {"PASS": 3, "FAIL": 5, "UNKNOWN": 1}}

    assert out.read_bytes().startswith(b"\xef\xbb\xbf")  # BOM so Excel reads UTF-8
    with out.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert list(rows[0]) == CSV_COLUMNS
    assert [(row["rule_id"], row["element"], row["status"]) for row in rows] == EXPECTED_ISSUES
    assert rows[3]["ifc_class"] == "IfcSpace" and rows[3]["storey"] == "Level 1"


@pytest.mark.anyio
async def test_export_bcf_over_mcp(ifc_path, tmp_path):
    out = tmp_path / "issues.bcf"
    result = await call("export_issues", {"ifc_path": ifc_path, "out_path": str(out), "format": "bcf"})
    assert result["path"] == str(out.resolve())
    assert result["issue_count"] == 6

    report = r.run_rules(q.open_model(ifc_path), r.load_rules())
    model = q.open_model(ifc_path)
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        assert names[:2] == ["bcf.version", "project.bcfp"]
        topics = sorted({name.split("/")[0] for name in names if "/" in name})
        assert len(topics) == 6
        selected, titles = set(), set()
        for topic in topics:
            markup = ET.fromstring(zf.read(f"{topic}/markup.bcf"))
            visinfo = ET.fromstring(zf.read(f"{topic}/viewpoint.bcfv"))
            file_el = markup.find("Header/File")
            assert file_el.get("IfcProject") == model.by_type("IfcProject")[0].GlobalId
            assert file_el.findtext("Filename") == Path(ifc_path).name
            assert markup.find("Topic").get("Guid") == topic
            titles.add(markup.findtext("Topic/Title"))
            selected.add(visinfo.find("Components/Selection/Component").get("IfcGuid"))
            # Every fixture door and space has a placement, so every topic gets a camera.
            assert visinfo.find("PerspectiveCamera") is not None
    assert selected == {i["global_id"] for i in report["issues"]}
    assert "SPACE-AREA-01 102: 8.4 m2 < 9.0 m2 (short by 0.6 m2) [area from geometry]" in titles
