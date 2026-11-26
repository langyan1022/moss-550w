"""MOSS 550W Strict Automated Release Gate (v5.3+).

Prevents blind spots from slipping into live releases across 4 fail-fast stages:
  Stage 1 (static): py_compile, node --check, i18n EN/ZH exact key parity,
                    cross-module JS export/call + HTML ID + /api/* contract audit,
                    fui_lint.py (0 blocker / 0 major / 0 minor), check_model_release.py.
  Stage 2 (unit):   spoken numbers (24/24), dialogue quality unit + HTTP,
                    erhua/personality/anti-hallucination unit (39/39),
                    v5.3 echo-prefix stripper, cloud transient retry, sustained-onset trim,
                    5-gesture classifier, and live CPU 3-sample alert debounce.
  Stage 3 (audio):  FUI speech latency parity (9/9) + real RTX 3070 Ti warm pre-warm
                    & sustained-onset head silence verification.
  Stage 4 (ui):     Live Edge full-click QA (verify_v53_gate_ui.mjs) + NumPy/PIL
                    pixel-sanity audit on all 8 Intel Dossier logos (rejects blank
                    black masks AND flat solid-circle blobs!).

Usage:
  python release_gate.py --stages static,unit
  python release_gate.py --stages static,unit,audio
  python release_gate.py --stages ui --url http://127.0.0.1:18221
  python release_gate.py --stages all --url http://127.0.0.1:18221
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import py_compile
import re
import subprocess
import sys
import tempfile
from time import perf_counter

ROOT = Path(__file__).resolve().parent
FUI_PUBLIC = ROOT / "fui" / "public"
FUI_LINT = Path(
    os.environ.get("MOSS_FUI_LINT")
    or (Path.home() / ".gemini" / "config" / "skills" / "wandering-earth-fui-design" / "scripts" / "fui_lint.py")
)

def collect_core_py_files() -> list[Path]:
    files = sorted(
        p for p in ROOT.glob("*.py")
        if not p.name.startswith("verify_") and p.name != "release_gate.py"
    )
    files += sorted((ROOT / "voice").glob("*.py"))
    files += sorted((ROOT / "holojarvis_runtime").glob("*.py"))
    files += sorted((ROOT / "brain").glob("*.py"))
    files += sorted((ROOT / "testkit").glob("*.py"))
    return files


def run_cmd(cmd, *, cwd=ROOT, env=None, label=""):
    started = perf_counter()
    full_env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", **(env or {})}
    proc = subprocess.run(cmd, cwd=str(cwd), env=full_env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    dur = round(perf_counter() - started, 2)
    if proc.returncode != 0:
        tail = "\n".join((proc.stdout + "\n" + proc.stderr).strip().splitlines()[-35:])
        raise RuntimeError(f"[{label}] FAILED (exit={proc.returncode}, {dur}s):\n{tail}")
    return {"label": label, "ok": True, "seconds": dur, "tail": (proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "")}


# ── Stage 1: Static & Cross-Module Contract Audit ─────────────────────────────

def audit_js_contracts():
    """Catch broken cross-module JS calls (like voice.isTtsEnabled), missing HTML IDs, and bad /api/ paths."""
    js_dir = FUI_PUBLIC / "js"
    app_src = (js_dir / "app.js").read_text(encoding="utf-8")

    # 1. Extract returned API keys from every createXxx factory in fui/public/js/*.js
    factory_methods: dict[str, set[str]] = {}
    for js_file in sorted(js_dir.glob("*.js")):
        src = js_file.read_text(encoding="utf-8")
        # Find each exported create* function and brace-match its own function body
        for match in re.finditer(r"export\s+function\s+(create\w+)\s*\(", src):
            fname = match.group(1)
            # Find opening `{` of the function body (skipping default param objects inside `(...)`)
            p_idx = match.end()
            p_depth = 1
            while p_idx < len(src) and p_depth > 0:
                if src[p_idx] == "(":
                    p_depth += 1
                elif src[p_idx] == ")":
                    p_depth -= 1
                p_idx += 1
            brace_open = src.find("{", p_idx)
            if brace_open == -1:
                continue
            # Find matching closing `}` of this function
            b_idx = brace_open + 1
            b_depth = 1
            while b_idx < len(src) and b_depth > 0:
                ch = src[b_idx]
                if ch == "{":
                    b_depth += 1
                elif ch == "}":
                    b_depth -= 1
                b_idx += 1
            body = src[brace_open:b_idx]
            # Find the last top-level (2-space indented) `return {` inside this function
            ret_matches = list(re.finditer(r"\n  return\s*\{", body))
            if not ret_matches:
                ret_matches = list(re.finditer(r"return\s*\{", body))
            if not ret_matches:
                continue
            ret_start = ret_matches[-1].end()
            # Brace-match to find the matching `}`
            depth = 1
            idx = ret_start
            while idx < len(body) and depth > 0:
                ch = body[idx]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                idx += 1
            obj_text = body[ret_start: idx - 1]
            # Extract top-level property/method names at depth 0 inside the return object
            keys: set[str] = set()
            d = 0
            token_buf = []
            for line in obj_text.splitlines():
                stripped = line.strip()
                if d == 0 and stripped and not stripped.startswith("//") and not stripped.startswith("/*"):
                    if stripped.startswith("..."):
                        keys.add(stripped.split(",")[0].strip())
                    else:
                        m = re.match(r"^(?:async\s+)?(?:get\s+|set\s+)?([A-Za-z_$][\w$]*)\s*(?:[:,(}]|$)", stripped)
                        if m:
                            keys.add(m.group(1))
                # Track brace nesting across lines (ignore strings/comments loosely)
                clean_line = re.sub(r"//.*", "", line)
                d += clean_line.count("{") - clean_line.count("}")
            factory_methods[fname] = keys

    # Resolve spreads (e.g. createDuplexVoice spreads voiceOutput / createVoiceEngine)
    if "createDuplexVoice" in factory_methods and "createVoiceEngine" in factory_methods:
        factory_methods["createDuplexVoice"] |= factory_methods["createVoiceEngine"]

    # Map controller variable names in app.js to their factory names
    bindings: dict[str, str] = {}
    for m in re.finditer(r"(?:const|let)?\s*([a-zA-Z_$][\w$]*)\s*=\s*(create\w+)\s*\(", app_src):
        var_name, factory_name = m.group(1), m.group(2)
        if factory_name in factory_methods:
            bindings[var_name] = factory_name
    bindings_by_canonical = {
        **bindings,
        "voice": "createDuplexVoice",
        "voiceOutput": "createVoiceEngine",
    }

    # Check every `varName.prop(` call in app.js AND cross-module destructured controller params
    bad_calls = []
    for var_name, factory_name in bindings.items():
        allowed = factory_methods[factory_name]
        for m in re.finditer(rf"\b{re.escape(var_name)}\??\.([A-Za-z_$][\w$]*)\s*\(", app_src):
            method = m.group(1)
            if method not in allowed:
                line_no = app_src[: m.start()].count("\n") + 1
                bad_calls.append(f"app.js:{line_no} calls {var_name}.{method}() not exported by {factory_name}")
    cross_checked = len(bindings)
    for js_file in sorted(js_dir.glob("*.js")):
        if js_file.name == "app.js":
            continue
        src = js_file.read_text(encoding="utf-8")
        for m_sig in re.finditer(r"export\s+function\s+create\w+\s*\([^{]*\{([^}]+)\}", src):
            params = {tok.strip().split("=")[0].split(":")[0].strip() for tok in m_sig.group(1).split(",")}
            for p in params:
                factory_name = bindings_by_canonical.get(p)
                if not factory_name or factory_name not in factory_methods:
                    continue
                cross_checked += 1
                allowed = factory_methods[factory_name]
                for mc in re.finditer(rf"(?<![.\w$]){re.escape(p)}\??\.([A-Za-z_$][\w$]*)\s*\(", src):
                    method = mc.group(1)
                    if method not in allowed:
                        line_no = src[: mc.start()].count("\n") + 1
                        bad_calls.append(f"{js_file.name}:{line_no} calls {p}.{method}() not exported by {factory_name}")

    if bad_calls:
        raise RuntimeError("JS cross-module call mismatch:\n  " + "\n  ".join(bad_calls))

    # 2. Check every document.getElementById('...') in JS files matches its SCOPED HTML document
    index_html = (FUI_PUBLIC / "index.html").read_text(encoding="utf-8")
    mobile_html = (FUI_PUBLIC / "mobile.html").read_text(encoding="utf-8") if (FUI_PUBLIC / "mobile.html").is_file() else ""
    status_html = (FUI_PUBLIC / "status.html").read_text(encoding="utf-8") if (FUI_PUBLIC / "status.html").is_file() else ""
    dyn_by_scope: dict[str, set[str]] = {"index": set(), "mobile": set(), "status": set()}
    for jf in sorted(js_dir.rglob("*.js")):
        sc = "mobile" if jf.name == "mobile.js" else ("status" if jf.name == "status.js" else "index")
        dyn_by_scope[sc].update(re.findall(r'\bid=["\']([a-zA-Z0-9_-]+)["\']', jf.read_text(encoding="utf-8")))
    html_ids_by_scope = {
        "index": set(re.findall(r'\bid=["\']([^"\']+)["\']', index_html)) | dyn_by_scope["index"],
        "mobile": set(re.findall(r'\bid=["\']([^"\']+)["\']', mobile_html)) | dyn_by_scope["mobile"],
        "status": set(re.findall(r'\bid=["\']([^"\']+)["\']', status_html)) | dyn_by_scope["status"],
    }

    # Explicit allowlist for intentionally optional progressive DOM hooks
    optional_dom_ids = {"set-badge-model-file", "set-badge-model-btn"}
    missing_ids = []
    for js_file in sorted(js_dir.rglob("*.js")):
        scope = "mobile" if js_file.name == "mobile.js" else ("status" if js_file.name == "status.js" else "index")
        known_ids = html_ids_by_scope[scope]
        src = js_file.read_text(encoding="utf-8")
        for m in re.finditer(r"document\.getElementById\(\s*['\"]([a-zA-Z0-9_-]+)['\"]\s*\)", src):
            dom_id = m.group(1)
            if dom_id not in known_ids and dom_id not in optional_dom_ids:
                line_no = src[: m.start()].count("\n") + 1
                missing_ids.append(f"{js_file.name}:{line_no} references unknown DOM id #{dom_id}")
    if missing_ids:
        raise RuntimeError("Missing HTML DOM IDs:\n  " + "\n  ".join(missing_ids))

    # 3. Check every static /api/... endpoint referenced in fui/public/js/**/*.js matches an ACTUAL registered backend route regex
    route_paths = [
        ROOT / p for p in ("serve.py", "console_settings.py") if (ROOT / p).is_file()
    ] + sorted(ROOT.glob("*_routes.py"))
    route_regexes: list[re.Pattern[str]] = []
    for rp in route_paths:
        rsrc = rp.read_text(encoding="utf-8")
        vars_map = dict(re.findall(r"^\s*([a-zA-Z_]\w*)\s*=\s*['\"](/api[^'\"]*)['\"]", rsrc, re.M))
        for m_rt in re.finditer(r"web\.(?:get|post|put|delete|patch)\(\s*(?:([a-zA-Z_]\w*)\s*\+\s*)?r?['\"]([^'\"]+)['\"]", rsrc):
            prefix_var, raw_pat = m_rt.group(1), m_rt.group(2)
            full_pat = (vars_map.get(prefix_var, "") + raw_pat) if prefix_var else raw_pat
            if not full_pat.startswith("/api/"):
                continue
            # Expand {name:regex} / {name} placeholders cleanly into non-capturing regex groups
            rx_parts = ["^"]
            pos = 0
            for ph in re.finditer(r"\{([a-zA-Z_]\w*)(?::([^}]+))?\}", full_pat):
                rx_parts.append(re.escape(full_pat[pos:ph.start()]))
                inner = ph.group(2) if ph.group(2) else "[^/]+"
                rx_parts.append(f"(?:{inner})")
                pos = ph.end()
            rx_parts.append(re.escape(full_pat[pos:]))
            rx_parts.append("$")
            route_regexes.append(re.compile("".join(rx_parts)))

    base_prefixes = {"/api/console-settings"}
    missing_routes = []
    scan_sources = [(jf.name, jf.read_text(encoding="utf-8")) for jf in sorted(js_dir.rglob("*.js"))]
    if status_html:
        scan_sources.append(("status.html", status_html))
    for src_name, src in scan_sources:
        for m in re.finditer(r"['\"`](/api/[a-zA-Z0-9/_-]+)['\"`]", src):
            ep = m.group(1)
            if ep.endswith("/") or ep in base_prefixes:
                continue
            if any(rx.match(ep) for rx in route_regexes):
                continue
            line_no = src[: m.start()].count("\n") + 1
            missing_routes.append(f"{src_name}:{line_no} calls unknown API route {ep}")
    if missing_routes:
        raise RuntimeError("Unknown /api/* routes:\n  " + "\n  ".join(missing_routes))

    return {
        "factories_checked": cross_checked,
        "html_ids_indexed": sum(len(v) for v in html_ids_by_scope.values()),
        "routes_compiled": len(route_regexes),
        "bad_calls": 0,
        "missing_ids": 0,
        "missing_routes": 0,
    }


def check_i18n_parity():
    script = (
        "import { MESSAGES } from './fui/public/js/i18n.js';"
        "const en = Object.keys(MESSAGES.en).sort();"
        "const zh = Object.keys(MESSAGES['zh-CN']).sort();"
        "const missZh = en.filter(k => !(k in MESSAGES['zh-CN']));"
        "const missEn = zh.filter(k => !(k in MESSAGES.en));"
        "console.log(JSON.stringify({ en: en.length, zh: zh.length, missZh, missEn }));"
        "if (missZh.length || missEn.length) process.exit(2);"
    )
    res = run_cmd(["node", "--input-type=module", "-e", script], label="i18n EN/ZH parity")
    return json.loads(res["tail"])


def run_stage_static():
    print("\n=== STAGE 1: STATIC & CROSS-MODULE CONTRACT AUDIT ===")
    results = {}

    # 1. py_compile
    py_files = collect_core_py_files()
    for pf in py_files:
        py_compile.compile(str(pf), doraise=True)
    results["py_compile"] = f"{len(py_files)} files OK"
    print(f"  [PASS] py_compile ({len(py_files)} core Python files)")

    # 2. node --check on all frontend JS + static/speech-delivery.js
    js_files = sorted((FUI_PUBLIC / "js").rglob("*.js")) + [ROOT / "static" / "speech-delivery.js"]
    for jf in js_files:
        run_cmd(["node", "--check", str(jf)], label=f"node --check {jf.name}")
    results["node_check"] = f"{len(js_files)} JS files OK"
    print(f"  [PASS] node --check ({len(js_files)} JS files)")

    # 3. i18n EN/ZH exact key parity
    i18n_info = check_i18n_parity()
    results["i18n"] = i18n_info
    print(f"  [PASS] i18n parity (EN={i18n_info['en']}, ZH={i18n_info['zh']}, diff=0)")

    # 4. Cross-module JS contract + DOM ID + API route audit
    contract_info = audit_js_contracts()
    results["js_contracts"] = contract_info
    print(f"  [PASS] JS contracts ({contract_info['factories_checked']} controllers, {contract_info['html_ids_indexed']} DOM IDs, 0 broken calls/routes)")

    # 5. fui_lint.py (0 blocker / 0 major / 0 minor — mandatory!)
    if not FUI_LINT.is_file():
        raise RuntimeError(f"Required fui_lint.py not found at {FUI_LINT}")
    lint_proc = subprocess.run(
        [sys.executable, str(FUI_LINT), str(FUI_PUBLIC)],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    lint_out = (lint_proc.stdout + "\n" + lint_proc.stderr).strip()
    if lint_proc.returncode != 0 or not re.search(r"0 blocker\s*[·•]\s*0 major\s*[·•]\s*0 minor", lint_out):
        raise RuntimeError(f"fui_lint failed or non-zero findings:\n{lint_out}")
    results["fui_lint"] = lint_out.splitlines()[-1]
    print(f"  [PASS] fui_lint ({results['fui_lint']})")

    # 6. 3D model certification guard
    r_model = run_cmd([sys.executable, "check_model_release.py"], label="check_model_release")
    results["model_release"] = r_model["tail"] or "PASS"
    print("  [PASS] check_model_release (h50-oled-qa-v5c intact)")
    return results


# ── Stage 2: Unit & Logic Regressions ─────────────────────────────────────────

def run_v53_unit_checks():
    """Verify every v5.3 unit behavior directly (echo strip, cloud retry, onset trim, 5 gestures, CPU debounce)."""
    sys.path.insert(0, str(ROOT))
    from assistant_brain import strip_echoed_user_prefix
    import holo_kernel
    from openai import APITimeoutError

    # 2a. Echo-prefix stripper
    assert strip_echoed_user_prefix("今儿总算下班了，聊两句", "今儿总算下班了，聊两句\n\n辛苦一天了，歇会儿吧。") == "辛苦一天了，歇会儿吧。"
    assert strip_echoed_user_prefix("明儿天气咋样？", "明儿天气咋样\n明天多云，12到21度。") == "明天多云，12到21度。"
    assert strip_echoed_user_prefix("嗯", "嗯\n我在听。") == "嗯\n我在听。"
    assert strip_echoed_user_prefix("明儿北京天气", "明天北京多云，12到21度。") == "明天北京多云，12到21度。"

    # 2b. Cloud silent 1-retry (before yield -> retries; after yield -> no retry; local -> no retry)
    class _FakeChoice:
        def __init__(self, text, finish=None):
            self.delta = type("D", (), {"content": text, "tool_calls": None})()
            self.finish_reason = finish

    class _FakeChunk:
        def __init__(self, text, finish=None):
            self.choices = [_FakeChoice(text, finish)]

    class _FakeStream:
        def __init__(self, items, fail_after=None):
            self._items = items
            self._fail_after = fail_after
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        def __aiter__(self):
            return self._gen()
        async def _gen(self):
            for i, it in enumerate(self._items):
                yield it
                if self._fail_after is not None and i + 1 >= self._fail_after:
                    raise APITimeoutError(request=None)

    async def _test_retries():
        old_backoff = holo_kernel.CLOUD_RETRY_BACKOFF_SECONDS
        holo_kernel.CLOUD_RETRY_BACKOFF_SECONDS = 0.01
        try:
            # Case 1: cloud fails once before first chunk -> recovers cleanly on attempt 2
            calls = [0]
            async def create_recover(**kw):
                calls[0] += 1
                if calls[0] == 1:
                    raise APITimeoutError(request=None)
                return _FakeStream([_FakeChunk("收到，没问题。", "stop")])
            fake_client = type("C", (), {"chat": type("Ch", (), {"completions": type("Co", (), {"create": staticmethod(create_recover)})()})()})()
            ans = await holo_kernel.run_holo(
                client=fake_client, provider_id="kimi", model="kimi-k2.6",
                system="sys", history=[], text="hi", tools=[], dispatch=None,
            )
            assert ans == "收到，没问题。" and calls[0] == 2, f"Expected retry recovery, got ans={ans}, calls={calls[0]}"

            # Case 2: cloud fails AFTER yielding 1 chunk -> must NOT retry/duplicate
            calls2 = [0]
            async def create_midfail(**kw):
                calls2[0] += 1
                return _FakeStream([_FakeChunk("前半句", None)], fail_after=1)
            fake_client2 = type("C", (), {"chat": type("Ch", (), {"completions": type("Co", (), {"create": staticmethod(create_midfail)})()})()})()
            try:
                await holo_kernel.run_holo(
                    client=fake_client2, provider_id="kimi", model="kimi-k2.6",
                    system="sys", history=[], text="hi", tools=[], dispatch=None,
                )
                raise AssertionError("Expected RuntimeError after mid-stream timeout")
            except RuntimeError:
                assert calls2[0] == 1, f"Must not retry after yielding tokens! calls={calls2[0]}"

            # Case 3: local model -> max_attempts=1 (no retry)
            calls3 = [0]
            async def create_local_fail(**kw):
                calls3[0] += 1
                raise APITimeoutError(request=None)
            fake_client3 = type("C", (), {"chat": type("Ch", (), {"completions": type("Co", (), {"create": staticmethod(create_local_fail)})()})()})()
            try:
                await holo_kernel.run_holo(
                    client=fake_client3, provider_id="local", model="qwen35-4b",
                    system="sys", history=[], text="hi", tools=[], dispatch=None,
                )
                raise AssertionError("Expected RuntimeError for local timeout")
            except RuntimeError:
                assert calls3[0] == 1, f"Local model must not retry! calls={calls3[0]}"
        finally:
            holo_kernel.CLOUD_RETRY_BACKOFF_SECONDS = old_backoff

    asyncio.run(_test_retries())

    # 2c. Sustained-onset silence trim (tested in OmniVoice's real Python runtime where torch/numpy live)
    omni_py = Path(os.environ.get("VOICESTUDIO_ROOT", r"E:\VoiceStudio")) / r"project\.venv\Scripts\python.exe"
    onset_script = (
        "import sys, numpy as np;"
        f"sys.path.insert(0, {str(ROOT)!r});"
        "from voice.clone_omnivoice import smooth_conversational_pauses;"
        "sr = 24000; pcm = np.zeros(int(sr * 0.8), dtype=np.float32);"
        "t_click = np.arange(int(sr * 0.010), dtype=np.float32);"
        "pcm[int(sr * 0.02):int(sr * 0.02)+len(t_click)] = 0.08 * np.sin(2 * np.pi * 400 * t_click / sr);"
        "t_speech = np.arange(int(sr * 0.40), dtype=np.float32);"
        "pcm[int(sr * 0.27):int(sr * 0.27)+len(t_speech)] = 0.25 * np.sin(2 * np.pi * 220 * t_speech / sr);"
        "trimmed = smooth_conversational_pauses(pcm, sr, warm=True);"
        "hop = int(sr * 0.01);"
        "first_loud = next((i * 0.01 for i in range(len(trimmed) // hop) "
        "if float(np.sqrt(np.mean(trimmed[i * hop:(i + 1) * hop] ** 2))) > 0.05), 9.9);"
        "assert first_loud <= 0.06, f'Pre-breath click leaked dead lead-in: {first_loud:.3f}s';"
        "print(f'{first_loud:.3f}')"
    )
    if omni_py.is_file():
        onset_res = run_cmd([str(omni_py), "-c", onset_script], label="sustained-onset trim (OmniVoice venv)")
        onset_label = f"first_loud={float(onset_res['tail'].strip()):.3f}s"
    else:
        onset_label = "skipped (no VoiceStudio)"

    # 2d. JS unit checks: 5-gesture classifier + live CPU 3-sample alert debounce
    js_unit = r"""
