"""Exercise the staff badge and welcome on isolated Edge without live synthesis."""

import argparse
import asyncio
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from aiohttp import web
from playwright.async_api import async_playwright, expect
from pydantic import SecretStr

from browser_lab import ROOT, confirm_muted_edge
from core import Moss
from local_media.library import MediaLibrary
from model_catalog import Profile, SPECS
from serve import application
from verify_context_reply import free_port


STAGED = {
    "/": ("conversation.html", "text/html"),
    "/static/agent-terminal.js": ("agent-terminal.js", "text/javascript"),
    "/static/agent-speech.js": ("agent-speech.js", "text/javascript"),
}
EDITABLE_FONT_FILE = "TaoBaoMaiCaiTi-Regular.woff2"
EDITABLE_FONT_ASSET = f"/conversation-assets/fonts/{EDITABLE_FONT_FILE}"
EDITABLE_FONT_SHA256 = "54a6e42d6ba74d51e95d55b6a7e9227826d0a236b23069a7f7e7eccc3ec1f7f4"
EDITABLE_FONT_POSTSCRIPT = "TBMCYXT"
CHINESE_FONT_FILE = "ZiHunChuangCuHei-Regular.ttf"
CHINESE_FONT_ASSET = f"/conversation-assets/fonts/{CHINESE_FONT_FILE}"
CHINESE_FONT_SHA256 = "6207501a8ae769aaee0de1530444853b8c70a5c538fac0c5f36a8f5eaa4a1a44"
CHINESE_FONT_POSTSCRIPT = "zihunchuangcuhei_T"
SERIAL_POSTSCRIPT = "FiraSans-Regular"
NUMBER_POSTSCRIPT = "Rajdhani-Bold"
SERIAL_FONT = f"/conversation-assets/fonts/{SERIAL_POSTSCRIPT}.ttf"
NUMBER_FONT = f"/conversation-assets/fonts/{NUMBER_POSTSCRIPT}.ttf"
ARTWORK = {
    "/static/credential-card.css": ("credential-card.css", "text/css"),
    "/static/credential-card.js": ("credential-card.js", "text/javascript"),
    "/static/staff-console.js": ("staff-console.js", "text/javascript"),
    "/conversation-assets/staff-badge-print.svg": ("staff-badge-print.svg", "image/svg+xml"),
    EDITABLE_FONT_ASSET: (EDITABLE_FONT_FILE, "font/woff2"),
    CHINESE_FONT_ASSET: (CHINESE_FONT_FILE, "font/ttf"),
    SERIAL_FONT: (f"{SERIAL_POSTSCRIPT}.ttf", "font/ttf"),
    NUMBER_FONT: (f"{NUMBER_POSTSCRIPT}.ttf", "font/ttf"),
}
WELCOME = "马兆实验室主任，操作权限已确认。欢迎接入 MOSS。"
NUMBER = "IQC-017-A"
LONG_NUMBER = "Moss.2026/0921_1234567-A"
REFERENCE_NUMBER = "IQC-001"
REFERENCE_SERIAL = "BSC–IQC001 222179 MAZHAO"
REFERENCE_BARCODE = REFERENCE_SERIAL.replace("–", "-")
CUSTOM_NAME = "图恒宇"
MIXED_NAME = "马宇"
MIXED_ROLE = "实验室副主任"
LONG_NAME = "马宇图恒宇王小明李华赵宁"
LONG_ROLE = "北京航天中心量子实验室高级研究员"
# The shipped SVG retains its previous fragment IDs for the two print faces.
FRONT_PRINT = "back"
BACK_PRINT = "front"
CREDENTIAL_BACK = (BACK_PRINT, f"{BACK_PRINT}-reference-id")
PROBE = """
window.staffAudioStarts = [];
window.staffAudioEnds = 0;
const staffAudioStart = AudioBufferSourceNode.prototype.start;
AudioBufferSourceNode.prototype.start = function(...args) {
  const result = staffAudioStart.apply(this, args);
  if (this.buffer) {
    staffAudioStarts.push({frames:this.buffer.length, rate:this.buffer.sampleRate});
    this.addEventListener('ended', () => staffAudioEnds++, {once:true});
  }
  return result;
};
"""


