"""Stage local renderer updates for the supported 550W release pair."""

import argparse
from hashlib import sha256
from pathlib import Path


SUPPORTED_RENDERER = "1012c6fb032472f3409ba10f6f791dfcf0a5c45b3199eec3eb35ca9e15999b3a"
SUPPORTED_ENTRY = "076e2274fa2017138403f35dc1e415ba46ac4fa270c776652bec90c727a2c04a"
PROBE_SOURCE = Path(__file__).with_name("rig_surface_probe.js")
CREATE_MODEL = "async function bo({createCanvas:r,decalOptions:e}={}){"

PROBE_CALL_SITES = (
    ("c.updateMatrixWorld();let d=new E2,i=e.paint.clone();",
     "c.updateMatrixWorld();const findForearmHit=prepareRigSurfaceProbe(b2,c);let d=new E2,i=e.paint.clone();"),
    ('let N=d.intersectObject(c,!1)[0];if(!N)throw new Error("纵臂原面射线没有命中',
     'let N=findForearmHit(d);if(!N)throw new Error("纵臂原面射线没有命中'),
    ("L.updateMatrixWorld(!0);let m=S=>{let D={};",
     "L.updateMatrixWorld(!0);const findShoulderHit=prepareRigSurfaceProbe(b2,L);let m=S=>{let D={};"),
    ("let B=p.intersectObject(L,!1)[0];if(!B||30-B.distance<18.44)",
     "let B=findShoulderHit(p);if(!B||30-B.distance<18.44)"),
    ("B.updateMatrixWorld();let n1=new E2,p1=[];function d1(f1,c1){n1.set(F5(120,f1,c1),j0.clone().negate());",
     "B.updateMatrixWorld();const findWristHit=prepareRigSurfaceProbe(b2,B);let n1=new E2,p1=[];"
     "function d1(f1,c1){n1.set(F5(120,f1,c1),j0.clone().negate());"),
    ('let L1=n1.intersectObject(B,!1)[0];if(!L1)throw new Error("腕轴法兰没有对应的原壳表面',
     'let L1=findWristHit(n1);if(!L1)throw new Error("腕轴法兰没有对应的原壳表面'),
)


def stage(base: Path, output: Path):
    renderer = (base / "moss-render.js").read_bytes()
    entry = (base / "moss-view.js").read_bytes()
    if sha256(renderer).hexdigest() != SUPPORTED_RENDERER or sha256(entry).hexdigest() != SUPPORTED_ENTRY:
        raise ValueError("源渲染包不属于此优化支持的 550W 发布版本。")

    model_hook = CREATE_MODEL.encode("utf-8")
    if renderer.count(model_hook) != 1:
        raise ValueError("已发布的创建模型入口不唯一。")
    for old, new in PROBE_CALL_SITES:
        anchor = old.encode("utf-8")
        if renderer.count(anchor) != 1:
            raise ValueError(f"已发布的 CAD 探测入口不唯一 {old}")
        renderer = renderer.replace(anchor, new.encode("utf-8"))
    renderer = renderer.replace(model_hook, b"\n" + PROBE_SOURCE.read_bytes() + b"\n" + model_hook)

    old_hash = SUPPORTED_RENDERER.encode("ascii")
    if entry.count(old_hash) != 1:
        raise ValueError("页面入口没有引用这份已支持的渲染包。")
    new_hash = sha256(renderer).hexdigest().encode("ascii")
    output.mkdir(parents=True)
    (output / "moss-render.js").write_bytes(renderer)
    (output / "moss-view.js").write_bytes(entry.replace(old_hash, new_hash))
    return new_hash.decode("ascii")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True, help="支持的原版渲染包目录")
    parser.add_argument("--out", type=Path, required=True, help="全新的候选包目录")
    args = parser.parse_args()
    print(stage(args.base.resolve(), args.out.resolve()))
