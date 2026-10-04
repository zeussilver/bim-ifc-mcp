"""Config-driven rule checks.

Thresholds live in a TOML file rather than in code, so a practice can tune them per
project (NCC, planning scheme, client brief) without touching Python. Each rule type
is a small function over the rows ifc_queries returns, so `evaluate` is pure and can
be tested without an IFC file; `run_rules` is the thin layer that reads the model.
"""

import math
import re
import tomllib
from pathlib import Path

import ifcopenshell

from bim_ifc_mcp import ifc_queries as q

DEFAULT_RULES = Path(__file__).with_name("default_rules.toml")
SEVERITIES = ("critical", "major", "minor", "info")
STATUSES = ("PASS", "FAIL", "UNKNOWN")
RULESET_KEYS = {"name", "description", "placeholder_values", "rules"}
COMMON_RULE_KEYS = {"id", "type", "title", "severity", "reference"}


def _has_text(value) -> bool:
    return value is not None and str(value).strip() != ""


def _is_placeholder(value, placeholders: frozenset[str]) -> bool:
    """Text an exporter left in a field instead of a real value, e.g. Revit's literal "Fire Rating"."""
    return _has_text(value) and str(value).strip().lower() in placeholders


def _door_result(door: dict, status: str, message: str) -> dict:
    return {"ifc_class": "IfcDoor", "global_id": door.get("global_id"), "element": door.get("mark"),
            "element_name": door.get("name"), "storey": door.get("storey"), "status": status, "message": message}


def _space_result(space: dict, status: str, message: str) -> dict:
    return {"ifc_class": "IfcSpace", "global_id": space.get("global_id"), "element": space.get("number"),
            "element_name": space.get("name"), "storey": space.get("storey"), "status": status, "message": message}


def _door_min_width(rule: dict, spaces: list[dict], doors: list[dict], placeholders: frozenset[str]):
    for door in doors:
        yield _door_result(door, *q.door_width_status(door.get("width_mm"), rule["min_width_mm"]))


def _space_min_area(rule: dict, spaces: list[dict], doors: list[dict], placeholders: frozenset[str]):
    pattern = re.compile(rule["name_pattern"], re.IGNORECASE) if rule.get("name_pattern") else None
    minimum = rule["min_area_m2"]
    for space in spaces:
        if pattern and not any(pattern.search(space.get(key) or "") for key in ("number", "name")):
            continue
        area = space.get("area_m2")
        if area is None:
            status, message = "UNKNOWN", "No area quantity or geometry"
        elif area < minimum:
            status, message = "FAIL", f"{area} m2 < {minimum} m2 (short by {round(minimum - area, 2)} m2)"
        else:
            status, message = "PASS", f"{area} m2 >= {minimum} m2"
        if area is not None and (space.get("area_source") or "").startswith("geometry"):
            message += " [area from geometry]"
        yield _space_result(space, status, message)


def _fire_door_self_closing(rule: dict, spaces: list[dict], doors: list[dict], placeholders: frozenset[str]):
    for door in doors:
        rating = door.get("fire_rating")
        if not _has_text(rating):
            continue
        if _is_placeholder(rating, placeholders):
            yield _door_result(door, "UNKNOWN",
                               f'FireRating holds placeholder "{rating}"; confirm whether this door is fire-rated')
            continue
        closing = door.get("self_closing")
        if closing is True:
            yield _door_result(door, "PASS", f"Fire-rated door ({rating}) is self-closing")
        elif closing is False:
            yield _door_result(door, "FAIL", f"Fire-rated door ({rating}) is not self-closing")
        else:
            yield _door_result(door, "UNKNOWN", "SelfClosing not recorded")


def _door_in_fire_wall_rated(rule: dict, spaces: list[dict], doors: list[dict], placeholders: frozenset[str]):
    for door in doors:
        wall_rating = door.get("host_wall_fire_rating")
        if not _has_text(wall_rating):
            continue
        if _is_placeholder(wall_rating, placeholders):
            yield _door_result(door, "UNKNOWN",
                               f'Host wall FireRating holds placeholder "{wall_rating}"; confirm whether the wall is fire-rated')
        elif _is_placeholder(door.get("fire_rating"), placeholders):
            yield _door_result(door, "UNKNOWN", f'Door sits in fire-rated wall ({wall_rating}) but its FireRating '
                                                f'holds placeholder "{door["fire_rating"]}"')
        elif _has_text(door.get("fire_rating")):
            yield _door_result(door, "PASS", f"Door rated {door['fire_rating']} in fire-rated wall ({wall_rating})")
        else:
            yield _door_result(door, "FAIL", f"Door sits in fire-rated wall ({wall_rating}) but has no FireRating")


# type -> (check function, required numeric parameters, optional parameters)
RULE_TYPES = {
    "door_min_width": (_door_min_width, ("min_width_mm",), ()),
    "space_min_area": (_space_min_area, ("min_area_m2",), ("name_pattern",)),
    "fire_door_self_closing": (_fire_door_self_closing, (), ()),
    "door_in_fire_wall_rated": (_door_in_fire_wall_rated, (), ()),
}