async def open_preferences(page):
    preferences = page.locator("details.preferences")
    if not await preferences.evaluate("details => details.open"):
        await preferences.locator("summary").click()


async def open_settings(page):
    await open_preferences(page)
    await page.locator("#staff-configure").click()
    settings = page.locator("#staff-settings")
    await expect(settings).to_be_visible()
    return settings


async def submit_settings(settings):
    await settings.get_by_role("button", name="保存工牌", exact=True).click()
    await expect(settings).to_be_hidden()


async def close_badge(page):
    view = page.locator("#staff-view")
    if await view.is_visible():
        await view.get_by_role("button", name="关闭", exact=True).click()
    await expect(view).to_be_hidden()


async def close_chat(page):
    if await page.locator("#dialogue").is_visible():
        await page.locator("#close-chat").click()
        await expect(page.locator("#dialogue")).to_be_hidden()


async def replay_and_skip(page):
    await open_preferences(page)
    await page.locator("#replay-opening").click()
    skip = page.locator("#skip-opening")
    await expect(skip).to_be_visible()
    await skip.click()
    await expect(skip).to_be_hidden()
    await expect(page.locator("main[data-opening]")).to_have_count(0)
    await expect(page.locator("#staff-instrument")).to_be_visible()


async def visual_check(page, card, folder, label):
    await card.evaluate("card => Promise.all([document.fonts.ready, ...Array.from(card.querySelectorAll('img'), image => image.decode())])")
    dimensions = await card.evaluate("""card => {
      const bounds = card.getBoundingClientRect();
      const center = document.elementFromPoint(bounds.x + bounds.width/2, bounds.y + bounds.height/2);
      return {width:innerWidth,height:innerHeight,pageWidth:document.documentElement.scrollWidth,
              box:bounds.toJSON(), visible:card.contains(center)};
    }""")
    box = dimensions["box"]
    assert dimensions["pageWidth"] <= dimensions["width"] + 1 and dimensions["visible"], dimensions
    assert 0 <= box["left"] < box["right"] <= dimensions["width"] + 1, dimensions
    assert 0 <= box["top"] < box["bottom"] <= dimensions["height"] + 1, dimensions
    screenshot = folder / f"{label}.png"
    await card.screenshot(path=screenshot, animations="disabled")
    return {"geometry":dimensions, "screenshot":str(screenshot)}


async def expect_barcode(card, value, payload=None):
    barcode = card.locator("svg.credential-card__barcode[data-value]")
    await expect(barcode).to_have_count(1)
    await expect(barcode).to_have_attribute("data-badge-number", value)
    await expect(barcode).to_have_attribute("data-value", payload or (REFERENCE_BARCODE if value == REFERENCE_NUMBER else value))
    await expect(barcode.locator("path")).to_have_count(1)
    outline = await barcode.locator("path").get_attribute("d")
    assert outline, "条码路径为空"
    return outline


async def scan_preview(view, folder, label, badge_number, payload):
    toggle = view.get_by_role("button", name="放大条码", exact=True)
    preview = view.locator("#staff-scan-preview")
    carrier = view.locator("#staff-large-preview")
    await toggle.click()
    await expect(toggle).to_have_attribute("aria-pressed", "true")
    await expect(preview).to_be_visible()
    await expect(carrier).to_be_hidden()
    barcode = preview.locator("svg")
    await expect(barcode).to_have_attribute("data-badge-number", badge_number)
    await expect(barcode).to_have_attribute("data-value", payload)
    assert await barcode.evaluate("code => code.getBoundingClientRect().width >= code.viewBox.baseVal.width - .1")
    path = folder / f"scan-{label}.png"
    await preview.screenshot(path=path, animations="disabled")
    await toggle.click()
    await expect(toggle).to_have_attribute("aria-pressed", "false")
    await expect(preview).to_be_hidden()
    await expect(carrier).to_be_visible()
    return {"screenshot":str(path), "payload":payload}


