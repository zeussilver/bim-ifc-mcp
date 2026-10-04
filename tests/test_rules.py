"""Unit tests for the rules engine and CSV export on hand-written rows (no IFC file needed)."""

import csv
from importlib import resources

import pytest

from bim_ifc_mcp import rules as r
from bim_ifc_mcp.export import CSV_COLUMNS, issues_to_csv

ISSUE_KEYS = ["rule_id", "rule_title", "severity", "status", "ifc_class", "global_id", "element",
              "element_name", "storey", "message", "reference"]


def door(mark, storey, width_mm, fire_rating=None, self_closing=None, host_wall_fire_rating=None):
    return {"global_id": f"guid-{mark}", "mark": mark, "name": f"Door {mark}", "type_name": None,
            "storey": storey, "width_mm": width_mm, "height_mm": 2040.0, "fire_rating": fire_rating,
            "self_closing": self_closing, "fire_exit": None, "is_external": None,
            "host_wall_fire_rating": host_wall_fire_rating}


def space(number, name, storey, area_m2, area_source="Qto_SpaceBaseQuantities.NetFloorArea"):
    return {"global_id": f"guid-{number}", "number": number, "name": name, "storey": storey,
            "area_m2": area_m2, "area_source": area_source if area_m2 is not None else None}


# Listed out of order on purpose: evaluate must sort by storey, then element.
DOORS = [
    door("D05", "Level 1", None),
    door("D04", "Level 1", 800.0, fire_rating="FRL -/30/30", self_closing=False),
    door("D01", "Ground Floor", 920.0),
    door("D02", "Ground Floor", 870.0),
    door("D03", "Ground Floor", 820.0, host_wall_fire_rating="FRL 60/60/60"),
]
SPACES = [
    space("G01", "Living", "Ground Floor", 32.5),
    space("G02", "Kitchen", "Ground Floor", 14.2),
    space("G03", "Accessible WC", "Ground Floor", 4.8),
    space("101", "Bedroom 1", "Level 1", 15.0),
    space("102", "Bedroom 2", "Level 1", 8.4, area_source="geometry:footprint"),
]


def config(*rules, name="Test rules"):
    return {"name": name, "rules": list(rules)}


@pytest.fixture(scope="module")
def defaults():
    return r.load_rules()


def test_default_rules_are_packaged():
    assert resources.files("bim_ifc_mcp").joinpath("default_rules.toml").is_file()


def test_load_default_rules(defaults):
    assert defaults["name"]
    assert [(x["id"], x["type"]) for x in defaults["rules"]] == [
        ("DOOR-WIDTH-01", "door_min_width"),
        ("SPACE-AREA-01", "space_min_area"),
        ("FIRE-DOOR-01", "fire_door_self_closing"),
        ("FIRE-DOOR-02", "door_in_fire_wall_rated"),
    ]
    by_id = {x["id"]: x for x in defaults["rules"]}
    assert by_id["DOOR-WIDTH-01"]["min_width_mm"] == 850
    assert by_id["SPACE-AREA-01"]["min_area_m2"] == 9.0
    assert by_id["SPACE-AREA-01"]["name_pattern"] == "bed"
    assert all(x["severity"] in r.SEVERITIES and x["reference"] for x in defaults["rules"])


def test_empty_path_means_default_rules(defaults):
    assert r.load_rules("") == defaults


def test_default_rules_on_sample_rows(defaults):
    report = r.evaluate(defaults, spaces=SPACES, doors=DOORS)
    found = [(i["rule_id"], i["element"], i["status"]) for i in report["issues"]]
    assert found == [
        ("DOOR-WIDTH-01", "D03", "FAIL"),
        ("DOOR-WIDTH-01", "D04", "FAIL"),
        ("DOOR-WIDTH-01", "D05", "UNKNOWN"),
        ("SPACE-AREA-01", "102", "FAIL"),
        ("FIRE-DOOR-01", "D04", "FAIL"),
        ("FIRE-DOOR-02", "D03", "FAIL"),
    ]
    assert report["summary"] == {"PASS": 3, "FAIL": 5, "UNKNOWN": 1}
    rules = {x["id"]: x for x in report["rules"]}
    assert rules["DOOR-WIDTH-01"]["checked"] == 5
    assert rules["SPACE-AREA-01"]["checked"] == 2  # only bedrooms
    assert rules["SPACE-AREA-01"]["summary"] == {"PASS": 1, "FAIL": 1, "UNKNOWN": 0}
    assert rules["FIRE-DOOR-01"]["checked"] == 1
    assert rules["FIRE-DOOR-02"]["checked"] == 1
    assert report["ruleset"] == defaults["name"]
    assert report["description"] == defaults["description"]


