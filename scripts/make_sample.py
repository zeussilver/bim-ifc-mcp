"""Generate two small, fully known IFC models for development and tests.

Real projects come out of Revit/ArchiCAD; these fixtures exist so every tool has
an answer we can check by hand:
  - 1 building, 2 storeys, 5 spaces; every space has a box Body (footprint = area)
  - 4 spaces carry NetFloorArea quantities; 102 has geometry only (3.0 x 2.8 m = 8.4 m2)
  - 5 doors: two pass the 850 mm width rule, two fail, one has no width at all
  - D04 is fire-rated but not self-closing; D03 fills an opening in fire wall W01
    yet has no FireRating of its own

sample_house.ifc is IFC4. sample_house_ifc2x3.ifc is the same building written
like a Revit IFC2X3 export: no quantity sets at all (areas must come from the
geometry) and doors typed by IfcDoorStyle.
"""

from pathlib import Path

import numpy as np

import ifcopenshell
import ifcopenshell.api.aggregate
import ifcopenshell.api.context
import ifcopenshell.api.feature
import ifcopenshell.api.geometry
import ifcopenshell.api.owner
import ifcopenshell.api.project
import ifcopenshell.api.pset
import ifcopenshell.api.root
import ifcopenshell.api.spatial
import ifcopenshell.api.type
import ifcopenshell.api.unit

MODELS = Path(__file__).resolve().parent.parent / "models"
OUTPUTS = {"IFC4": MODELS / "sample_house.ifc", "IFC2X3": MODELS / "sample_house_ifc2x3.ifc"}

STOREYS = [("Ground Floor", 0.0), ("Level 1", 3.0)]  # (name, elevation m)
SPACE_HEIGHT = 2.7  # m

SPACES = [
    # (storey, number, name, corner (x, y) m, size (dx, dy) m, IFC4 NetFloorArea m2 or None)
    ("Ground Floor", "G01", "Living", (0.0, 0.0), (6.5, 5.0), 32.5),
    ("Ground Floor", "G02", "Kitchen", (6.5, 0.0), (3.55, 4.0), 14.2),
    ("Ground Floor", "G03", "Accessible WC", (6.5, 4.0), (2.0, 2.4), 4.8),
    ("Level 1", "101", "Bedroom", (0.0, 0.0), (5.0, 3.0), 15.0),
    ("Level 1", "102", "Bedroom 2", (5.0, 0.0), (3.0, 2.8), None),
]

DOORS = [
    # (storey, mark, type, overall width mm or None, height mm, fire rating, self-closing)
    ("Ground Floor", "D01", "Entry Door", 1020, 2100, None, None),
    ("Ground Floor", "D02", "Internal Door", 870, 2040, None, None),
    ("Ground Floor", "D03", "Internal Door", 820, 2040, None, None),
    ("Ground Floor", "D04", "WC Door", 770, 2040, "FRL -/30/30", False),
    ("Level 1", "D05", "Internal Door", None, 2040, None, None),
]
DOOR_SPACING = 1.5  # m; door i sits at x = i * DOOR_SPACING on its storey

FIRE_WALL = ("Ground Floor", "W01", "Fire Wall W01", "FRL 60/60/60", "D03")  # storey, tag, name, rating, door


def place(model, product, x=0.0, y=0.0, z=0.0):
    """Absolute placement in metres; the API makes it relative to the parent's placement."""
    matrix = np.eye(4)
    matrix[:3, 3] = (x, y, z)
    ifcopenshell.api.geometry.edit_object_placement(model, product=product, matrix=matrix)


def box_body(model, context, dx, dy, height):
    """dx x dy m rectangle with its corner at the placement origin, extruded up (project units are mm)."""
    dx, dy, height = dx * 1000, dy * 1000, height * 1000
    centre = model.createIfcAxis2Placement2D(model.createIfcCartesianPoint((dx / 2, dy / 2)))
    profile = model.createIfcRectangleProfileDef("AREA", None, centre, dx, dy)
    origin = model.createIfcAxis2Placement3D(model.createIfcCartesianPoint((0.0, 0.0, 0.0)))
    solid = model.createIfcExtrudedAreaSolid(profile, origin, model.createIfcDirection((0.0, 0.0, 1.0)), height)
    return model.createIfcShapeRepresentation(context, "Body", "SweptSolid", [solid])