async def expect_artwork(card, *symbols):
    artwork = card.locator('use[href*="/conversation-assets/staff-badge-print.svg"]')
    await expect(artwork).to_have_count(len(symbols))
    urls = [urlsplit(href) for href in await artwork.evaluate_all("nodes => nodes.map(node => node.getAttribute('href'))")]
    assert all(url.path == "/conversation-assets/staff-badge-print.svg" for url in urls), urls
    assert sorted(url.fragment for url in urls) == sorted(symbols), urls
    for symbol in symbols:
        use = await card.locator(f'use[href$="#{symbol}"]').element_handle()
        await card.page.wait_for_function("use => use.getBBox().width > 0 && use.getBBox().height > 0", arg=use)


async def expect_custom_serial(card, name):
    serial = card.locator(".credential-card__serial")
    await expect(serial).to_contain_text(NUMBER)
    await expect(serial).to_contain_text(name)
    await expect(serial).not_to_contain_text("222179 MAZHAO")


async def expect_face(view, side):
    face_label = "正面" if side == "front" else "背面"
    await expect(view.locator('[data-staff-face][aria-pressed="true"]')).to_have_attribute("data-staff-face", side)
    card = view.get_by_role("article", name=f"工牌{face_label}", exact=True)
    await expect(card).to_be_visible()
    await expect(card).to_have_attribute("data-side", side)


async def select_face(view, side):
    await view.locator(f'[data-staff-face="{side}"]').click()
    await expect_face(view, side)


async def rendered_label(page, session, root_id, side, field, value, *, container="#staff-large-preview",
                         postscript=EDITABLE_FONT_POSTSCRIPT):
    selector = f'{container} article[data-side="{side}"] .credential-card__{field}'
    node = page.locator(selector)
    await expect(node).to_have_text(value)
    await page.evaluate("document.fonts.ready")
    node_id = (await session.send("DOM.querySelector", {"nodeId":root_id, "selector":selector}))["nodeId"]
    fonts = (await session.send("CSS.getPlatformFontsForNode", {"nodeId":node_id}))["fonts"]
    expected_fonts = {postscript} if isinstance(postscript, str) else set(postscript)
    assert ({font["postScriptName"] for font in fonts} == expected_fonts and len(fonts) == len(expected_fonts)
            and all(font["isCustomFont"] and font["glyphCount"] > 0 for font in fonts)), (field, value, postscript, fonts)
    geometry = await node.evaluate("""label => {
        const fit = label.closest(".credential-card").getBoundingClientRect();
        const field = label.closest(".credential-card__stripe") || label;
        const range = document.createRange();
        range.selectNodeContents(field);
        const glyphs = Array.from(range.getClientRects(), box => box.toJSON()).filter(box => box.width);
        return {runs:glyphs.length, inCard:glyphs.length > 0 && glyphs.every(box =>
            box.left >= fit.left - 1 && box.right <= fit.right + 1 &&
            box.top >= fit.top - 1 && box.bottom <= fit.bottom + 1),
            overflowX:field.clientWidth ? field.scrollWidth - field.clientWidth : 0,
            overflowY:field.clientHeight ? field.scrollHeight - field.clientHeight : 0};
    }""")
    assert geometry["inCard"] and max(geometry["overflowX"], geometry["overflowY"]) <= 1, (value, geometry)
    return {"value":value, "fonts":fonts, "geometry":geometry}


async def expect_reference_pair(page, container):
    name = page.locator(f"{container} .credential-card__name[data-reference-pair]")
    await expect(name).to_have_text("马兆")
    raised = name.locator(":scope > span")
    await expect(raised).to_have_text("兆")
    rise = await raised.evaluate(
        """node => new DOMMatrixReadOnly(getComputedStyle(node).transform).m42 /
                    node.closest(".credential-card").getBoundingClientRect().width"""
    )
    assert -0.02 < rise < 0, rise
    return rise


