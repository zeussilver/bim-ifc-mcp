import math
import re
import uuid
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import ifcopenshell
import ifcopenshell.api.context
import ifcopenshell.api.unit
import ifcopenshell.guid
import pytest

from bim_ifc_mcp import bcf

NOW = datetime(2026, 10, 4, 9, 30, tzinfo=timezone.utc)
GUID_RE = re.compile(r"^[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$")

# xs:sequence orders copied from the official BCF 2.1 XSDs (buildingSMART/BCF-XML release_2_1/Schemas).
MARKUP_ORDER = ["Header", "Topic", "Comment", "Viewpoints"]
TOPIC_ORDER = [
    "ReferenceLink", "Title", "Priority", "Index", "Labels", "CreationDate", "CreationAuthor", "ModifiedDate",
    "ModifiedAuthor", "DueDate", "AssignedTo", "Stage", "Description", "BimSnippet", "DocumentReference",
    "RelatedTopic",
]
VISINFO_ORDER = ["Components", "OrthogonalCamera", "PerspectiveCamera", "Lines", "ClippingPlanes", "Bitmap"]
COMPONENTS_ORDER = ["ViewSetupHints", "Selection", "Visibility", "Coloring"]
CAMERA_ORDER = ["CameraViewPoint", "CameraDirection", "CameraUpVector", "FieldOfView"]

DUPLEX = Path(__file__).resolve().parent.parent / "models" / "external" / "Duplex_A_20110907.ifc"
DOOR_GID = "2O2Fr$t4X7Zf8NOew3FLOH"
SPACE_GID = "0YvctVUKr0kugbFTf53O9L"


def make_issue(**overrides) -> dict:
    issue = {
        "rule_id": "DOOR-WIDTH-01", "rule_title": "Door width >= 850 mm", "severity": "major", "status": "FAIL",
        "ifc_class": "IfcDoor", "global_id": DOOR_GID, "element": "D03", "element_name": "Internal Door:D03",
        "storey": "Ground Floor", "message": "820.0 mm < 850 mm (short by 30.0 mm)", "reference": "AS 1428.1",
    }
    return {**issue, **overrides}


ISSUES = [
    make_issue(),
    make_issue(rule_id="SPACE-AREA-01", rule_title="Bedroom >= 9 m2", severity="minor", ifc_class="IfcSpace",
               global_id=SPACE_GID, element="102", element_name="Bedroom 2", storey="Level 1",
               message="8.4 m2 < 9.0 m2 (short by 0.6 m2) [area from geometry]", reference=None),
    make_issue(rule_id="DOOR-WIDTH-01", status="UNKNOWN", severity="info", global_id="1kTvXnbbzCWw8lcMd1dR4o",
               element="D05", message="OverallWidth missing in model"),
]


def read_bcf(path) -> dict[str, ET.Element]:
    with zipfile.ZipFile(path) as zf:
        return {i.filename: ET.fromstring(zf.read(i)) for i in zf.infolist() if not i.is_dir()}


def tags(el: ET.Element) -> list[str]:
    return [child.tag for child in el]


def assert_follows(actual: list[str], xsd_order: list[str]):
    """Children appear in XSD sequence order (repeats allowed, optional ones may be missing)."""
    assert all(t in xsd_order for t in actual), actual
    positions = [xsd_order.index(t) for t in actual]
    assert positions == sorted(positions), actual


def topic_guids(path) -> list[str]:
    return sorted({name.split("/")[0] for name in zipfile.ZipFile(path).namelist() if "/" in name})


def build_model(schema: str):
    """Tiny model in millimetres: a door placed 3.5 m up via a storey placement, a space with no placement."""
    model = ifcopenshell.file(schema=schema)
    model.header.file_name.time_stamp = "2011-09-07T12:28:29"
    project = model.create_entity("IfcProject", GlobalId=ifcopenshell.guid.new(), Name="BCF Test")
    ifcopenshell.api.unit.assign_unit(model)  # millimetres

    def local(xyz, rel_to=None):
        point = model.create_entity("IfcCartesianPoint", Coordinates=xyz)
        axis = model.create_entity("IfcAxis2Placement3D", Location=point)
        return model.create_entity("IfcLocalPlacement", PlacementRelTo=rel_to, RelativePlacement=axis)

    storey = local((0.0, 0.0, 3000.0))
    door = model.create_entity("IfcDoor", GlobalId=ifcopenshell.guid.new(), Name="D03",
                               ObjectPlacement=local((1000.0, 2000.0, 500.0), storey))
    space = model.create_entity("IfcSpace", GlobalId=ifcopenshell.guid.new(), Name="102")
    return model, project, door, space


