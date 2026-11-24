import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
from time import perf_counter
from urllib.request import urlopen

from core import Moss
from media_chat import verified_media_answer
from model_catalog import SPECS


async def run(snapshot, provider=None):
    folder = Path(__file__).resolve().parent / "runs" / datetime.now().strftime("named-media-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    label = "西海情歌 · 刀郎"
    live_provider = None
    if snapshot:
        proof = json.loads(snapshot.read_text(encoding="utf-8"))
    else:
        with urlopen("http://127.0.0.1:18221/api/state", timeout=10) as connection:
            live = json.load(connection)
        live_provider = live.get("model_id")
        shown = next(t for t in live["turns"] if t["user"] == "我想听刀郎的歌" and t.get("media", {}).get("items"))
        wrong = next(t for t in live["turns"] if t["user"] == label and t["source"] == "native" and "冲动的惩罚" in t["reply"])
        proof = {"rows": shown["media"]["items"], "shown_selected": shown["media"]["selected"]["media_id"],
                 "actual_old_reply": wrong["reply"], "actual_old_receipts": wrong.get("native_receipts", []),
                 "actual_old_mode": wrong["model_id"]}
    (folder / "snapshot.json").write_text(json.dumps(proof, ensure_ascii=False, indent=2), encoding="utf-8")
    report = {"passed": False, "historical_provider": proof["actual_old_mode"], "rows": [], "model_client_attempts": 0}
    moss = Moss(folder / "data", "http://127.0.0.1:18222/v1", "moss-chat-qwen35-4b")

    def reject_model_client(*_args, **_kwargs):
        report["model_client_attempts"] += 1
        raise AssertionError("本机点歌验收不得调用模型客户端")

    moss.model_client = reject_model_client

    async def ask(text):
        start = perf_counter()
        ident = await moss.message(text)
        await moss.job
        t = moss._get(ident)
        assert t["status"] == "replied" and t["source"] == "media", (text, t.get("error"), t["reply"])
        v = t["media"]
        report["rows"].append({"user": text, "model_selected": t["model_id"], "reply": t["reply"],
                               "id": (v.get("selected") or {}).get("media_id"), "autoplay": v["autoplay"],
                               "mode": v.get("play_mode"), "elapsed_ms": round((perf_counter() - start)*1000, 1)})
        return t
    try:
        available = [key for key in SPECS if moss.model_available(key)]
        if provider is not None:
            extra = provider if provider in available else None
            selection = {"requested": provider, "selected": extra, "source": "cli"}
            if extra is None:
                selection["skip_reason"] = "指定的云模型未配置可用凭据"
        else:
            extra = live_provider if live_provider in available else next(iter(available), None)
            selection = {"requested": live_provider, "selected": extra,
                         "source": "live" if extra is not None and extra == live_provider else "configured"}
            if extra is None:
                selection["skip_reason"] = "未找到已配置的云模型"
        report["provider_check"] = selection
        profiles = ("local", extra) if extra else ("local",)
        report["tested_profiles"] = list(profiles)
        expected = [r for r in proof["rows"] if r["title"] == "西海情歌" and r["artist"] == "刀郎"]
        assert len(expected) == 2
        checked = verified_media_answer(proof["actual_old_reply"], receipts=proof["actual_old_receipts"])
        assert checked == "本轮没有收到播放器的播放回执。"
        report["old_reply_after_receipt_check"] = checked
        for profile in profiles:
            await moss.select_model(profile)
            await moss.new_chat()
            cold = await ask(label)
            assert not cold["media"]["autoplay"] and len(cold["media"]["items"]) == 2
            assert all(x["title"] == "西海情歌" and x["artist"] == "刀郎" for x in cold["media"]["items"])
            cold_play = (await ask("播放" + label))["media"]
            assert cold_play["selected"]["media_id"] == expected[0]["media_id"] and cold_play["play_mode"] == "track"
            await moss.select_model("local")
            listing = await ask("搜索本地音乐 刀郎")
            assert [r["media_id"] for r in listing["media"]["items"]] == [r["media_id"] for r in proof["rows"]]
            assert listing["media"]["selected"]["media_id"] == proof["shown_selected"]
            assert all(Path(r["path"]).name in listing["reply"] for r in expected)
            await moss.select_model(profile)
            read = await ask("第二首叫什么，谁唱的？")
            second = proof["rows"][1]
            assert second["title"] in read["reply"] and second["artist"] in read["reply"] and not read["media"]["autoplay"]
            named = await ask(label)
            assert named["media"]["autoplay"] and named["media"]["play_mode"] == "track"
            assert named["media"]["selected"]["media_id"] == expected[0]["media_id"]
            assert "冲动的惩罚" not in named["reply"] and all(Path(r["path"]).name in named["reply"] for r in expected)
            missing = await ask("播放西海情歌 · 不存在的歌手")
            assert not missing["media"]["autoplay"] and not missing["media"]["selected"] and missing["media"]["items"] == []
        assert report["model_client_attempts"] == 0, report
        report["passed"] = True
    except Exception as error:
        report["error"] = repr(error)
        raise
    finally:
        await moss.close()
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--provider", choices=tuple(SPECS), help="指定第二组当下已配置的云模型，验收仍不请求推理")
    args = parser.parse_args()
    asyncio.run(run(args.snapshot, args.provider))
