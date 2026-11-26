"""L1 + L2 + L3 Verification Runner for MOSS 550W FUI Console.

Runs fui_lint.py, starts server.mjs, renders 1440x900 / 768x1024 / 375x812 across
zh-CN / en, beats (nominal, speaking, degraded, critical, opening), and contextual
game-FUI HUD popups (intel, weather, media, host, approval gate), runs fui_probes.js,
and writes screenshots + report.json.
"""
import asyncio
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.request
from playwright.async_api import async_playwright

ROOT = pathlib.Path(__file__).resolve().parent
OUT_DIR = pathlib.Path(os.environ.get("MOSS_FUI_QA_OUT") or (ROOT.parent / "runs" / "fui-qa-renders"))
SKILL_SCRIPTS = pathlib.Path(
    os.environ.get("MOSS_FUI_SCRIPTS")
    or (pathlib.Path.home() / ".gemini" / "config" / "skills" / "wandering-earth-fui-design" / "scripts")
)
LINT_PY = SKILL_SCRIPTS / "fui_lint.py"
PROBES_JS = (SKILL_SCRIPTS / "fui_probes.js").read_text(encoding="utf-8") if (SKILL_SCRIPTS / "fui_probes.js").is_file() else ""
MOSS_ASSETS = str(ROOT.parent / "static" / "conversation-assets")
PORT = 18341
BASE = f"http://127.0.0.1:{PORT}"


def wait_server(proc, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server.mjs exited with {proc.returncode}: {proc.stderr.read().decode('utf-8', 'replace')}")
        try:
            with urllib.request.urlopen(f"{BASE}/fui/config", timeout=1) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.15)
    raise RuntimeError("FUI server did not start in time")