def _validate(data: dict) -> dict:
    """Check a parsed rule set and fill defaults; raises ValueError naming the rule and problem.

    Unknown keys are errors, not ignored: a typo such as `name_patern` would otherwise
    silently widen a rule to every room.
    """
    unknown = set(data) - RULESET_KEYS
    if unknown:
        raise ValueError(f"unknown top-level key(s) {', '.join(sorted(unknown))} (allowed: {', '.join(sorted(RULESET_KEYS))})")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("rule set needs a top-level 'name' string")
    description = data.get("description")
    if description is not None and not isinstance(description, str):
        raise ValueError("'description' must be a string")
    placeholders = data.get("placeholder_values", [])
    if not isinstance(placeholders, list) or not all(isinstance(v, str) for v in placeholders):
        raise ValueError("'placeholder_values' must be a list of strings")
    rules = data.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ValueError("rule set needs at least one [[rules]] entry")

    seen, out = set(), []
    for index, rule in enumerate(rules, 1):
        if not isinstance(rule, dict):
            raise ValueError(f"rule #{index} must be a table")
        rule_id = rule.get("id")
        if not isinstance(rule_id, str) or not rule_id.strip():
            raise ValueError(f"rule #{index} has no 'id'")
        if rule_id in seen:
            raise ValueError(f"rule {rule_id}: duplicate id")
        seen.add(rule_id)
        rule_type = rule.get("type")
        if rule_type not in RULE_TYPES:
            raise ValueError(f"rule {rule_id}: unknown type {rule_type!r} (known: {', '.join(RULE_TYPES)})")
        _, required, optional = RULE_TYPES[rule_type]
        unknown = set(rule) - COMMON_RULE_KEYS - set(required) - set(optional)
        if unknown:
            allowed = sorted(COMMON_RULE_KEYS | set(required) | set(optional))
            raise ValueError(f"rule {rule_id}: unknown key(s) {', '.join(sorted(unknown))} (allowed: {', '.join(allowed)})")
        if not isinstance(rule.get("title"), str) or not rule["title"].strip():
            raise ValueError(f"rule {rule_id}: missing 'title'")
        severity = rule.get("severity", "major")
        if severity not in SEVERITIES:
            raise ValueError(f"rule {rule_id}: severity {severity!r} must be one of {', '.join(SEVERITIES)}")
        reference = rule.get("reference")
        if reference is not None and not isinstance(reference, str):
            raise ValueError(f"rule {rule_id}: 'reference' must be a string")
        for param in required:
            value = rule.get(param)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"rule {rule_id}: '{param}' must be a number, got {value!r}")
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"rule {rule_id}: '{param}' must be a positive number, got {value!r}")
        pattern = rule.get("name_pattern")
        if pattern is not None:
            if not isinstance(pattern, str):
                raise ValueError(f"rule {rule_id}: 'name_pattern' must be a string")
            try:
                re.compile(pattern)
            except re.error as e:
                raise ValueError(f"rule {rule_id}: invalid name_pattern {pattern!r}: {e}") from e
        out.append({**rule, "severity": severity, "reference": reference})
    return {"name": name, "description": description, "placeholder_values": placeholders, "rules": out}


def load_rules(path: str | Path | None = None) -> dict:
    """Load and validate a TOML rule set; no path means the packaged default_rules.toml."""
    p = Path(path).expanduser() if path else DEFAULT_RULES
    try:
        with p.open("rb") as f:
            return _validate(tomllib.load(f))
    except ValueError as e:  # includes tomllib.TOMLDecodeError
        raise ValueError(f"Invalid rules file {p}: {e}") from e


def _count(results: list[dict]) -> dict:
    return {s: sum(r["status"] == s for r in results) for s in STATUSES}


def evaluate(config: dict, *, spaces: list[dict], doors: list[dict]) -> dict:
    """Run every rule over list_spaces / door_schedule rows. Issues are FAIL + UNKNOWN only."""
    config = _validate(config)
    placeholders = frozenset(v.strip().lower() for v in config["placeholder_values"])
    rule_reports, all_results, issues = [], [], []
    for rule in config["rules"]:
        check = RULE_TYPES[rule["type"]][0]
        results = sorted(check(rule, spaces, doors, placeholders), key=lambda r: (r["storey"] or "", r["element"] or ""))
        all_results += results
        # checked == 0 is kept visible: it usually means name_pattern matched nothing.
        rule_reports.append({
            "id": rule["id"],
            "title": rule["title"],
            "type": rule["type"],
            "severity": rule["severity"],
            "reference": rule["reference"],
            "checked": len(results),
            "summary": _count(results),
        })
        issues += [{
            "rule_id": rule["id"],
            "rule_title": rule["title"],
            "severity": rule["severity"],
            "status": r["status"],
            "ifc_class": r["ifc_class"],
            "global_id": r["global_id"],
            "element": r["element"],
            "element_name": r["element_name"],
            "storey": r["storey"],
            "message": r["message"],
            "reference": rule["reference"],
        } for r in results if r["status"] != "PASS"]

    return {
        "ruleset": config["name"],
        "description": config["description"],
        "summary": _count(all_results),
        "rules": rule_reports,
        "issues": issues,
    }


def run_rules(model: ifcopenshell.file, config: dict) -> dict:
    """Evaluate a rule set (from load_rules) against an opened IFC model."""
    return evaluate(config, spaces=q.list_spaces(model), doors=q.door_schedule(model))
