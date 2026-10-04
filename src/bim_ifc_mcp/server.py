"""MCP server: exposes the deterministic IFC functions as tools.

Division of labour ("model proposes, code executes"):
  - tools  -> read the IFC file and compute every number and PASS/FAIL
  - LLM    -> chooses which tool to call, explains results, drafts issue lists
  - human  -> reviews the draft before anything leaves the studio
"""

from pathlib import Path
from typing import Literal

import ifcopenshell
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from bim_ifc_mcp import ifc_queries as q
from bim_ifc_mcp import rules as r
from bim_ifc_mcp.bcf import issues_to_bcf
from bim_ifc_mcp.export import issues_to_csv

READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
# Writes (or overwrites) one export file and touches nothing else.
WRITES_FILE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
EXPORT_SUFFIXES = {"csv": (".csv",), "bcf": (".bcf", ".bcfzip")}

mcp = MCPServer(
    name="bim-ifc",
    instructions=(
        "Tools for reading IFC (BIM) models. All quantities and rule results come from the tools; "
        "never estimate or recompute them yourself. When a result is UNKNOWN, say the model data is "
        "missing rather than guessing. Flag that outputs need review by a qualified person."
    ),
)


def _open(ifc_path: str) -> ifcopenshell.file:
    # ToolError, not a crash: other exceptions reach the model as a bare "Error executing tool",
    # and it should be able to tell the user the file is missing or is not IFC.
    try:
        return q.open_model(ifc_path)
    except FileNotFoundError as e:
        raise ToolError(str(e)) from e
    except (OSError, ifcopenshell.Error) as e:
        raise ToolError(f"Could not read {ifc_path} as IFC: {e}") from e


@mcp.tool(annotations=READ_ONLY)
def model_summary(ifc_path: str) -> dict:
    """Overview of an IFC model: schema, project name, storeys and element counts.

    Args:
        ifc_path: Absolute path to a .ifc file.
    """
    return q.model_summary(_open(ifc_path))


@mcp.tool(annotations=READ_ONLY)
def list_spaces(ifc_path: str) -> list[dict]:
    """List rooms (IfcSpace) with number, name, storey and floor area in m2.

    area_source says where area_m2 came from: a quantity such as
    "Qto_SpaceBaseQuantities.NetFloorArea", or "geometry:footprint" when the model has no
    area quantity (common in Revit exports) and the area was computed from the space shape.
    Geometry areas are gross footprints for screening; flag them for review.
    area_m2 is null when neither a quantity nor usable geometry exists.

    Args:
        ifc_path: Absolute path to a .ifc file.
    """
    return q.list_spaces(_open(ifc_path))


@mcp.tool(annotations=READ_ONLY)
def door_schedule(ifc_path: str) -> list[dict]:
    """Door schedule: mark, type name, storey, width/height in mm, fire rating, self-closing,
    external flag, and the fire rating of the wall the door sits in (host_wall_fire_rating).

    mark is the Revit Mark parameter when the export has one, else IfcDoor.Tag; tag is the raw
    IfcDoor.Tag (the Revit ElementId in Revit exports).

    Args:
        ifc_path: Absolute path to a .ifc file.
    """
    return q.door_schedule(_open(ifc_path))


@mcp.tool(annotations=READ_ONLY)
def check_door_widths(ifc_path: str, min_width_mm: float = 850) -> dict:
    """Check every door width against a minimum (default 850 mm, the AS 1428.1 clear-opening figure).

    Returns PASS / FAIL / UNKNOWN per door plus a summary. Measures IfcDoor.OverallWidth,
    the door's overall (nominal) width, never less than the clear opening: a FAIL is definite,
    a PASS only means the door is not ruled out. A screening check, not a compliance certificate.

    Args:
        ifc_path: Absolute path to a .ifc file.
        min_width_mm: Minimum acceptable width in millimetres.
    """
    return q.check_door_widths(_open(ifc_path), min_width_mm)