def add_revit_room(model, storey_placement):
    """IfcSpace placed at the storey origin like Revit does, its 2 x 3 x 2.7 m room drawn 4 m / 5 m away."""
    context = ifcopenshell.api.context.add_context(model, context_type="Model")
    centre = model.createIfcAxis2Placement2D(model.createIfcCartesianPoint((5000.0, 6500.0)))
    profile = model.createIfcRectangleProfileDef("AREA", None, centre, 2000.0, 3000.0)
    origin = model.createIfcAxis2Placement3D(model.createIfcCartesianPoint((0.0, 0.0, 0.0)))
    solid = model.createIfcExtrudedAreaSolid(profile, origin, model.createIfcDirection((0.0, 0.0, 1.0)), 2700.0)
    body = model.createIfcShapeRepresentation(context, "Body", "SweptSolid", [solid])
    return model.create_entity(
        "IfcSpace", GlobalId=ifcopenshell.guid.new(), Name="A102",
        ObjectPlacement=model.createIfcLocalPlacement(storey_placement, origin),
        Representation=model.createIfcProductDefinitionShape(None, None, [body]),
    )


def viewpoints(path) -> dict[str, ET.Element]:
    """{selected IfcGuid: VisualizationInfo}"""
    return {root.find("Components/Selection/Component").get("IfcGuid"): root
            for name, root in read_bcf(path).items() if name.endswith("viewpoint.bcfv")}


def camera_point(visinfo: ET.Element) -> list[float]:
    return [float(visinfo.findtext(f"PerspectiveCamera/CameraViewPoint/{a}")) for a in "XYZ"]


def test_zip_members_and_xml(tmp_path):
    out = bcf.issues_to_bcf(ISSUES, tmp_path / "issues.bcf", now=NOW)
    files = read_bcf(out)
    guids = topic_guids(out)
    assert len(guids) == 3
    assert sorted(files) == sorted(["bcf.version"] + [f"{g}/{f}" for g in guids for f in ("markup.bcf", "viewpoint.bcfv")])
    # The BCF 2.1 README requires a folder entry per topic in the zip's central directory.
    dirs = [i.filename for i in zipfile.ZipFile(out).infolist() if i.is_dir()]
    assert sorted(dirs) == [f"{g}/" for g in guids]
    assert zipfile.ZipFile(out).read("bcf.version").startswith(b"<?xml")

    version = files["bcf.version"]
    assert version.tag == "Version" and version.get("VersionId") == "2.1"
    assert version.findtext("DetailedVersion") == "2.1"

    for g in guids:
        assert GUID_RE.match(g)
        markup, visinfo = files[f"{g}/markup.bcf"], files[f"{g}/viewpoint.bcfv"]
        assert markup.find("Topic").get("Guid") == g
        vp = markup.find("Viewpoints")
        assert GUID_RE.match(vp.get("Guid")) and vp.get("Guid") == visinfo.get("Guid") != g
        assert vp.findtext("Viewpoint") == "viewpoint.bcfv"
        assert "Header" not in tags(markup)  # no model, no filename


def test_element_order_follows_xsd(tmp_path):
    model, project, door, _ = build_model("IFC4")
    issues = [make_issue(global_id=door.GlobalId), ISSUES[1]]
    out = bcf.issues_to_bcf(issues, tmp_path / "issues.bcf", model=model, ifc_filename="house.ifc", now=NOW)
    files = read_bcf(out)
    for g in topic_guids(out):
        markup, visinfo = files[f"{g}/markup.bcf"], files[f"{g}/viewpoint.bcfv"]
        assert tags(markup) == ["Header", "Topic", "Viewpoints"]
        assert_follows(tags(markup), MARKUP_ORDER)
        assert_follows(tags(markup.find("Topic")), TOPIC_ORDER)
        assert tags(markup.find("Header/File")) == ["Filename", "Date"]
        assert_follows(tags(visinfo), VISINFO_ORDER)
        assert_follows(tags(visinfo.find("Components")), COMPONENTS_ORDER)
        if visinfo.find("PerspectiveCamera") is not None:
            assert tags(visinfo.find("PerspectiveCamera")) == CAMERA_ORDER
            for point in visinfo.find("PerspectiveCamera")[:3]:
                assert tags(point) == ["X", "Y", "Z"]
    project_ext = files["project.bcfp"]
    assert tags(project_ext) == ["Project", "ExtensionSchema"]


