import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile


ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "static" / "conversation-assets"
ACCEPTED = ROOT / "model-release.json"


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def read_json(path):
    with path.open(encoding="utf-8-sig") as handle:
        return json.load(handle)


def upstream_release(projects):
    candidates = [path for path in projects.glob("550W-MOSS-*.blend")
                  if not any(label in path.stem.lower() for label in ("原声", "音轨", "-audio")) and path.is_file()]
    if not candidates:
        raise ValueError(f"没有找到正式无声模型：{projects}")
    candidates.sort(key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True)
    delivery = projects / "moss/qa/blender-delivery"
    reports = [*sorted(delivery.rglob("release*.json")), *sorted(delivery.rglob("final.json"))]
    pending = []
    for index, source in enumerate(candidates):
        stat = source.stat()
        if index + 1 < len(candidates) and candidates[index + 1].stat().st_mtime_ns == stat.st_mtime_ns:
            raise ValueError(f"存在同时间戳的无声文件：{source.name}、{candidates[index + 1].name}")
        checksum = digest(source)
        for report in reports:
            master = report.with_suffix(".blend")
            if not master.is_file() or master.stat().st_size != stat.st_size or digest(master) != checksum:
                continue
            try:
                evidence = read_json(report)
            except (UnicodeError, json.JSONDecodeError) as error:
                raise ValueError(f"发布记录无法读取：{report}（{error}）") from error
            if not isinstance(evidence, dict) or not evidence:
                raise ValueError(f"发布记录内容无效：{report}")
            return source.name, checksum, report.relative_to(projects).as_posix(), pending
        pending.append((source.name, checksum))
    raise ValueError("尚无同一版本的无声源文件和配套发布记录：" + "、".join(name for name, _ in pending))


def asset_hashes():
    return {name: digest(ASSETS / name) for name in
            ("moss-exterior.glb", "moss-exterior.json", "moss-render.js", "moss-view.js")}


def record(proof_path, source, source_hash, release_report):
    exported = read_json(ASSETS / "moss-exterior.json")
    files = asset_hashes()
    if (exported.get("blend") != source or exported.get("sha256") != source_hash
            or exported.get("glbSha256") != files["moss-exterior.glb"]):
        raise ValueError("导出清单、公开 .blend 与实际 GLB 不一致")
    proof = read_json(proof_path)
    model = proof.get("modelRelease") or {}
    if (proof.get("passed") is not True or model.get("sourceFile") != source or model.get("sourceSha256") != source_hash
            or proof.get("rendererSha256") != files["moss-render.js"] or proof.get("glbSha256") != files["moss-exterior.glb"]
            or proof.get("entrySha256") != files["moss-view.js"]
            or not all(type(model.get(key)) is int and model[key] > 0 for key in ("visibleMeshes", "visibleTriangles", "groups"))):
        raise ValueError("需要对当前 GLB、渲染包和可见网格均通过的真实页面验收")
    qa_path = proof_path.resolve()
    summary = {"source": source, "sourceSha256": source_hash, "upstreamReport": release_report,
               "assets":files, "browser":{key:model[key] for key in ("groups", "visibleMeshes", "visibleTriangles")},
               "qaReport":qa_path.relative_to(ROOT).as_posix() if qa_path.is_relative_to(ROOT) else str(qa_path),
               "qaSha256":digest(proof_path)}
    # 完整写入后替换，避免中断时覆盖上次验收记录。
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=ACCEPTED.parent, prefix=".model-release-",
                                         suffix=".tmp", delete=False)
    temp = Path(handle.name)
    try:
        with handle:
            json.dump(summary, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        temp.replace(ACCEPTED)
    finally:
        temp.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="对照 550W 无声 Blender 交付件与网页已验收的模型")
    parser.add_argument("--projects", type=Path, default=Path(os.environ.get("MOSS_MODEL_PROJECTS", "F:/动画logo")))
    accept = parser.add_mutually_exclusive_group()
    accept.add_argument("--accept", type=Path, metavar="BROWSER_RESULT_JSON", help="登记通过浏览器验收的当前版本")
    accept.add_argument("--accept-runtime", type=Path, metavar="BROWSER_RESULT_JSON", help="登记已验收的渲染改动，沿用已登记模型")
    args = parser.parse_args()
    try:
        if args.accept_runtime:
            previous = read_json(ACCEPTED)
            current = asset_hashes()
            if any(current[name] != previous["assets"][name] for name in ("moss-exterior.glb", "moss-exterior.json")):
                raise ValueError("渲染改动需要沿用已登记的机身文件和导出清单。")
            record(args.accept_runtime, previous["source"], previous["sourceSha256"], previous["upstreamReport"])
            print(f"已更新渲染验收 {ACCEPTED}")
            return
        source, checksum, report, pending = upstream_release(args.projects.resolve())
        for name, pending_hash in pending:
            print(f"等待发布记录  {name}  SHA-256 {pending_hash}")
        print(f"上游无声模型  {source}\nSHA-256       {checksum}")
        print(f"发布记录      {report}")
        if args.accept:
            record(args.accept, source, checksum, report)
        if not ACCEPTED.is_file():
            print("待接入或验收。尚无已通过浏览器验收的 550W 模型清单。")
            return 2
        current = read_json(ACCEPTED)
        if current.get("sourceSha256") != checksum:
            print(f"有更新。上次验收模型为 {current.get('source', '未知')}。")
            return 2
        if current.get("assets") != asset_hashes():
            print("运行时资产已变更，请重新执行浏览器验收。")
            return 2
        print(f"已集成        {current['source']}\n浏览器验收    {current['qaReport']}")
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"无法核实：{error}")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