async def exercise_editable_fonts(page, view, folder, width, *, chinese_font, latin_fonts):
    staff_open = page.locator("#staff-open")
    main = page.locator('#staff-preview article[data-side="front"]')
    large = view.locator("#staff-large-preview")
    configure = view.locator("#staff-view-configure")
    settings = page.locator("#staff-settings")
    keys = ("name", "number", "role")
    await configure.click()
    await expect(settings).to_be_visible()
    original = {key:await settings.locator(f"#staff-{key}").input_value() for key in keys}

    async def set_profile(profile):
        for key, value in profile.items():
            await settings.locator(f"#staff-{key}").fill(value)
        await submit_settings(settings)
        if not await view.is_visible():
            await staff_open.click()
        await expect(view).to_be_visible()

    async def expect_profile(card, profile):
        for key, value in profile.items():
            await expect(card.locator(f".credential-card__{key}")).to_have_text(value)
        return await expect_barcode(card, profile["number"])

    session = await page.context.new_cdp_session(page)
    try:
        await session.send("DOM.enable")
        await session.send("CSS.enable")
        root_id = (await session.send("DOM.getDocument"))["root"]["nodeId"]
        evidence = {}
        profiles = {
            "one_glyph": {"name":MIXED_NAME, "number":NUMBER, "role":MIXED_ROLE},
            "full_length": {"name":LONG_NAME, "number":LONG_NUMBER, "role":LONG_ROLE},
        }
        postscript = {
            key:CHINESE_FONT_POSTSCRIPT if chinese_font and key in {"name", "role"} else EDITABLE_FONT_POSTSCRIPT
            for key in ("name", "role", "number", "serial")
        }
        if latin_fonts:
            postscript.update(number=NUMBER_POSTSCRIPT, serial=(SERIAL_POSTSCRIPT, EDITABLE_FONT_POSTSCRIPT))
        for label, profile in profiles.items():
            await set_profile(profile)
            number = profile["number"]
            serial = f"BSC-{number} {profile['name']}"
            front_fields = {**profile, "serial":serial}
            barcode = await expect_profile(main, front_fields)
            await select_face(view, "front")
            await expect_artwork(large, FRONT_PRINT)
            assert await expect_barcode(large, number) == barcode
            fields = {key:await rendered_label(page, session, root_id, "front", key, value, postscript=postscript[key])
                      for key, value in front_fields.items()}
            fields["main"] = {
                key:await rendered_label(page, session, root_id, "front", key, value,
                                         container="#staff-preview", postscript=postscript[key])
                for key, value in front_fields.items()
            }
            fields["front"] = await visual_check(page, large, folder, f"badge-editable-{label}-front-{width}")
            await select_face(view, "back")
            await expect_artwork(large, BACK_PRINT)
            assert await expect_barcode(large, number) == barcode
            fields["back_number"] = await rendered_label(page, session, root_id, "back", "number", number,
                                                         postscript=postscript["number"])
            fields["back"] = await visual_check(page, large, folder, f"badge-editable-{label}-back-{width}")
            if label == "full_length":
                await close_badge(page)
                await page.reload(wait_until="domcontentloaded")
                await page.locator(".connection.online").wait_for(state="attached")
                await close_chat(page)
                assert await expect_profile(main, front_fields) == barcode
                await staff_open.click()
                await expect_face(view, "front")
                assert await expect_profile(large, front_fields) == barcode
                fields["persisted"] = True
            evidence[label] = fields
            await configure.click()
            await expect(settings).to_be_visible()
        await set_profile(original)
        return evidence
    finally:
        await session.detach()