def test_topic_content(tmp_path):
    out = bcf.issues_to_bcf(ISSUES, tmp_path / "issues.bcf", author="reviewer@example.com", now=NOW)
    files = read_bcf(out)
    topics = {t.findtext("Title"): t for t in (files[n].find("Topic") for n in files if n.endswith("markup.bcf"))}
    door = topics["DOOR-WIDTH-01 D03: 820.0 mm < 850 mm (short by 30.0 mm)"]
    assert door.get("TopicType") == "Error" and door.get("TopicStatus") == "Open"
    assert door.findtext("Priority") == "Major"
    assert [l.text for l in door.findall("Labels")] == ["DOOR-WIDTH-01", "IfcDoor"]
    assert door.findtext("CreationDate") == "2026-10-04T09:30:00Z"
    assert door.findtext("CreationAuthor") == "reviewer@example.com"
    description = door.findtext("Description")
    for text in ("820.0 mm < 850 mm", "Status: FAIL", "Storey: Ground Floor", "Reference: AS 1428.1", bcf.DISCLAIMER):
        assert text in description

    unknown = topics["DOOR-WIDTH-01 D05: OverallWidth missing in model"]
    assert unknown.get("TopicType") == "Request" and unknown.findtext("Priority") == "Normal"
    space = topics["SPACE-AREA-01 102: 8.4 m2 < 9.0 m2 (short by 0.6 m2) [area from geometry]"]
    assert space.findtext("Priority") == "Minor"
    assert "Reference:" not in space.findtext("Description")


def test_selection_and_visibility(tmp_path):
    out = bcf.issues_to_bcf(ISSUES[:2], tmp_path / "issues.bcf", now=NOW)
    files = read_bcf(out)
    by_guid = {}
    for name, root in files.items():
        if name.endswith("viewpoint.bcfv"):
            by_guid[root.find("Components/Selection/Component").get("IfcGuid")] = root
    assert set(by_guid) == {DOOR_GID, SPACE_GID}
    for root in by_guid.values():
        assert root.find("Components/Visibility").get("DefaultVisibility") == "true"
        assert root.find("PerspectiveCamera") is None  # no model, no camera
    assert by_guid[DOOR_GID].find("Components/ViewSetupHints") is None
    assert by_guid[SPACE_GID].find("Components/ViewSetupHints").get("SpacesVisible") == "true"


def test_guids_stable_and_output_deterministic(tmp_path):
    a = bcf.issues_to_bcf(ISSUES, tmp_path / "a.bcf", now=NOW)
    b = bcf.issues_to_bcf(ISSUES, tmp_path / "b.bcfzip", now=NOW)
    c = bcf.issues_to_bcf(list(reversed(ISSUES)), tmp_path / "c.bcf", now=datetime(2027, 1, 1, tzinfo=timezone.utc))
    assert a.read_bytes() == b.read_bytes()
    assert topic_guids(a) == topic_guids(c)
    assert str(uuid.uuid5(bcf.NAMESPACE, f"DOOR-WIDTH-01|{DOOR_GID}")) in topic_guids(a)


@pytest.mark.parametrize("schema", ["IFC4", "IFC2X3"])
def test_camera_only_with_placement(tmp_path, schema):
    model, project, door, space = build_model(schema)
    issues = [make_issue(global_id=door.GlobalId),
              make_issue(rule_id="SPACE-AREA-01", ifc_class="IfcSpace", global_id=space.GlobalId, element="102")]
    out = bcf.issues_to_bcf(issues, tmp_path / "issues.bcf", model=model, ifc_filename="house.ifc", now=NOW)
    files = read_bcf(out)

    project_ext = files["project.bcfp"]
    assert GUID_RE.match(project_ext.find("Project").get("ProjectId"))
    assert project_ext.findtext("Project/Name") == "BCF Test"
    for name, root in files.items():
        if name.endswith("markup.bcf"):
            assert root.find("Header/File").get("IfcProject") == project.GlobalId
            assert root.findtext("Header/File/Filename") == "house.ifc"
            assert root.findtext("Header/File/Date") == "2011-09-07T12:28:29"  # the IFC file's date ...
            assert root.findtext("Topic/CreationDate") == "2026-10-04T09:30:00Z"  # ... not the export's

    views = {root.find("Components/Selection/Component").get("IfcGuid"): root
             for name, root in files.items() if name.endswith("viewpoint.bcfv")}
    assert views[space.GlobalId].find("PerspectiveCamera") is None

    cam = views[door.GlobalId].find("PerspectiveCamera")
    vec = {t: [float(cam.findtext(f"{t}/{a}")) for a in "XYZ"] for t in CAMERA_ORDER[:3]}
    # Door origin is (1, 2, 3.5) m (mm model, storey placement 3 m up); camera sits at offset (-6, -6, +5) m.
    assert vec["CameraViewPoint"] == pytest.approx([-5.0, -4.0, 8.5])
    assert vec["CameraDirection"] == pytest.approx([6 / math.sqrt(97), 6 / math.sqrt(97), -5 / math.sqrt(97)], abs=1e-4)
    up = vec["CameraUpVector"]
    assert math.hypot(*up) == pytest.approx(1, abs=1e-3)
    assert sum(u * d for u, d in zip(up, vec["CameraDirection"])) == pytest.approx(0, abs=1e-3)
    assert up[2] > 0
    assert float(cam.findtext("FieldOfView")) == 60