def test_issue_dict_contract(defaults):
    issue = r.evaluate(defaults, spaces=SPACES, doors=DOORS)["issues"][0]
    assert list(issue) == ISSUE_KEYS
    assert issue == {
        "rule_id": "DOOR-WIDTH-01",
        "rule_title": "Door width >= 850 mm",
        "severity": "major",
        "status": "FAIL",
        "ifc_class": "IfcDoor",
        "global_id": "guid-D03",
        "element": "D03",
        "element_name": "Door D03",
        "storey": "Ground Floor",
        "message": "820.0 mm < 850 mm (short by 30.0 mm)",
        "reference": issue["reference"],
    }


def test_space_min_area_messages():
    rule = {"id": "A", "type": "space_min_area", "title": "Bedrooms", "min_area_m2": 9.0, "name_pattern": "BED"}
    spaces = SPACES + [space("103", "Bed 3", "Level 1", None)]
    report = r.evaluate(config(rule), spaces=spaces, doors=[])
    messages = {i["element"]: (i["status"], i["message"], i["ifc_class"]) for i in report["issues"]}
    assert messages == {
        "102": ("FAIL", "8.4 m2 < 9.0 m2 (short by 0.6 m2) [area from geometry]", "IfcSpace"),
        "103": ("UNKNOWN", "No area quantity or geometry", "IfcSpace"),
    }
    assert report["rules"][0]["summary"] == {"PASS": 1, "FAIL": 1, "UNKNOWN": 1}


def test_space_pattern_matches_number_and_absent_pattern_checks_all():
    by_number = {"id": "A", "type": "space_min_area", "title": "Ground", "min_area_m2": 10, "name_pattern": "^g"}
    all_spaces = {"id": "B", "type": "space_min_area", "title": "All", "min_area_m2": 10}
    rules = {x["id"]: x for x in r.evaluate(config(by_number, all_spaces), spaces=SPACES, doors=[])["rules"]}
    assert rules["A"]["checked"] == 3
    assert rules["B"]["checked"] == len(SPACES)


def test_pattern_matching_nothing_stays_visible():
    german = [space("1.01", "Schlafzimmer", "EG", 12.0), space("1.02", "Küche", "EG", 9.5)]
    rule = {"id": "A", "type": "space_min_area", "title": "Bedrooms", "min_area_m2": 9.0, "name_pattern": "bed"}
    report = r.evaluate(config(rule), spaces=german, doors=[])
    assert report["rules"][0]["checked"] == 0
    assert report["rules"][0]["summary"] == {"PASS": 0, "FAIL": 0, "UNKNOWN": 0}
    assert report["issues"] == []


def test_fire_door_self_closing():
    doors = [
        door("F1", "L1", 900, fire_rating="FRL -/60/30", self_closing=True),
        door("F2", "L1", 900, fire_rating="FRL -/30/30", self_closing=False),
        door("F3", "L1", 900, fire_rating="FRL -/30/30"),
        door("F4", "L1", 900, fire_rating="  ", self_closing=False),  # blank rating: not a fire door
        door("F5", "L1", 900),
    ]
    rule = {"id": "FD", "type": "fire_door_self_closing", "title": "Self-closing"}
    report = r.evaluate(config(rule), spaces=[], doors=doors)
    assert report["rules"][0]["checked"] == 3
    assert [(i["element"], i["status"], i["message"]) for i in report["issues"]] == [
        ("F2", "FAIL", "Fire-rated door (FRL -/30/30) is not self-closing"),
        ("F3", "UNKNOWN", "SelfClosing not recorded"),
    ]