import { classifyGesture } from './fui/public/js/gestures.js';
import { computeDebouncedCpuState } from './fui/public/js/telemetry.js';

function makeHand({ idx, mid, ring, pinky, pinch = false }) {
  const pts = Array.from({ length: 21 }, () => ({ x: 0.5, y: 0.5 }));
  pts[0] = { x: 0.5, y: 0.85 }; // wrist
  pts[9] = { x: 0.5, y: 0.60 }; // palm = 0.25
  // thumb
  pts[4] = pinch ? { x: 0.44, y: 0.34 } : { x: 0.22, y: 0.62 };
  const setFinger = (pip, tip, x, ext) => {
    pts[pip] = { x, y: 0.55 };
    pts[tip] = ext ? { x, y: 0.32 } : { x, y: 0.66 };
  };
  setFinger(6, 8, 0.43, idx);
  if (pinch) pts[8] = { x: 0.45, y: 0.35 };
  setFinger(10, 12, 0.50, mid);
  setFinger(14, 16, 0.57, ring);
  setFinger(18, 20, 0.64, pinky);
  return pts;
}

const gPoint = classifyGesture(makeHand({ idx: true, mid: false, ring: false, pinky: false }));
const gPeace = classifyGesture(makeHand({ idx: true, mid: true, ring: false, pinky: false }));
const gRail  = classifyGesture(makeHand({ idx: true, mid: true, ring: true, pinky: false }));
const gFist  = classifyGesture(makeHand({ idx: false, mid: false, ring: false, pinky: false }));
const gPalm  = classifyGesture(makeHand({ idx: true, mid: true, ring: true, pinky: true }));
const gPinch = classifyGesture(makeHand({ idx: true, mid: true, ring: true, pinky: true, pinch: true }));
if (gPoint !== 'point' || gPeace !== 'peace' || gRail !== 'rail' || gFist !== 'fist' || gPalm !== 'palm' || gPinch !== 'pinch') {
  throw new Error('Gesture mismatch: ' + JSON.stringify({ gPoint, gPeace, gRail, gFist, gPalm, gPinch }));
}