@pytest.mark.parametrize("schema", ["IFC4", "IFC2X3"])
def test_camera_aims_at_room_geometry_not_storey_origin(tmp_path, schema):
    model, _, door, _ = build_model(schema)
    room = add_revit_room(model, door.ObjectPlacement.PlacementRelTo)
    issue = make_issue(rule_id="SPACE-AREA-01", ifc_class="IfcSpace", global_id=room.GlobalId, element="A102")
    out = bcf.issues_to_bcf([issue], tmp_path / "issues.bcf", model=model, now=NOW)
    # Room box x 4..6, y 5..8, z 3..5.7 m (storey 3 m up) has centre (5, 6.5, 4.35); camera offset (-6, -6, +5) m.
    assert camera_point(viewpoints(out)[room.GlobalId]) == pytest.approx([-1.0, 0.5, 9.35])


@pytest.mark.skipif(not DUPLEX.is_file(), reason="run scripts/fetch_samples.py")
def test_revit_export_rooms_get_their_own_viewpoint(tmp_path):
    # Revit 2011 export: most rooms sit at their storey origin, yet each topic must show its own room.
    model = ifcopenshell.open(str(DUPLEX))
    rooms = model.by_type("IfcSpace")
    issues = [make_issue(rule_id="SPACE-AREA-01", ifc_class="IfcSpace", global_id=s.GlobalId, element=s.Name)
              for s in rooms]
    out = bcf.issues_to_bcf(issues, tmp_path / "duplex.bcf", model=model, now=NOW)
    assert len({tuple(camera_point(v)) for v in viewpoints(out).values()}) == len(rooms)
    markup = next(root for name, root in read_bcf(out).items() if name.endswith("markup.bcf"))
    assert markup.findtext("Header/File/Date") == "2011-09-07T12:28:29"


@pytest.mark.parametrize("stamp", ["", "Wed Sep  7 12:28:29 2011"])
def test_header_date_omitted_without_usable_file_date(tmp_path, stamp):
    model, *_ = build_model("IFC4")
    model.header.file_name.time_stamp = stamp
    out = bcf.issues_to_bcf(ISSUES[:1], tmp_path / "issues.bcf", model=model, ifc_filename="house.ifc", now=NOW)
    markup = next(root for name, root in read_bcf(out).items() if name.endswith("markup.bcf"))
    assert tags(markup.find("Header/File")) == ["Filename"]


def test_xml_forbidden_characters_are_replaced(tmp_path):
    # IFC strings can carry characters XML 1.0 forbids, e.g. a Word line break (U+000B) in a Revit name.
    model, project, door, _ = build_model("IFC4")
    project.Name = "BCF\x0cTest"
    issue = make_issue(global_id=door.GlobalId, element_name="Door\x0bLine2", storey="Level\x001")
    files = read_bcf(bcf.issues_to_bcf([issue], tmp_path / "issues.bcf", model=model, now=NOW))  # parses every member
    description = next(root for name, root in files.items() if name.endswith("markup.bcf")).findtext("Topic/Description")
    assert "Door Line2" in description and "Storey: Level 1" in description
    assert files["project.bcfp"].findtext("Project/Name") == "BCF Test"


def test_rejects_bad_suffix_and_duplicates(tmp_path):
    with pytest.raises(ValueError, match="bcf"):
        bcf.issues_to_bcf(ISSUES, tmp_path / "issues.zip")
    with pytest.raises(ValueError, match="Duplicate"):
        bcf.issues_to_bcf([ISSUES[0], ISSUES[0]], tmp_path / "issues.bcf")


def test_empty_issue_list_and_nested_dir(tmp_path):
    out = bcf.issues_to_bcf([], tmp_path / "out" / "nested" / "issues.bcf", now=NOW)
    assert zipfile.ZipFile(out).namelist() == ["bcf.version"]