def _load_rules(rules_path: str | None) -> dict:
    # ToolError, not a crash: the model should see which rule or file is wrong.
    try:
        return r.load_rules(rules_path)
    except (FileNotFoundError, ValueError) as e:
        raise ToolError(str(e)) from e


@mcp.tool(annotations=READ_ONLY)
def run_rules(ifc_path: str, rules_path: str | None = None) -> dict:
    """Run a configurable rule set (door width, room area, fire doors) and list the issues.

    Returns a summary, per-rule counts and the FAIL / UNKNOWN issues. A rule with
    checked = 0 matched no element (e.g. its room-name pattern did not fit the model's
    naming), which is not the same as passing. Thresholds in the default rule set are
    examples for screening, not project requirements.

    Args:
        ifc_path: Absolute path to a .ifc file.
        rules_path: Absolute path to a TOML rule set; omit to use the packaged default rules.
    """
    return r.run_rules(_open(ifc_path), _load_rules(rules_path))


@mcp.tool(annotations=WRITES_FILE)
def export_issues(
    ifc_path: str, out_path: str, format: Literal["csv", "bcf"], rules_path: str | None = None
) -> dict:
    """Run the rule set and write its issues to a CSV (Excel) or BCF 2.1 (BIMcollab, Solibri, Revit add-ins) file.

    Overwrites out_path if it exists. BCF topics have stable GUIDs, so re-exporting after a
    model update updates the same topics instead of duplicating them.

    Args:
        ifc_path: Absolute path to a .ifc file.
        out_path: Absolute path of the file to write; must end in .csv for csv, .bcf or .bcfzip for bcf.
        format: "csv" or "bcf".
        rules_path: Absolute path to a TOML rule set; omit to use the packaged default rules.
    """
    out = Path(out_path).expanduser()
    suffixes = EXPORT_SUFFIXES[format]
    if out.suffix.lower() not in suffixes:
        raise ToolError(f"out_path for format {format!r} must end in {' or '.join(suffixes)}, got: {out.name}")
    model = _open(ifc_path)
    report = r.run_rules(model, _load_rules(rules_path))
    try:
        if format == "csv":
            path = issues_to_csv(report["issues"], out)
        else:
            path = issues_to_bcf(report["issues"], out, model=model, ifc_filename=Path(ifc_path).name)
    except (OSError, ValueError) as e:  # e.g. unwritable location, or duplicate GlobalIds for BCF
        raise ToolError(f"Could not write {out}: {e}") from e
    return {
        "path": str(path.resolve()),
        "format": format,
        "issue_count": len(report["issues"]),
        "summary": report["summary"],
    }


@mcp.prompt()
def door_issue_list(ifc_path: str) -> str:
    """Run the door width check and draft an issue list for human review."""
    return (
        f"Call check_door_widths on {ifc_path}. Using only the returned data, draft an issue list "
        "as a table: Door mark | Storey | Width | Problem | Suggested action. Include FAIL and UNKNOWN "
        "doors only. Then add a short 'Reviewer notes' section listing the assumptions (OverallWidth "
        "vs clear opening, which standard applies) that an architect must confirm before issuing."
    )


@mcp.prompt()
def issue_report(ifc_path: str, rules_path: str = "") -> str:
    """Run the configured rule set and draft an issue report for human review."""
    rules = f"rules_path={rules_path}" if rules_path else "the default rule set"
    return (
        f"Call run_rules on {ifc_path} with {rules}. Using only the returned data (never invent or "
        "recompute numbers), draft an issue report as a table: Rule | Severity | Element | Storey | "
        "Status | Problem | Suggested action. Include every issue; for UNKNOWN say which model data is "
        "missing. List rules with checked = 0 separately: they matched nothing, which is not a pass. "
        "Then add a short 'Reviewer notes' section listing what an architect must confirm before issuing: "
        "the thresholds and references against the project's NCC / AS / planning requirements, door "
        "widths measured as OverallWidth rather than clear opening, and areas marked [area from geometry] "
        "being computed gross footprints. Finally offer to export the issues with export_issues "
        "as CSV or BCF."
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
