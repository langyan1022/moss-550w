import argparse
import hashlib
import json
from pathlib import Path
import sys

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
from exterior_pbr import link_pbr


def principled(material):
    return next(node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED")


def extract(source, manifest, target, names):
    with source.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    if checksum != manifest["sourceSha256"]:
        raise ValueError("Blend 来源与网页机身清单不一致")
    missing = set(names) - manifest["materialRanges"].keys()
    if missing:
        raise ValueError(f"待核验材质没有出现在原机身清单：{sorted(missing)}")
    target.mkdir(parents=True, exist_ok=False)
    report = {"sourceSha256": checksum, "maps": {}}
    with bpy.data.libraries.load(str(source), link=False) as (library, requested):
        absent = set(names) - set(library.materials)
        if absent:
            raise ValueError(f"源模型中缺少材质：{sorted(absent)}")
        requested.materials = list(names)

    for name, material in zip(names, requested.materials, strict=True):
        web = bpy.data.materials.new("QA " + material.name)
        web.use_nodes = True
        settings = link_pbr(material, web, principled(material), principled(web))
        result = principled(web)
        roughness = result.inputs["Roughness"].links[0].from_node
        metallic = result.inputs["Metallic"].links[0].from_node
        if roughness != metallic or roughness.type != "SEPARATE_COLOR":
            raise ValueError(f"没有生成组合材质通道：{name}")
        image = roughness.inputs[0].links[0].from_node.image
        if settings["channelSize"] < 512 or list(image.size) != [settings["channelSize"]] * 2:
            raise ValueError(f"材质通道未使用源精度：{name}")
        payload = image.packed_file.data
        key = hashlib.sha256(payload).hexdigest()
        output = target / (key + ".png")
        output.write_bytes(payload)
        report["maps"][name] = {**settings, "file": output.name, "sha256": key, "bytes": len(payload)}
        bpy.data.materials.remove(web)
        bpy.data.images.remove(image)

    (target / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--blend", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("name", nargs="+")
    options = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    extract(options.blend, json.loads(options.manifest.read_text(encoding="utf-8")), options.output, options.name)
