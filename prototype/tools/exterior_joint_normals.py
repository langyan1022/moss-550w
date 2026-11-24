from math import acos, degrees

from mathutils import Vector


JOINTS = {
    "Shoulder bearing / fixed overlapping collar": "shoulderYaw",
    "Shoulder bearing / continuous output sleeve": "shoulderYaw",
    "颈槽同轴密封环": "elbowYaw",
}


def restore_joint_normals(mesh, source_name, transform, anchors):
    joint = JOINTS.get(source_name)
    if not joint:
        return None
    x, _, z = anchors[joint]
    center = Vector((x, -z))
    normal_world = transform.to_3x3().inverted().transposed()
    normal_local = normal_world.inverted()
    offsets = [(transform @ vertex.co).xy - center for vertex in mesh.vertices]
    radii = [offset.length for offset in offsets]
    threshold = max(radii, default=0) * .80
    original = [corner.vector.copy() for corner in mesh.corner_normals]
    normals = original.copy()
    selected = []
    before = []
    for polygon in mesh.polygons:
        outward = (normal_world @ polygon.normal).normalized()
        if abs(outward.z) > .10:
            continue
        corners = polygon.loop_indices
        vertices = polygon.vertices
        distances = [radii[index] for index in vertices]
        if min(distances) < threshold or max(distances) - min(distances) > .15:
            continue
        radial = [Vector((*offsets[index].normalized(), 0)) for index in vertices]
        if any(outward.dot(direction) < .97 for direction in radial):
            continue
        for corner, direction in zip(corners, radial, strict=True):
            old = (normal_world @ original[corner]).normalized()
            before.append(degrees(acos(max(-1, min(1, old.dot(direction))))))
            normals[corner] = (normal_local @ direction).normalized()
            selected.append((corner, direction))
    if not selected:
        return None
    mesh.normals_split_custom_set(normals)
    mesh.update()
    after = min((normal_world @ mesh.corner_normals[corner].vector).normalized().dot(direction)
                for corner, direction in selected)
    if after < .995:
        raise ValueError(f"圆筒外壁法线写入后未与径向一致：{source_name}")
    return {"sourceName": source_name, "joint": joint, "sideCorners": len(selected),
            "beforeMaxDegrees": round(max(before), 2),
            "afterMaxDegrees": round(degrees(acos(max(-1, min(1, after)))), 2)}