async def exercise(browser, base, folder, report, width, assets, fixture):
    size = {"width":width, "height":900 if width == 1440 else 812}
    scale = 2 if width == 768 else 1
    context = await browser.new_context(viewport=size, device_scale_factor=scale, reduced_motion="reduce", service_workers="block")
    case = {"viewport":size, "device_scale_factor":scale, "speak_calls":[], "checks":{}}
    report["cases"][str(width)] = case
    seen = set()
    chinese_font = bool(report.get("chinese_font"))
    fixed_name = () if chinese_font else ("reference-name",)
    fixed_role = () if chinese_font else ("reference-role",)
    fixed_front = (*fixed_name, *fixed_role)
    reference_front = (FRONT_PRINT, f"{FRONT_PRINT}-reference-id", *fixed_front, "reference-serial")
    font_responses = {report[key]["url"]: [] for key in ("editable_font", "chinese_font", "serial_font", "number_font")
                      if key in report}

    async def route_request(route):
        request = route.request
        url = urlsplit(request.url)
        if url.scheme in {"http", "https"} and not request.url.startswith(base + "/"):
            report["outside"].append([url.hostname, url.path])
            await route.abort()
        elif url.path in assets:
            seen.add(url.path)
            body, mime = assets[url.path]
            await route.fulfill(body=body, content_type=mime + "; charset=utf-8")
        elif url.path == "/api/speak" and request.method == "POST":
            case["speak_calls"].append(request.post_data_json)
            await route.fulfill(body=fixture, content_type="audio/wav")
        elif url.path.startswith("/api/") and request.method not in {"GET", "HEAD"}:
            report["other_api_writes"].append([request.method, url.path])
            await route.abort()
        else:
            await route.continue_()

    try:
        await context.route("**/*", route_request)
        await context.add_init_script(PROBE)
        page = await context.new_page()
        page.set_default_timeout(10000)
        page.on("pageerror", lambda error: report["page_errors"].append(str(error)))

        def observe_font(response):
            path = urlsplit(response.url).path
            if path in font_responses:
                font_responses[path].append(response.status)

        page.on("response", observe_font)
        await page.goto(base + "/?scene=off", wait_until="domcontentloaded")
        await page.locator(".connection.online").wait_for(state="attached")
        await page.bring_to_front()
        await close_chat(page)
        instrument = page.locator("#staff-instrument")
        staff_open = page.locator("#staff-open")
        view = page.locator("#staff-view")
        preview = page.locator("#staff-preview")
        large = page.locator("#staff-large-preview")
        await expect(instrument).to_be_visible()
        await expect(staff_open).to_have_accessible_name("查看工牌正面")
        main_card = preview.get_by_role("article", name="工牌正面", exact=True)
        await expect(main_card).to_be_visible()
        await expect(main_card).to_have_attribute("data-side", "front")
        await expect_artwork(preview, *reference_front)
        reference_barcode = await expect_barcode(preview, REFERENCE_NUMBER)
        await expect(view).to_be_hidden()
        await page.wait_for_timeout(300)
        assert not case["speak_calls"], "轻量场景首次打开时自动播报了"
        case["checks"]["initial_instrument"] = True
        await instrument.screenshot(path=folder / f"badge-main-{width}.png", animations="disabled")
        if chinese_font:
            case["checks"]["main_reference_name_rise"] = await expect_reference_pair(page, "#staff-preview")

        if width == 1440:
            await staff_open.click()
            await expect(view).to_be_visible()
            await expect_face(view, "front")
            await expect_artwork(large, *reference_front)
            await expect(large.locator(".credential-card__number")).to_have_text(REFERENCE_NUMBER)
            await expect(large.locator(".credential-card__name")).to_have_text("马兆")
            await expect(large.locator(".credential-card__role")).to_have_text("实验室主任")
            await expect(large.locator(".credential-card__serial")).to_have_text(REFERENCE_SERIAL)
            if chinese_font:
                case["checks"]["dialog_reference_name_rise"] = await expect_reference_pair(page, "#staff-large-preview")
                session = await context.new_cdp_session(page)
                try:
                    await session.send("DOM.enable")
                    await session.send("CSS.enable")
                    root_id = (await session.send("DOM.getDocument"))["root"]["nodeId"]
                    case["checks"]["reference_chinese_font"] = {
                        key:await rendered_label(page, session, root_id, "front", key, value,
                                                 postscript=CHINESE_FONT_POSTSCRIPT)
                        for key, value in (("name", "马兆"), ("role", "实验室主任"))
                    }
                finally:
                    await session.detach()
            case["checks"]["default_front"] = await visual_check(page, large, folder, "badge-reference-front-1440")
            case["checks"]["reference_scan"] = await scan_preview(view, folder, "reference-1440", REFERENCE_NUMBER, REFERENCE_BARCODE)
            await select_face(view, "back")
            await expect_artwork(large, *CREDENTIAL_BACK)
            await expect(large.locator(".credential-card__number")).to_have_text(REFERENCE_NUMBER)
            await expect(large.locator(".credential-card__name")).to_have_count(0)
            case["checks"]["reference_back"] = await visual_check(page, large, folder, "badge-reference-back-1440")
            await close_badge(page)
            settings = await open_settings(page)
            original_profile = await page.evaluate("localStorage.getItem('moss.staff.profile')")
            await settings.locator("#staff-name").fill("马兆")
            await settings.locator("#staff-number").fill("工号01")
            await settings.locator("#staff-role").fill("实验室主任")
            await settings.locator("#staff-auto-welcome").check()
            await settings.get_by_role("button", name="保存工牌", exact=True).click()
            await expect(settings).to_be_visible()
            assert not await settings.locator("#staff-number").evaluate("input => input.checkValidity()") or \
                await settings.locator("#staff-save-feedback").is_visible(), "非法编号未明确提示"
            assert await page.evaluate("localStorage.getItem('moss.staff.profile')") == original_profile
            await settings.locator("#staff-number").fill(NUMBER)
            await submit_settings(settings)
            await expect_artwork(preview, FRONT_PRINT, *fixed_front)
            custom_barcode = await expect_barcode(preview, NUMBER)
            assert custom_barcode != reference_barcode, "更换编号后条码图案未更新"
            await staff_open.click()
            await expect(view).to_be_visible()
            await expect_face(view, "front")
            await expect(large).to_contain_text("马兆")
            await expect(large).to_contain_text("实验室主任")
            await expect_artwork(large, FRONT_PRINT, *fixed_front)
            await expect_custom_serial(large, "马兆")
            assert await expect_barcode(large, NUMBER) == custom_barcode
            await view.locator("#staff-view-configure").click()
            await expect(settings).to_be_visible()
            await settings.locator("#staff-name").fill("<em>马兆</em>")
            await submit_settings(settings)
            if not await view.is_visible():
                await staff_open.click()
            await select_face(view, "front")
            await expect(large).to_contain_text("<em>马兆</em>")
            await expect(large.locator("em", has_text="马兆")).to_have_count(0)
            await expect_artwork(large, FRONT_PRINT, *fixed_role)
            await view.locator("#staff-view-configure").click()
            await expect(settings).to_be_visible()
            await settings.locator("#staff-name").fill(CUSTOM_NAME)
            await submit_settings(settings)
            await close_badge(page)
            await page.reload(wait_until="domcontentloaded")
            await page.locator(".connection.online").wait_for(state="attached")
            await close_chat(page)
            await expect_artwork(preview, FRONT_PRINT, *fixed_role)
            assert await expect_barcode(preview, NUMBER) == custom_barcode
            await staff_open.click()
            await expect(view).to_be_visible()
            await expect_face(view, "front")
            await expect_artwork(large, FRONT_PRINT, *fixed_role)
            await expect(large.locator(".credential-card__name")).to_have_text(CUSTOM_NAME)
            await expect_custom_serial(large, CUSTOM_NAME)
            assert await expect_barcode(large, NUMBER) == custom_barcode
            await page.evaluate("document.fonts.ready")
            assert await large.locator(".credential-card__name").evaluate(
                "name => name.scrollWidth <= name.clientWidth + 1"), "三字姓名被截断"
            case["checks"]["custom_persisted"] = await visual_check(page, large, folder, "badge-custom-front-1440")
            await view.locator("#staff-view-configure").click()
            await expect(settings).to_be_visible()
            for key, value in (("name", CUSTOM_NAME), ("number", NUMBER), ("role", "实验室主任")):
                await expect(settings.locator(f"#staff-{key}")).to_have_value(value)
            await expect(settings.locator("#staff-auto-welcome")).to_be_checked()
            await settings.locator("#staff-name").fill("马兆")
            await submit_settings(settings)
            await close_badge(page)
            case["checks"]["persisted_and_escaped"] = True

        await staff_open.click()
        await expect(view).to_be_visible()
        await expect_face(view, "front")
        displayed_number = NUMBER if width == 1440 else REFERENCE_NUMBER
        await expect_barcode(large, displayed_number)
        await expect_artwork(large, *((FRONT_PRINT, *fixed_front) if width == 1440 else reference_front))
        case["checks"]["front"] = await visual_check(page, large, folder, f"badge-front-{width}")
        await select_face(view, "back")
        await expect_barcode(large, displayed_number)
        await expect_artwork(large, *((BACK_PRINT,) if width == 1440 else CREDENTIAL_BACK))
        case["checks"]["back"] = await visual_check(page, large, folder, f"badge-back-{width}")
        await select_face(view, "front")
        payload = NUMBER if width == 1440 else REFERENCE_BARCODE
        case["checks"]["scan"] = await scan_preview(view, folder, str(width), displayed_number, payload)
        if report.get("editable_font"):
            evidence = await exercise_editable_fonts(page, view, folder, width, chinese_font=chinese_font,
                                                     latin_fonts="serial_font" in report and "number_font" in report)
            assert all(200 in statuses for statuses in font_responses.values()), font_responses
            evidence["font_http_statuses"] = font_responses
            case["checks"]["editable_fonts"] = evidence
        if width == 375:
            await view.locator("#staff-view-configure").click()
            settings = page.locator("#staff-settings")
            await settings.locator("#staff-number").fill(LONG_NUMBER)
            await submit_settings(settings)
            await expect_barcode(large, LONG_NUMBER)
            case["checks"]["long_scan"] = await scan_preview(view, folder, "long-375", LONG_NUMBER, LONG_NUMBER)

        if width == 1440:
            assert not case["speak_calls"], "重新加载或编辑时自动播报了"
            async with page.expect_response(lambda response: urlsplit(response.url).path == "/api/speak") as response:
                await view.locator("#staff-view-welcome").click()
            assert (await response.value).ok
            await page.wait_for_function("staffAudioStarts.length === 1")
            assert len(case["speak_calls"]) == 1
            assert case["speak_calls"][0]["text"] == WELCOME
            assert case["speak_calls"][0]["category"] == "reply"
            await page.wait_for_function("staffAudioEnds === 1")
            await close_badge(page)
            async with page.expect_response(lambda response: urlsplit(response.url).path == "/api/speak") as response:
                await replay_and_skip(page)
            assert (await response.value).ok
            await page.wait_for_function("staffAudioStarts.length === 2")
            await page.wait_for_function("staffAudioEnds === 2")
            assert [row["text"] for row in case["speak_calls"]] == [WELCOME, WELCOME], case
            case["checks"]["manual_and_intro_audio"] = await page.evaluate("staffAudioStarts")

            await close_badge(page)
            settings = await open_settings(page)
            await settings.locator("#staff-auto-welcome").uncheck()
            await submit_settings(settings)
            await replay_and_skip(page)
            await page.wait_for_timeout(500)
            assert len(case["speak_calls"]) == 2, case
            await close_badge(page)
            settings = await open_settings(page)
            await settings.locator("#staff-auto-welcome").check()
            await submit_settings(settings)
            await open_preferences(page)
            await page.get_by_role("checkbox", name="MOSS 语音播报").uncheck()
            await replay_and_skip(page)
            await page.wait_for_timeout(500)
            assert len(case["speak_calls"]) == 2 and await page.evaluate("staffAudioStarts.length") == 2, case
            case["checks"]["welcome_off_and_muted"] = True

            await open_preferences(page)
            await page.get_by_role("checkbox", name="MOSS 语音播报").check()
            await page.locator("details.preferences summary").click()
            await page.locator(".brand-field").click()
            await expect(page.locator("#opening-film")).to_be_visible()
            assert len(case["speak_calls"]) == 2, case
            await page.wait_for_function("document.querySelector('#opening-film').ended")
            await page.wait_for_function("staffAudioStarts.length === 3 && staffAudioEnds === 3")
            assert [row["text"] for row in case["speak_calls"]] == [WELCOME, WELCOME, WELCOME], case
            case["checks"]["actual_film_end_one_welcome"] = True

        assert seen == set(assets), (seen, sorted(assets))
        case["asset_hits"] = sorted(seen)
    finally:
        await context.close()