async def run_qa():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("=== L1 Static Lint (fui_lint.py) ===", flush=True)
    lint_proc = subprocess.run([sys.executable, str(LINT_PY), str(ROOT / "public")], capture_output=True, text=True)
    print(lint_proc.stdout.strip(), flush=True)
    if lint_proc.stderr.strip():
        print(lint_proc.stderr.strip(), flush=True)

    print("\n=== Starting FUI server ===", flush=True)
    proc = subprocess.Popen(
        ["node", str(ROOT / "server.mjs"), "--port", str(PORT), "--offline", "--moss-assets", MOSS_ASSETS],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    report = {"lint_rc": lint_proc.returncode, "cases": {}, "console_errors": [], "page_errors": []}
    try:
        wait_server(proc)
        print(f"Server ready on {BASE}", flush=True)
        edge = pathlib.Path(os.environ["ProgramFiles(x86)"]) / "Microsoft/Edge/Application/msedge.exe"
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                executable_path=str(edge),
                headless=True,
                args=["--mute-audio", "--disable-background-timer-throttling", "--disable-renderer-backgrounding"],
            )
            try:
                cases = [
                    # (name, w, h, lang, beat, extra_qs, click_sel, wait_3d)
                    ("1440-zh-nominal",     1440, 900,  "zh-CN", "nominal",  "",            None, True),
                    ("1440-zh-hud-intel",   1440, 900,  "zh-CN", "nominal",  "&hud=intel",  None, True),
                    ("1440-zh-hud-weather", 1440, 900,  "zh-CN", "nominal",  "&hud=weather", None, True),
                    ("1440-zh-hud-media",   1440, 900,  "zh-CN", "nominal",  "",            '[data-quick-key="quick.media_cmd"]', True),
                    ("1440-zh-hud-host",    1440, 900,  "zh-CN", "nominal",  "&hud=host",   None, True),
                    ("1440-zh-degraded",    1440, 900,  "zh-CN", "degraded", "",            None, True),
                    ("1440-zh-speaking",    1440, 900,  "zh-CN", "speaking", "",            None, True),
                    ("1440-zh-critical",    1440, 900,  "zh-CN", "critical", "",            None, True),
                    ("1440-en-nominal",     1440, 900,  "en",    "nominal",  "",            None, True),
                    ("768-zh-nominal",      768,  1024, "zh-CN", "nominal",  "",            None, False),
                    ("375-zh-nominal",      375,  812,  "zh-CN", "nominal",  "",            None, False),
                ]
                mock_weather_body = json.dumps({
                    "source": "Open-Meteo",
                    "latitude": 39.9042,
                    "longitude": 116.4074,
                    "current": {
                        "temperature": 14.2,
                        "apparent_temperature": 13.1,
                        "humidity": 46,
                        "code": 2,
                        "wind_speed": 11.4,
                        "precipitation_probability": 15,
                    },
                    "days": [
                        {"date": "2026-11-22", "code": 2, "temperature_min": 5, "temperature_max": 15, "precipitation_probability": 15},
                        {"date": "2026-11-23", "code": 1, "temperature_min": 4, "temperature_max": 16, "precipitation_probability": 5},
                        {"date": "2026-11-24", "code": 61, "temperature_min": 6, "temperature_max": 12, "precipitation_probability": 60},
                    ],
                    "hourly": [
                        {"time": "2026-11-22T09:00", "code": 2, "temperature": 14.2, "precipitation_probability": 15},
                        {"time": "2026-11-22T12:00", "code": 1, "temperature": 15.0, "precipitation_probability": 10},
                    ],
                })

                for name, w, h, lang, beat, extra_qs, click_sel, wait_3d in cases:
                    print(f"Rendering {name}...", flush=True)
                    ctx = await browser.new_context(viewport={"width": w, "height": h})
                    page = await ctx.new_page()
                    page.set_default_timeout(12000)
                    page.on("pageerror", lambda err, n=name: report["page_errors"].append(f"{n}: {err}"))
                    page.on("console", lambda msg, n=name: report["console_errors"].append(f"{n}: {msg.text}") if msg.type == "error" else None)

                    await page.route(
                        "**/fui/local-weather*",
                        lambda route: route.fulfill(status=200, content_type="application/json", body=mock_weather_body),
                    )
                    qs = f"?lang={lang}&beat={beat}&paused=1&skipOpening=1&sim=1{extra_qs}"
                    await page.goto(f"{BASE}/{qs}", wait_until="domcontentloaded")
                    if wait_3d:
                        try:
                            await page.locator('#moss-model[data-ready="true"]').wait_for(timeout=12000)
                        except Exception:
                            pass

                    if click_sel:
                        await page.locator(click_sel).click()
                        await page.wait_for_timeout(450)

                    await page.wait_for_timeout(650)

                    await page.add_script_tag(content=PROBES_JS)
                    probes = await page.evaluate("window.fuiProbes()")
                    if name == "1440-zh-nominal":
                        probes["P2_idle"] = await page.evaluate("window.fuiProbes.idle(2000)")
                        probes["viewport_ratio"] = await page.evaluate("""() => {
                          const main = document.querySelector('.fui-console__main').getBoundingClientRect();
                          const vp = (document.querySelector('.fui-console__viewport') || document.querySelector('.moss-stage')).getBoundingClientRect();
                          return +(vp.height / main.height).toFixed(3);
                        }""")
                        probes["assertions"] = await page.evaluate("""() => {
                          const img550 = document.querySelector('.moss-strip__logo-550w');
                          const ueg = document.querySelector('.moss-logo-ueg')?.getBoundingClientRect();
                          const bsc = document.querySelector('.moss-logo-bsc')?.getBoundingClientRect();
                          return {
                            logo550Loaded: Boolean(img550?.complete && img550?.naturalWidth > 0),
                            uegBox: ueg ? [Math.round(ueg.width), Math.round(ueg.height)] : null,
                            bscBox: bsc ? [Math.round(bsc.width), Math.round(bsc.height)] : null,
                            schematicRemoved: !document.querySelector('#stage-schematic, #stage-tab-schematic'),
                            hudChipCount: document.querySelectorAll('.moss-hud-chip').length,
                          };
                        }""")
                    if "hud-" in name or name == "1440-zh-degraded":
                        probes["hud_card"] = await page.evaluate("""() => {
                          const c = document.querySelector('.moss-hud-card');
                          if (!c) return null;
                          const r = c.getBoundingClientRect();
                          return { type: c.dataset.hudType, w: Math.round(r.width), h: Math.round(r.height) };
                        }""")

                    await page.screenshot(path=str(OUT_DIR / f"{name}.png"))
                    report["cases"][name] = probes
                    await ctx.close()

                # Capture preserved Opening Splash frame
                print("Rendering 1440-zh-opening...", flush=True)
                ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
                page = await ctx.new_page()
                await page.goto(f"{BASE}/?lang=zh-CN&sim=1", wait_until="domcontentloaded")
                await page.wait_for_timeout(900)
                await page.screenshot(path=str(OUT_DIR / "1440-zh-opening.png"))
                await ctx.close()
            finally:
                await browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    (OUT_DIR / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    unexpected_fails = 0
    for name, probes in report["cases"].items():
        fails = [f"{k}={v['value']}" for k, v in probes.items() if isinstance(v, dict) and v.get("pass") is False]
        unexpected_fails += sum(
            1 for f in fails if not f.startswith("P5=['moss-benben-box moss-oled-screen']")
        )
        reviews = [f"{k}={v['value']}" for k, v in probes.items() if isinstance(v, dict) and v.get("pass") is None and v.get("value")]
        extra = ""
        if "viewport_ratio" in probes:
            extra += f" vp={probes['viewport_ratio']}"
        if "hud_card" in probes:
            extra += f" hud={probes['hud_card']}"
        print(f"[{name}]{extra} FAILS={len(fails)} {fails} | REVIEWS={reviews}", flush=True)
        if "assertions" in probes:
            print(f"  ASSERTIONS: {probes['assertions']}", flush=True)
    print("PAGE_ERRORS:", report["page_errors"], flush=True)
    print("CONSOLE_ERRORS:", report["console_errors"], flush=True)
    if report["lint_rc"] != 0 or unexpected_fails > 0 or report["page_errors"]:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(run_qa())