def build(schema: str = "IFC4") -> ifcopenshell.file:
    revit_like = schema == "IFC2X3"
    model = ifcopenshell.api.project.create_file(version=schema)
    if revit_like:  # IFC2X3 makes OwnerHistory mandatory; the API looks these up in the file
        ifcopenshell.api.owner.add_application(model)
        ifcopenshell.api.owner.add_person_and_organisation(
            model,
            person=ifcopenshell.api.owner.add_person(model),
            organisation=ifcopenshell.api.owner.add_organisation(model),
        )
    name = "Sample House (IFC2X3)" if revit_like else "Sample House"
    project = ifcopenshell.api.root.create_entity(model, ifc_class="IfcProject", name=name)
    ifcopenshell.api.unit.assign_unit(model)  # millimetres, m2, m3
    model_ctx = ifcopenshell.api.context.add_context(model, context_type="Model")
    body = ifcopenshell.api.context.add_context(
        model, context_type="Model", context_identifier="Body", target_view="MODEL_VIEW", parent=model_ctx
    )

    def spatial(ifc_class, name, parent):
        el = ifcopenshell.api.root.create_entity(model, ifc_class=ifc_class, name=name)
        if revit_like:
            el.CompositionType = "ELEMENT"  # required in IFC2X3
        ifcopenshell.api.aggregate.assign_object(model, products=[el], relating_object=parent)
        return el

    site = spatial("IfcSite", "Site", project)
    building = spatial("IfcBuilding", "House", site)
    place(model, site)
    place(model, building)
    storeys = {}
    for storey_name, elevation in STOREYS:
        storey = storeys[storey_name] = spatial("IfcBuildingStorey", storey_name, building)
        storey.Elevation = elevation * 1000
        place(model, storey, z=elevation)
    elevations = dict(STOREYS)

    for storey, number, long_name, (x, y), (dx, dy), area in SPACES:
        space = spatial("IfcSpace", number, storeys[storey])
        space.LongName = long_name
        if revit_like:
            space.InteriorOrExteriorSpace = "INTERNAL"  # required in IFC2X3
        place(model, space, x, y, elevations[storey])
        ifcopenshell.api.geometry.assign_representation(
            model, product=space, representation=box_body(model, body, dx, dy, SPACE_HEIGHT)
        )
        if area is not None and not revit_like:
            qto = ifcopenshell.api.pset.add_qto(model, product=space, name="Qto_SpaceBaseQuantities")
            ifcopenshell.api.pset.edit_qto(model, qto=qto, properties={"NetFloorArea": area})

    # Fire wall along the door line, with an opening around its door.
    wall_storey, wall_tag, wall_name, wall_rating, wall_door = FIRE_WALL
    door_x = [m for _, m, *_ in DOORS].index(wall_door) * DOOR_SPACING
    wall = ifcopenshell.api.root.create_entity(model, ifc_class="IfcWall", name=wall_name)
    wall.Tag = wall_tag
    ifcopenshell.api.spatial.assign_container(model, products=[wall], relating_structure=storeys[wall_storey])
    place(model, wall, door_x - 1.0, 0.0, elevations[wall_storey])
    ifcopenshell.api.geometry.assign_representation(
        model,
        product=wall,
        representation=ifcopenshell.api.geometry.add_wall_representation(
            model, context=body, length=2.0, height=SPACE_HEIGHT, thickness=0.2
        ),
    )
    pset = ifcopenshell.api.pset.add_pset(model, product=wall, name="Pset_WallCommon")
    ifcopenshell.api.pset.edit_pset(model, pset=pset, properties={"FireRating": wall_rating})
    opening = ifcopenshell.api.root.create_entity(model, ifc_class="IfcOpeningElement", name=f"Opening {wall_door}")
    ifcopenshell.api.feature.add_feature(model, feature=opening, element=wall)
    place(model, opening, door_x, 0.0, elevations[wall_storey])

    door_styles = {}
    for i, (storey, mark, type_name, width, height, fire, self_closing) in enumerate(DOORS):
        door = ifcopenshell.api.root.create_entity(model, ifc_class="IfcDoor", name=f"{type_name}:{mark}")
        door.Tag = mark
        door.OverallWidth = width
        door.OverallHeight = height
        ifcopenshell.api.spatial.assign_container(model, products=[door], relating_structure=storeys[storey])
        if mark == wall_door:
            fills = ifcopenshell.api.feature.add_filling(model, opening=opening, element=door)
            if revit_like:  # the API skips OwnerHistory on this relationship
                fills.OwnerHistory = ifcopenshell.api.owner.create_owner_history(model)
        place(model, door, i * DOOR_SPACING, 0.0, elevations[storey])
        if revit_like:
            if type_name not in door_styles:
                style = ifcopenshell.api.root.create_entity(model, ifc_class="IfcDoorStyle", name=type_name)
                # all four are required in IFC2X3
                style.OperationType = "SINGLE_SWING_LEFT"
                style.ConstructionType = "NOTDEFINED"
                style.ParameterTakesPrecedence = False
                style.Sizeable = False
                door_styles[type_name] = style
            ifcopenshell.api.type.assign_type(model, related_objects=[door], relating_type=door_styles[type_name])
        pset = ifcopenshell.api.pset.add_pset(model, product=door, name="Pset_DoorCommon")
        props = {"Reference": type_name, "IsExternal": mark == "D01"}
        if fire:
            props["FireRating"] = fire
        if self_closing is not None:
            props["SelfClosing"] = self_closing
        ifcopenshell.api.pset.edit_pset(model, pset=pset, properties=props)

    return model


if __name__ == "__main__":
    MODELS.mkdir(exist_ok=True)
    for schema, out in OUTPUTS.items():
        build(schema).write(str(out))
        print(f"wrote {out}")