async def verify(candidate):
    folder = ROOT / "runs" / datetime.now().strftime("staff-console-browser-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    outcome = folder / "result.json"
    print(outcome, flush=True)
    assets = {url:((candidate / name).read_bytes(), mime) for url, (name, mime) in STAGED.items()} if candidate else {}
    if candidate:
        assets.update({url:((candidate / name).read_bytes(), mime) for url, (name, mime) in ARTWORK.items()
                       if (candidate / name).is_file()})
    fixture = (ROOT / "voice" / "lines" / "done.wav").read_bytes()
    catalog = folder / "catalog.sqlite3"
    MediaLibrary(catalog)
    report = {"passed":False, "cases":{}, "outside":[], "other_api_writes":[], "page_errors":[], "model_connections":0,
              "candidate_asset_sha256":{url:sha256(data).hexdigest() for url, (data, _) in assets.items()},
              "fixture_wave_sha256":sha256(fixture).hexdigest()}
    environment = {"LOCALAPPDATA":str(folder / "profile"), "MOSS_PROVIDER_DIR":str(folder / "providers"),
                   "MOSS_MEDIA_DB":str(catalog), "MOSS_MEM0_ENABLED":"0", "MOSS_WINDOWS_REMINDERS":"0",
                   "MOSS_SPEECH_PREPARE":"0", "MOSS_SPEECH_STANDBY":"0", "MOSS_SPEECH_MIC_WARM":"0"}

    async def guard(reader, writer):
        report["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    try:
        stylesheet = "/static/credential-card.css"
        css = assets[stylesheet][0] if stylesheet in assets else (ROOT / "static/credential-card.css").read_bytes()
        for key, url, expected, postscript in (
            ("editable_font", EDITABLE_FONT_ASSET, EDITABLE_FONT_SHA256, EDITABLE_FONT_POSTSCRIPT),
            ("chinese_font", CHINESE_FONT_ASSET, CHINESE_FONT_SHA256, CHINESE_FONT_POSTSCRIPT),
            ("serial_font", SERIAL_FONT, "c29556a2719bf613ef3d5e070e40d903a8965d9c081beca1375dc1e6e0f93c23", SERIAL_POSTSCRIPT),
            ("number_font", NUMBER_FONT, "691470dd3286a14e9677940d0bf75796179841ba5215cbda1a2c8910a3226afd", NUMBER_POSTSCRIPT),
        ):
            if key in ("serial_font", "number_font") and url.encode("ascii") not in css:
                continue
            font_path = ROOT / "static" / url.lstrip("/")
            if url in assets or font_path.is_file():
                font = assets[url][0] if url in assets else font_path.read_bytes()
                digest = sha256(font).hexdigest()
                assert digest == expected, f"字体官方文件校验失败 {url} {digest}"
                report[key] = {"url":url, "sha256":digest, "postscript":postscript}
        assert "chinese_font" not in report or "editable_font" in report
        assert ("serial_font" in report) == ("number_font" in report)
        async with await asyncio.start_server(guard, "127.0.0.1", 0) as sink:
            model = f"http://127.0.0.1:{sink.sockets[0].getsockname()[1]}/v1"
            profiles = {key:Profile(key, label, model, model_id, SecretStr(""))
                        for key, (label, _, __, model_id) in SPECS.items()}
            with patch.dict(os.environ, environment), patch("core.profiles", return_value=profiles):
                port, data = free_port(), folder / "data"
                base = f"http://127.0.0.1:{port}"
                moss = Moss(data, model, "isolated-model-guard")
                runner = web.AppRunner(application(moss, lambda:"standby", data, port, "fixture", asyncio.Event()),
                                       access_log=None, shutdown_timeout=1)
                try:
                    await runner.setup()
                    await web.TCPSite(runner, "127.0.0.1", port).start()
                    async with async_playwright() as playwright:
                        edge = Path(os.environ["ProgramFiles(x86)"]) / "Microsoft/Edge/Application/msedge.exe"
                        browser = await playwright.chromium.launch(executable_path=str(edge), headless=True,
                            args=["--mute-audio", "--disable-gpu", "--disable-renderer-backgrounding",
                                  "--autoplay-policy=document-user-activation-required"])
                        try:
                            await confirm_muted_edge(browser)
                            report["browser"] = browser.version
                            for width in (1440, 768, 375):
                                await exercise(browser, base, folder, report, width, assets, fixture)
                        finally:
                            await browser.close()
                    assert not moss.state()["turns"] and not report["model_connections"], report
                    assert not any(report[key] for key in ("outside", "other_api_writes", "page_errors")), report
                    report["passed"] = True
                finally:
                    await runner.cleanup()
                    await moss.close()
    except Exception as error:
        report["error"] = repr(error)
        raise
    finally:
        outcome.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(outcome, report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path,
                        help="Directory with staged HTML, terminal, speech and optional artwork and editable font files.")
    asyncio.run(verify(parser.parse_args().candidate))