def test_door_in_fire_wall_rated():
    doors = [
        door("W1", "L1", 900, fire_rating="FRL -/60/30", host_wall_fire_rating="FRL 60/60/60"),
        door("W2", "L1", 900, host_wall_fire_rating="FRL 60/60/60"),
        door("W3", "L1", 900, fire_rating="FRL -/60/30"),  # not in a rated wall
    ]
    rule = {"id": "FW", "type": "door_in_fire_wall_rated", "title": "Rated wall"}
    report = r.evaluate(config(rule), spaces=[], doors=doors)
    assert report["rules"][0]["summary"] == {"PASS": 1, "FAIL": 1, "UNKNOWN": 0}
    assert [(i["element"], i["message"]) for i in report["issues"]] == [
        ("W2", "Door sits in fire-rated wall (FRL 60/60/60) but has no FireRating"),
    ]


def test_placeholder_fire_ratings_are_unknown_not_fire_rated():
    doors = [
        door("P1", "L1", 900, fire_rating="Fire Rating"),  # Revit 2011 exports the parameter name
        door("P2", "L1", 900, fire_rating=" tbc ", self_closing=True),
        door("P3", "L1", 900, fire_rating="Fire Rating", host_wall_fire_rating="FRL 60/60/60"),
        door("P4", "L1", 900, host_wall_fire_rating="TBC"),
    ]
    rules = [{"id": "FD", "type": "fire_door_self_closing", "title": "Self-closing"},
             {"id": "FW", "type": "door_in_fire_wall_rated", "title": "Rated wall"}]
    report = r.evaluate({**config(*rules), "placeholder_values": ["Fire Rating", "TBC"]}, spaces=[], doors=doors)
    assert [(i["rule_id"], i["element"], i["status"], i["message"]) for i in report["issues"]] == [
        ("FD", "P1", "UNKNOWN", 'FireRating holds placeholder "Fire Rating"; confirm whether this door is fire-rated'),
        ("FD", "P2", "UNKNOWN", 'FireRating holds placeholder " tbc "; confirm whether this door is fire-rated'),
        ("FD", "P3", "UNKNOWN", 'FireRating holds placeholder "Fire Rating"; confirm whether this door is fire-rated'),
        ("FW", "P3", "UNKNOWN",
         'Door sits in fire-rated wall (FRL 60/60/60) but its FireRating holds placeholder "Fire Rating"'),
        ("FW", "P4", "UNKNOWN", 'Host wall FireRating holds placeholder "TBC"; confirm whether the wall is fire-rated'),
    ]
    # Without placeholder_values the same text counts as a real rating.
    plain = r.evaluate(config(*rules), spaces=[], doors=doors)
    assert plain["rules"][0]["summary"] == {"PASS": 1, "FAIL": 0, "UNKNOWN": 2}


def test_default_rules_treat_revit_fire_rating_placeholder(defaults):
    assert "Fire Rating" in defaults["placeholder_values"]


def test_severity_default_and_reference_optional():
    rule = {"id": "DW", "type": "door_min_width", "title": "Width", "min_width_mm": 900}
    report = r.evaluate(config(rule), spaces=[], doors=DOORS)
    assert report["rules"][0]["severity"] == "major"
    assert report["rules"][0]["reference"] is None
    assert report["description"] is None
    assert {i["element"] for i in report["issues"]} == {"D02", "D03", "D04", "D05"}


def test_load_custom_rules_file(tmp_path):
    path = tmp_path / "project.toml"
    path.write_text(
        'name = "Project X"\n'
        'description = "Client brief"\n'
        "[[rules]]\n"
        'id = "DW"\ntype = "door_min_width"\ntitle = "Width >= 900"\nmin_width_mm = 900\nseverity = "critical"\n',
        encoding="utf-8",
    )
    cfg = r.load_rules(path)
    assert cfg["name"] == "Project X"
    assert cfg["rules"][0]["severity"] == "critical"
    assert r.evaluate(cfg, spaces=[], doors=DOORS)["summary"] == {"PASS": 1, "FAIL": 3, "UNKNOWN": 1}


GOOD = {"id": "R1", "type": "door_min_width", "title": "Width", "min_width_mm": 850}