// Live CPU 3-sample debounce
let st = { warnStreak: 0, critStreak: 0 };
const s1 = computeDebouncedCpuState(95, st, { live: true }); st = s1;
const s2 = computeDebouncedCpuState(96, st, { live: true }); st = s2;
const s3 = computeDebouncedCpuState(94, st, { live: true }); st = s3;
const s4 = computeDebouncedCpuState(45, st, { live: true });
const sim1 = computeDebouncedCpuState(95, {}, { live: false });
if (s1.state !== 'idle' || s2.state !== 'idle' || s3.state !== 'crit' || s4.state !== 'idle' || sim1.state !== 'crit') {
  throw new Error('CPU debounce mismatch: ' + JSON.stringify({ s1, s2, s3, s4, sim1 }));
}
console.log('V53_JS_UNITS_OK');
"""
    run_cmd(["node", "--input-type=module", "-e", js_unit], label="v5.3 JS units (5 gestures + CPU debounce)")
    return {"echo_strip": "PASS", "cloud_retry": "PASS", "onset_trim": onset_label, "gestures_5": "PASS", "cpu_debounce": "PASS"}


def run_stage_unit():
    print("\n=== STAGE 2: UNIT & LOGIC REGRESSIONS ===")
    results = {}
    results["spoken_numbers"] = run_cmd([sys.executable, "verify_spoken_numbers.py"], label="verify_spoken_numbers")
    print(f"  [PASS] verify_spoken_numbers ({results['spoken_numbers']['seconds']}s)")

    results["dialogue_quality"] = run_cmd([sys.executable, "verify_dialogue_quality.py", "--mode", "unit"], label="verify_dialogue_quality --mode unit")
    print(f"  [PASS] verify_dialogue_quality --mode unit ({results['dialogue_quality']['seconds']}s)")

    results["erhua_unit"] = run_cmd([sys.executable, "verify_multimodel_erhua_tone.py", "--models", "", "--no-audio", "--tag", "gate-unit"], label="verify_multimodel_erhua_tone unit")
    print(f"  [PASS] verify_multimodel_erhua_tone unit ({results['erhua_unit']['seconds']}s)")

    results["daily_golden_260"] = run_cmd([sys.executable, "verify_daily_golden_200.py", "--tier", "full", "--tag", "gate"], label="verify_daily_golden_200 (260 samples)")
    print(f"  [PASS] verify_daily_golden_200 (260/260 samples, {results['daily_golden_260']['seconds']}s)")

    results["brain_cognitive_138"] = run_cmd([sys.executable, "verify_brain_cognitive_suite.py"], label="verify_brain_cognitive_suite (138 samples)")
    print(f"  [PASS] verify_brain_cognitive_suite (138/138 samples, {results['brain_cognitive_138']['seconds']}s, {results['brain_cognitive_138']['tail']})")

    results["v53_units"] = run_v53_unit_checks()
    print(f"  [PASS] v5.3 unit checks ({results['v53_units']})")

    results["v55_units"] = run_cmd([sys.executable, "verify_v55_units.py"], label="verify_v55_units")
    print(f"  [PASS] verify_v55_units ({results['v55_units']['seconds']}s, {results['v55_units']['tail']})")

    results["soul_memory"] = run_cmd([sys.executable, "verify_soul_memory.py"], label="verify_soul_memory.py")
    print(f"  [PASS] verify_soul_memory ({results['soul_memory']['seconds']}s, {results['soul_memory']['tail']})")

    results["online_music"] = run_cmd([sys.executable, "verify_online_music.py"], label="verify_online_music.py")
    print(f"  [PASS] verify_online_music ({results['online_music']['seconds']}s, {results['online_music']['tail']})")

    results["gesture_replay"] = run_cmd(["node", "verify_gesture_replay.mjs"], label="verify_gesture_replay.mjs")
    print(f"  [PASS] verify_gesture_replay ({results['gesture_replay']['seconds']}s, {results['gesture_replay']['tail']})")

    results["fui_voice_longchat"] = run_cmd([sys.executable, "verify_fui_voice_longchat.py"], label="verify_fui_voice_longchat.py")
    print(f"  [PASS] verify_fui_voice_longchat ({results['fui_voice_longchat']['seconds']}s, {results['fui_voice_longchat']['tail']})")
    return results


# ── Stage 3: Speech Parity + Real RTX 3070 Ti Warm Pre-Warm Audio Check ───────

def run_warm_gpu_check():
    """Verify OmniVoice primes warm_prompt on CUDA and cuts dead lead-in to <=0.12s on first warm utterance."""
    sys.path.insert(0, str(ROOT))
    import io
    import wave
    import numpy as np
    from voice_output import MossSpeech, SESSION_CUDA_MIN_FREE_MIB, SESSION_MIN_AVAILABLE, memory_sample

    async def _run():
        with tempfile.TemporaryDirectory(prefix="moss-gate-warm-") as td:
            sp = MossSpeech(Path(td), idle_seconds=30, standby_seconds=0)
            try:
                ok = await sp.prepare_session("gate", eligible=lambda: True, ttl_seconds=60, prime=True)
                if not ok:
                    await asyncio.sleep(2.0)
                    ok = await sp.prepare_session("gate", eligible=lambda: True, ttl_seconds=60, prime=True)
                if not ok:
                    m = memory_sample()
                    raise RuntimeError(
                        f"prepare_session(prime=True) failed on CUDA "
                        f"(avail_ram={m.available}, avail_pf={m.available_pagefile}, need_ram={SESSION_MIN_AVAILABLE}, need_vram_mib={SESSION_CUDA_MIN_FREE_MIB})"
                    )
                t0 = perf_counter()
                timing = {}
                wav_bytes = await sp.render(
                    "辛苦一天了，先歇会儿吧，有事儿慢慢聊。",
                    lambda: False,
                    category="warm",
                    timings=timing,
                    room_amount=100,
                )
                warm_s = round(perf_counter() - t0, 3)
                with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
                    sr = wf.getframerate()
                    ch = wf.getnchannels()
                    raw = wf.readframes(wf.getnframes())
                pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                mono = pcm.reshape(-1, ch).mean(axis=1) if ch > 1 else pcm
                hop = int(sr * 0.01)
                rms = np.array([float(np.sqrt(np.mean(mono[i * hop:(i + 1) * hop] ** 2))) for i in range(len(mono) // hop)])
                peak = float(np.max(rms)) if rms.size else 0.0
                thr = max(0.008, peak * 0.08)
                voiced = np.where(rms >= thr)[0]
                head_s = round(int(voiced[0]) * 0.01, 3) if voiced.size else 9.9
                # Measure longest interior silent run
                max_pause = 0.0
                if voiced.size > 2:
                    run_len = 0
                    for idx in range(int(voiced[0]), int(voiced[-1])):
                        if rms[idx] < thr:
                            run_len += 1
                            max_pause = max(max_pause, run_len * 0.01)
                        else:
                            run_len = 0
                if head_s > 0.12:
                    raise RuntimeError(f"Warm first-clip lead silence too long: head_s={head_s}s > 0.12s")
                if max_pause > 0.45:
                    raise RuntimeError(f"Warm clip interior pause too long: max_pause={max_pause:.2f}s > 0.45s")
                return {"warm_s": warm_s, "head_s": head_s, "max_pause_s": round(max_pause, 3), "duration_s": round(len(mono) / sr, 2), "timing": timing}
            finally:
                await sp.close()

    return asyncio.run(_run())


def run_stage_audio():
    print("\n=== STAGE 3: SPEECH LATENCY PARITY & REAL RTX 3070 Ti WARM CHECK ===")
    results = {}
    parity_res = run_cmd(
        [sys.executable, "verify_fui_speech_latency.py", "--mode", "parity", "--tag", "v53-gate"],
        label="verify_fui_speech_latency --mode parity",
    )
    if "passed= True" not in parity_res["tail"]:
        raise RuntimeError(f"verify_fui_speech_latency parity did NOT pass: {parity_res['tail']}")
    results["speech_parity"] = parity_res
    print(f"  [PASS] verify_fui_speech_latency --mode parity (9/9, {results['speech_parity']['seconds']}s)")

    results["warm_gpu"] = run_warm_gpu_check()
    print(f"  [PASS] RTX 3070 Ti warm pre-warm + onset trim ({results['warm_gpu']})")
    return results


# ── Stage 4: Live Edge Full-Click QA + Pixel-Sanity Audit on All 8 Logos ─────

def analyze_intel_logo_pixels(report_path: Path):
    """Inspect every rendered Intel Dossier logo PNG to reject BOTH blank black boxes AND solid white blobs."""
    import numpy as np
    from PIL import Image

    data = json.loads(report_path.read_text(encoding="utf-8"))
    logos = data.get("intelLogos", [])
    if len(logos) != 8:
        raise RuntimeError(f"Expected 8 Intel dossier logos, got {len(logos)}")

    audit = []
    failures = []
    for item in logos:
        img = Image.open(item["file"]).convert("RGB")
        arr = np.asarray(img, dtype=np.float32)
        # Luminance [0..255]
        luma = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
        # Background is either dark (median < 40) or light plate (median > 200)
        bg_luma = float(np.median(luma))
        fg_mask = np.abs(luma - bg_luma) > 35.0
        fg_frac = float(np.mean(fg_mask))

        # Interior edge gradient density: real emblems/wordmarks have rich strokes; solid white circles do not!
        dx = np.abs(np.diff(luma, axis=1, prepend=luma[:, :1]))
        dy = np.abs(np.diff(luma, axis=0, prepend=luma[:1, :]))
        edge_mask = (dx + dy) > 35.0
        fg_pixels = int(np.sum(fg_mask))
        edge_pixels = int(np.sum(edge_mask))
        edge_to_fg = round(edge_pixels / max(1, fg_pixels), 3)

        # Reject blank (<1.2% foreground) OR full-bleed solid (>85%) OR flat solid circle/rect blob (edge_to_fg < 0.09)
        ok = (0.012 <= fg_frac <= 0.85) and (edge_to_fg >= 0.09)
        entry = {
            "dossier": item["dossier"],
            "slide": item["slide"],
            "kind": item["kind"],
            "fg_pct": round(fg_frac * 100, 2),
            "edge_to_fg": edge_to_fg,
            "ok": ok,
        }
        audit.append(entry)
        if not ok:
            failures.append(f"{item['dossier']}#0{item['slide']} ({item['src']}): fg_pct={entry['fg_pct']}%, edge_to_fg={edge_to_fg}")
    if failures:
        raise RuntimeError("Intel logo pixel-sanity audit FAILED (blank or flat solid blob detected):\n  " + "\n  ".join(failures))
    return audit


def _pick_free_port() -> int:
    from testkit import safe_free_port

    return safe_free_port()


def _run_stage_ui_against(base_url: str, out_dir: Path, *, mode_label: str):
    print(f"\n=== STAGE 4: EDGE FULL-CLICK QA & 8-LOGO PIXEL-SANITY ({mode_label}: {base_url}) ===")
    out_dir.mkdir(parents=True, exist_ok=True)
    ui_res = run_cmd(
        ["node", "verify_v53_gate_ui.mjs"],
        env={"MOSS_URL": base_url, "OUT_DIR": str(out_dir)},
        label="verify_v53_gate_ui.mjs",
    )
    print(f"  [PASS] verify_v53_gate_ui.mjs ({ui_res['seconds']}s)")

    logo_audit = analyze_intel_logo_pixels(out_dir / "ui_gate_report.json")
    for row in logo_audit:
        print(f"  [PASS] logo {row['dossier']}#0{row['slide']}: fg={row['fg_pct']}%, edge/fg={row['edge_to_fg']} ({row['kind']})")
    return {"mode": mode_label, "url": base_url, "ui": ui_res, "logo_pixel_audit": logo_audit, "out_dir": str(out_dir)}


def _seed_isolated_ui_media(data_dir: Path) -> Path:
    """Create a hermetic SQLite + local WAV catalog for isolated Stage 4 UI checks."""
    import hashlib
    import wave
    from local_media.library import MediaLibrary

    media_dir = data_dir / "media_samples"
    media_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "media.sqlite3"
    tracks = [
        ("缝纫机乐队", "都选C", "frj_duoxuanc.wav"),
        ("屠洪刚", "精忠报国", "tu_jingzhong.wav"),
        ("刀郎", "西海情歌", "dao_xihai.wav"),
        ("刀郎", "冲动的惩罚", "dao_chongdong.wav"),
        ("刘德华", "冰雨", "ldh_bingyu.wav"),
    ]
    lib = MediaLibrary(db_path)
    with lib.connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO sources(id, config, state, error, checked) VALUES(?,?,?,?,?)",
            ("local-test", json.dumps({"id": "local-test", "kind": "local", "path": str(media_dir)}), "ready", "", "2026-11-23T00:00:00Z"),
        )
        for artist, title, fname in tracks:
            wav_path = media_dir / fname
            with wave.open(str(wav_path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(b"\x00\x00" * 1600)
            st = wav_path.stat()
            searchable = f"{artist} {title} {fname}".lower()
            mid = hashlib.md5(fname.encode("utf-8")).hexdigest()
            db.execute(
                "INSERT OR REPLACE INTO media(media_id, source, rel, path, kind, title, artist, size, modified, searchable) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (mid, "local-test", fname, str(wav_path), "audio", title, artist, st.st_size, st.st_mtime_ns, searchable),
            )
    return db_path


def run_stage_ui(base_url: str, out_dir: Path):
    if base_url:
        return _run_stage_ui_against(base_url, out_dir, mode_label="explicit-url")

    import tempfile
    import time
    import urllib.request

    port = _pick_free_port()
    iso_url = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory(prefix="moss-gate-ui-", ignore_cleanup_errors=True) as td:
        media_db = _seed_isolated_ui_media(Path(td))
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["MOSS_ISOLATED_FUI"] = "1"
        env["MOSS_MEDIA_DB"] = str(media_db)
        proc = subprocess.Popen(
            [sys.executable, "-u", "serve.py", "--port", str(port), "--data", td],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            deadline = time.monotonic() + 25.0
            ready = False
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    out = proc.stdout.read() if proc.stdout else ""
                    raise RuntimeError(f"Isolated serve.py exited early ({proc.returncode}): {out[-1000:]}")
                try:
                    with urllib.request.urlopen(f"{iso_url}/api/health", timeout=1.5) as r:
                        if r.status == 200:
                            ready = True
                            break
                except Exception:
                    time.sleep(0.25)
            if not ready:
                raise RuntimeError(f"Isolated serve.py did not become healthy on {iso_url} within 25s")
            ui_result = _run_stage_ui_against(iso_url, out_dir, mode_label="isolated-muted")
            # Simulate physical drive disconnect (unplug media_samples) while live serve.py remains up,
            # verifying hud-popups.js prunes media:pending cleanly instead of hanging infinitely.
            unplugged = Path(td) / "media_samples_unplugged"
            (Path(td) / "media_samples").rename(unplugged)
            hot_unplug_js = r"""