@pytest.mark.parametrize("data, match", [
    ({"rules": [GOOD]}, "name"),
    ({"name": "x", "rules": []}, r"\[\[rules\]\]"),
    ({"name": "x", "rules": [GOOD, GOOD]}, "R1: duplicate id"),
    ({"name": "x", "rules": [{**GOOD, "type": "door_colour"}]}, "R1: unknown type 'door_colour'"),
    ({"name": "x", "rules": [{k: v for k, v in GOOD.items() if k != "title"}]}, "R1: missing 'title'"),
    ({"name": "x", "rules": [{k: v for k, v in GOOD.items() if k != "id"}]}, "rule #1 has no 'id'"),
    ({"name": "x", "rules": [{**GOOD, "severity": "urgent"}]}, "R1: severity 'urgent'"),
    ({"name": "x", "rules": [{**GOOD, "min_width_mm": "850"}]}, "R1: 'min_width_mm' must be a number"),
    ({"name": "x", "rules": [{**GOOD, "min_width_mm": True}]}, "R1: 'min_width_mm' must be a number"),
    ({"name": "x", "rules": [{"id": "S1", "type": "space_min_area", "title": "t"}]}, "S1: 'min_area_m2'"),
    ({"name": "x", "rules": [{"id": "S1", "type": "space_min_area", "title": "t", "min_area_m2": 9,
                              "name_pattern": "bed("}]}, "S1: invalid name_pattern"),
    ({"name": "x", "rules": [{"id": "S1", "type": "space_min_area", "title": "t", "min_area_m2": 9,
                              "name_patern": "bed"}]}, "S1: unknown key.*name_patern"),
    ({"name": "x", "rules": [{**GOOD, "name_pattern": "bed"}]}, "R1: unknown key.*name_pattern"),
    ({"name": "x", "rule": [GOOD]}, "unknown top-level key.*rule"),
    ({"name": "x", "rules": [{**GOOD, "min_width_mm": float("nan")}]}, "R1: 'min_width_mm' must be a positive"),
    ({"name": "x", "rules": [{**GOOD, "min_width_mm": float("inf")}]}, "R1: 'min_width_mm' must be a positive"),
    ({"name": "x", "rules": [{**GOOD, "min_width_mm": -5}]}, "R1: 'min_width_mm' must be a positive"),
    ({"name": "x", "placeholder_values": "TBC", "rules": [GOOD]}, "'placeholder_values' must be a list"),
])
def test_invalid_config_raises(data, match):
    with pytest.raises(ValueError, match=match):
        r.evaluate(data, spaces=[], doors=[])


def test_load_rules_reports_file_and_rule(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text('name = "x"\n[[rules]]\nid = "R9"\ntype = "nope"\ntitle = "t"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="bad.toml.*R9: unknown type"):
        r.load_rules(path)
    path.write_text("name = \n", encoding="utf-8")
    with pytest.raises(ValueError, match="bad.toml"):
        r.load_rules(path)


def test_issues_to_csv(tmp_path, defaults):
    issues = r.evaluate(defaults, spaces=SPACES, doors=DOORS)["issues"]
    issues.append({**issues[0], "element": "1.02", "element_name": "Küche", "ifc_class": "IfcSpace",
                   "reference": None})
    out = issues_to_csv(issues, tmp_path / "nested" / "dir" / "issues.csv")

    assert out.is_file()
    assert out.read_bytes().startswith(b"\xef\xbb\xbf")  # BOM so Excel reads UTF-8
    with out.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    assert reader.fieldnames == CSV_COLUMNS
    assert len(rows) == len(issues) == 7
    assert rows[0]["rule_id"] == "DOOR-WIDTH-01"
    assert rows[0]["message"] == "820.0 mm < 850 mm (short by 30.0 mm)"
    assert rows[-1]["element_name"] == "Küche"
    assert rows[-1]["reference"] == ""


def test_issues_to_csv_empty_writes_header(tmp_path):
    out = issues_to_csv([], str(tmp_path / "empty.csv"))
    assert out.read_text(encoding="utf-8-sig").strip() == ",".join(CSV_COLUMNS)