import fs from 'node:fs';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const p = process.env.PLAYWRIGHT_PATH || 'F:\\moss\\agent-lab\\prototype\\fui\\node_modules\\playwright';
const { chromium } = require(fs.existsSync(p) ? p : 'playwright');
const browser = await chromium.launch({ channel: 'msedge', headless: true, args: ['--mute-audio', '--autoplay-policy=no-user-gesture-required'] });
try {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  const page = await ctx.newPage();
  const errs = [];
  page.on('pageerror', (e) => errs.push(String(e)));
  await page.addInitScript(() => sessionStorage.setItem('moss.agent.opened', '1'));
  await page.goto(`${process.env.MOSS_URL}/?skipOpening=1`, { waitUntil: 'domcontentloaded', timeout: 20000 });
  await page.waitForFunction(() => document.body.dataset.backend === 'live', { timeout: 15000 });
  await page.waitForTimeout(450);
  const skipBtn = page.locator('#skip-opening, #opening-skip').first();
  if (await skipBtn.isVisible({ timeout: 500 }).catch(() => false)) {
    await skipBtn.click().catch(() => {});
  }
  if (await page.locator('#dlg-badge').evaluate((el) => Boolean(el?.open)).catch(() => false)) {
    await page.locator('#badge-verify').click().catch(() => {});
  }
  await page.waitForFunction(() => !document.querySelector('#opening-overlay:not([hidden])') && !document.getElementById('dlg-badge')?.open, { timeout: 10000 });
  await page.waitForSelector('#comms-new', { timeout: 10000 });
  await page.locator('#comms-new').click();
  await page.waitForTimeout(250);
  await page.fill('#comms-input', '先来首歌吧');
  await page.locator('#comms-send').click();
  await page.waitForFunction(async () => {
    const r = await fetch('/api/state', { cache: 'no-store' });
    const st = await r.json();
    const last = st?.turns?.at(-1);
    return last && last.user === '先来首歌吧' && last.status !== 'thinking' && last.status !== 'saving';
  }, { timeout: 12000 });
  await page.waitForTimeout(350);
  const res = await page.evaluate(async () => {
    const r = await fetch('/api/state', { cache: 'no-store' });
    const st = await r.json();
    const last = st?.turns?.at(-1);
    const pendingCards = document.querySelectorAll('.moss-hud-card[data-hud-type="media"] .moss-hud-pending').length;
    const openMediaHud = document.querySelectorAll('.moss-hud-card[data-hud-type="media"]:not([hidden])').length;
    return {
      status: last?.status,
      unavailable: (last?.media?.unavailable_sources || []).length,
      items: (last?.media?.items || []).length,
      pendingCards,
      openMediaHud,
    };
  });
  if (errs.length || res.unavailable < 1 || res.items !== 0 || res.pendingCards !== 0 || res.openMediaHud !== 0) {
    throw new Error('Hot-unplug offline recovery mismatch: ' + JSON.stringify({ res, errs }));
  }
  console.log('HOT_UNPLUG_RECOVERY_OK: ' + JSON.stringify(res));
} finally {
  await browser.close();
}
"""
            unplug_res = run_cmd(["node", "--input-type=module", "-e", hot_unplug_js], env={"MOSS_URL": iso_url}, label="hot-unplug offline media recovery")
            print(f"  [PASS] {unplug_res['tail']} ({unplug_res['seconds']}s)")
            ui_result["hot_unplug_media"] = unplug_res
            return ui_result
        finally:
            try:
                inst = Path(td) / "instance.json"
                if inst.is_file():
                    tok = json.loads(inst.read_text(encoding="utf-8")).get("token", "")
                    req = urllib.request.Request(
                        f"{iso_url}/api/shutdown",
                        data=json.dumps({"token": tok}).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    urllib.request.urlopen(req, timeout=2).read()
            except Exception:
                pass
            try:
                proc.wait(timeout=6)
            except Exception:
                proc.terminate()
                try:
                    proc.wait(timeout=4)
                except Exception:
                    proc.kill()
                    proc.wait(timeout=3)
            if proc.stdout:
                try:
                    proc.stdout.close()
                except Exception:
                    pass


def main():
    parser = argparse.ArgumentParser(description="MOSS 550W Hardcore Automated Release Gate")
    parser.add_argument("--stages", default="static,unit", help="Comma-separated stages: static,unit,audio,ui,all")
    parser.add_argument("--url", default="", help="Optional existing MOSS URL for Stage 4 (empty = isolated silent temp server)")
    parser.add_argument("--tag", default="v54", help="Output run folder tag")
    args = parser.parse_args()

    raw = [s.strip().lower() for s in args.stages.split(",") if s.strip()]
    stages = ["static", "unit", "audio", "ui"] if "all" in raw else raw

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = ROOT / "runs" / f"release-gate-{args.tag}-{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)

    summary = {"tag": args.tag, "stages": stages, "started_at": stamp, "results": {}}
    started = perf_counter()
    if "static" in stages:
        summary["results"]["static"] = run_stage_static()
    if "unit" in stages:
        summary["results"]["unit"] = run_stage_unit()
    if "audio" in stages:
        summary["results"]["audio"] = run_stage_audio()
    if "ui" in stages:
        summary["results"]["ui"] = run_stage_ui(args.url, run_dir / "ui")

    summary["total_seconds"] = round(perf_counter() - started, 2)
    summary["status"] = "PASS"
    out_json = run_dir / "gate_summary.json"
    out_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n=== RELEASE GATE PASSED ({summary['total_seconds']}s) -> {out_json} ===")


if __name__ == "__main__":
    main()
