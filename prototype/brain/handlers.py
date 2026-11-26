"""Domain-grouped Brain tool dispatchers — replaces the 969-line nested if/elif chain."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta
import json
import re
import sys
from time import time as epoch_now
from typing import Any, Callable
from uuid import uuid4

from assistant_notes import NoteUpdate
from assistant_timers import DURATION as TIMER_DURATION, duration_seconds
from brain.math_date_tools import (
    _parse_brain_datetime,
    _parse_iso_date,
    _safe_int,
    run_calculate_date,
    run_calculate_math,
    run_list_local_directory,
    run_read_local_file,
    world_clock_reply,
)
from brain.web_tools import run_fetch_web_page, run_news_headlines, run_web_search
from brain.weather_flow import WEATHER_TOPIC, simple_forecast, weather_result
from brain.registry import TOOL_BY_NAME, ab as _ab
from cognitive_utils import SAG_PUBLIC_SOURCES
from desktop_actions import run_desktop
from device_status import DEFAULT_DEVICE_CACHE, DeviceStatusCache, GIB, cpu_percent as calc_cpu_percent, cpu_ticks, hardware_snapshot
from holojarvis_runtime.brain import RUNTIME_TOOL_UNLOCK
from jev_reply import draft_reply
from local_calendar import (
    _slot as calendar_slot,
    apply_mutation as apply_calendar_mutation,
    calendar_event_snapshot,
    calendar_list_reply,
    calendar_reply,
    create_event as create_calendar_db_event,
    list_events,
)
from local_overview import local_clock_reply
from media_chat import CALM_EXCLUDE_KEYWORDS, CALM_PREFER_KEYWORDS, MOOD_CATEGORIES, track_key
from memory_store import find_memory, remove_memory, save_memory, select_memories
from online_music import parse_online_music_request
from weather_forecast import WEATHER_DAYS
from weather_location import FORECAST_RANGE_REPLY, NO_LOOKUP, OUTSIDE_FORECAST_DAY, districts, mentioned_district, period_from


def _device_cache() -> DeviceStatusCache:
    return _ab("_DEVICE_CACHE", DEFAULT_DEVICE_CACHE)


@dataclass
class BrainTurnContext:
    moss: Any
    turn: dict
    token: Any
    client: Any
    text: str
    colloquial_text: str
    weather_text: str
    earlier: dict
    turn_owner: str
    is_host_owner: bool
    ignore_memory: bool
    allow_personal: bool
    named: Any
    jev: Any
    previous_weather: dict | None
    weather_versions: dict | None
    nearby_location: dict | None
    location: dict | None
    day_follow: Any
    short_follow: Any
    follow_forecast: bool
    song_requested: bool
    offered_song: bool
    used: list[str]
    current_row: Callable[[], dict]
    retain_weather_memory: Callable[..., None]
    lookup_weather: Callable[..., Any]
    allow_own_personal: bool = True
    web_search_calls: int = 0
    _extra: dict = field(default_factory=dict)


# ── System / Utility Tools ──────────────────────────────────────────────────

async def _h_get_time(ctx: BrainTurnContext, args: dict) -> str:
    loc = str(args.get("location") or "").strip()
    return world_clock_reply(loc) if loc else _ab("local_clock_reply", local_clock_reply)()


async def _h_get_volume(ctx: BrainTurnContext, args: dict) -> str:
    return await run_desktop({"action": "get_volume"})


async def _h_calculate_math(ctx: BrainTurnContext, args: dict) -> str:
    return run_calculate_math(str(args.get("expression") or ""))


async def _h_calculate_date(ctx: BrainTurnContext, args: dict) -> str:
    return run_calculate_date(
        operation=str(args.get("operation") or "offset"),
        base_date=str(args.get("base_date") or ""),
        target_date=str(args.get("target_date") or ""),
        offset_days=_safe_int(args.get("offset_days"), 0),
    )


async def _h_read_local_file(ctx: BrainTurnContext, args: dict) -> str:
    if not ctx.is_host_owner:
        return "当前为非管理员授权身份，不能读取主机本地文件。"
    return await asyncio.to_thread(run_read_local_file, str(args.get("path") or ""))


async def _h_list_local_directory(ctx: BrainTurnContext, args: dict) -> str:
    if not ctx.is_host_owner:
        return "当前为非管理员授权身份，不能读取主机本地目录。"
    return await asyncio.to_thread(run_list_local_directory, str(args.get("path") or ""))


async def _h_web_search(ctx: BrainTurnContext, args: dict) -> str:
    ctx.web_search_calls += 1
    if ctx.web_search_calls > 2:
        return "本轮已执行两次联网检索，请基于已有结果直接整理回复或说明信息不足，不要继续重复搜索。"
    return await run_web_search(str(args.get("query") or ""))


async def _h_fetch_web_page(ctx: BrainTurnContext, args: dict) -> str:
    return await run_fetch_web_page(str(args.get("url") or ""))


async def _h_get_news_headlines(ctx: BrainTurnContext, args: dict) -> str:
    return await run_news_headlines(
        category=str(args.get("category") or "china"),
        count=_safe_int(args.get("count"), 5),
    )


async def _h_get_device_status(ctx: BrainTurnContext, args: dict) -> str:
    sample = await _device_cache().read()
    cpu_pct = sample.get("cpu_percent")
    if cpu_pct is None:
        t1 = await asyncio.to_thread(cpu_ticks)
        await asyncio.sleep(0.22)
        t2 = await asyncio.to_thread(cpu_ticks)
        cpu_pct = calc_cpu_percent(t1, t2) if t1 and t2 else None
    cpu_info = sample.get("cpu")
    gpu_info = sample.get("gpu")
    if cpu_info is None and gpu_info is None:
        hw_snap = await asyncio.to_thread(hardware_snapshot)
        cpu_info = hw_snap.get("cpu")
        gpu_info = hw_snap.get("gpu")
    parts = []
    if cpu_pct is not None:
        ct = f"（{cpu_info['temperature_c']:.1f}°C）" if (cpu_info or {}).get("temperature_c") is not None else ""
        parts.append(f"CPU 负载 {cpu_pct:.0f}%{ct}")
    elif (cpu_info or {}).get("temperature_c") is not None:
        parts.append(f"CPU 温度 {cpu_info['temperature_c']:.1f}°C")
    gpu = gpu_info or {}
    if gpu.get("percent") is not None:
        gt = f"（{gpu['temperature_c']:.1f}°C）" if gpu.get("temperature_c") is not None else ""
        parts.append(f"GPU {gpu.get('name') or ''} 负载 {gpu['percent']:.0f}%{gt}")
    mem = sample.get("memory") or {}
    if mem.get("available") is not None and mem.get("total"):
        parts.append(f"可用内存 {mem['available']/GIB:.1f} / {mem['total']/GIB:.1f} GiB")
    bat = sample.get("battery") or {}
    if bat.get("present") is not False and bat.get("percent") is not None:
        parts.append(f"电量 {bat['percent']:.0f}%（{'已接电' if bat.get('ac') else '电池供电'}）")
    disk = sample.get("disk") or {}
    if disk.get("free") is not None and disk.get("total"):
        parts.append(f"磁盘 {disk.get('drive', '')} 剩余 {disk['free']/GIB:.1f} / {disk['total']/GIB:.1f} GiB")
    return "；".join(parts) + "。" if parts else "暂时无法读取硬件状态。"


async def _h_list_capabilities(ctx: BrainTurnContext, args: dict) -> str:
    moss = ctx.moss
    if not ctx.is_host_owner:
        base_caps = (
            "可以播放开放曲库、中文与国际音乐电台以及公共曲库媒体、实时联网搜索公开资讯、读指定公网网页、"
            "查时间、区县天气与日历换算、精确数学计算、管理你自己的长期个人记忆，以及生成交互式 FUI 计算器/对比看板组件。"
            "当前为 C 级普通操作员权限，主机管理员便签、提醒、本机日程、本机文件、智能家居与系统控制已按安全隔离规则禁用。"
        )
    else:
        base_caps = (
            "可以播放本机或 NAS 的音乐和视频，以及在线开源曲库（Audius、Internet Archive）、中文与国际音乐电台及私有 Navidrome 曲库、查看曲库分类和歌手、实时联网搜索公开资讯、查时间、区县天气，设置、查询或取消提醒与本机日程。"
            "也可以打开应用和网页、用浏览器搜索、调整系统音量和静音状态、查看硬件遥测与目录、预览文件分类、读写便签、保存草稿和长期记忆。"
            "窗口识别和剪贴板里的文字可以编辑后拟回复。你还可以让我朗诵诗词，用停顿标记控制节奏。"
            "可以查询和控制 Windows 当前系统播放器。"
            "设置中可启用本地唤醒。可以说“列出 MCP 工具”，也可以在确认后归类或回收指定文件、锁屏、睡眠或执行 PowerShell。"
        )
    try:
        diag = moss.self_diagnostics(ctx.turn_owner)
        br = diag.get("brain") or {}
        sub = diag.get("subsystems") or {}
        cog = diag.get("cognition") or {}
        backups = "、".join(br.get("healthy_backups") or ()) or "无"
        om_srcs = "、".join(sub.get("online_music_sources") or ()) if sub.get("online_music_active") else "已关闭或隔离"
        dev_snap = await _device_cache().read()
        cpu_s = f"CPU {dev_snap['cpu_percent']:.0f}%" if isinstance(dev_snap.get("cpu_percent"), (int, float)) else ""
        mem_d = dev_snap.get("memory") or {}
        ram_s = f"可用内存 {mem_d['available']/GIB:.1f} GiB" if isinstance(mem_d.get("available"), (int, float)) else ""
        hw_s = " / ".join(x for x in (cpu_s, ram_s) if x) or "硬件采样待更新"
        if ctx.is_host_owner:
            cog_s = (
                f"长期记忆 {cog.get('memories', 0)} 条、Soul 画像 {cog.get('soul_traits', 0)} 项、"
                f"RSI 规则 {cog.get('rsi_active_lessons', 0)} 条、SAG 节点 {cog.get('sag_events', 0)} 条"
            )
        else:
            cog_s = f"当前授权身份 {ctx.turn_owner} 独立记忆 {cog.get('memories', 0)} 条（已与管理员数据完全隔离）"
        self_line = (
            f"\n当前 550W 自感知状态：主脑模型 {br.get('active_model')}（{br.get('active_provider')}，"
            f"{'在线可用' if br.get('provider_available') else '凭据未就绪'}），"
            f"健康热备 {backups}，记忆后端 {br.get('memory_backend')}，{cog_s}；"
            f"在线曲库：{om_srcs}；Home Assistant：{sub.get('home_assistant_mode')}；本机状态：{hw_s}。"
        )
        return base_caps + self_line
    except Exception:
        return base_caps


# ── Timers & Calendar Tools ─────────────────────────────────────────────────

def _resolve_timer_id(moss, kw: str, chat_id: str, action_name: str) -> tuple[int, str | None]:
    snap = moss.timers.snapshot()
    exact = [it for it in snap if it.get("label") == kw]
    fuzzy = exact or [it for it in snap if kw.casefold() in str(it.get("label") or "").casefold()]
    if len(fuzzy) == 1:
        return int(fuzzy[0]["id"]), None
    if len(fuzzy) > 1:
        same_chat = [it for it in fuzzy if it.get("chat_id") == chat_id]
        if len(same_chat) == 1:
            return int(same_chat[0]["id"]), None
        desc = "、".join(f"{it['id']} 号「{it['label']}」" for it in fuzzy[:5])
        return 0, f"匹配到多个待处理提醒（{desc}），请按编号调用 {action_name}。"
    return 0, None


async def _h_list_timers(ctx: BrainTurnContext, args: dict) -> str:
    if not ctx.is_host_owner:
        return "当前为非管理员授权身份，不能读取主机个人待办提醒。"
    return ctx.moss.timers.execute({"action": "list"})


async def _h_create_timer(ctx: BrainTurnContext, args: dict) -> str:
    if not ctx.is_host_owner:
        return "当前为非管理员授权身份，不能修改主机个人待办提醒。"
    mins = args.get("minutes")
    label = str(args.get("label") or "").strip()
    if not isinstance(mins, (int, float)) or not 0.5 <= mins <= 1440 or not label:
        return "创建提醒需要有效的分钟数（0.5-1440）和提醒内容。"
    sec = int(round(mins * 60))
    moss = ctx.moss
    with moss.db:
        reply_t = moss.timers.execute(
            {"action": "create", "label": label[:80], "seconds": sec},
            chat_id=ctx.turn["chat_id"], turn_id=ctx.turn["id"],
        )
        moss.db.execute("UPDATE state SET revision=revision+1 WHERE id=1")
    moss._notify()
    await moss.sync_reminders()
    return reply_t


async def _h_reschedule_timer(ctx: BrainTurnContext, args: dict) -> str:
    if not ctx.is_host_owner:
        return "当前为非管理员授权身份，不能修改主机个人待办提醒。"
    moss = ctx.moss
    tid = _safe_int(args.get("timer_id"), 0)
    kw = str(args.get("keyword") or "").strip()
    delay_mins = args.get("delay_minutes")
    new_time = str(args.get("new_time") or "").strip()
    if delay_mins is None and not new_time:
        return "改期提醒需要提供 delay_minutes（偏移分钟数）或 new_time（新的提醒时间）。"
    if tid <= 0 and kw:
        tid, err = _resolve_timer_id(moss, kw, ctx.turn["chat_id"], "reschedule_timer")
        if err:
            return err
    intent_r = {
        "action": "reschedule", "all": False, "id": tid if tid > 0 else None,
        "latest": tid <= 0 and not kw, "next": False, "label": "" if tid > 0 else kw,
    }
    if isinstance(delay_mins, (int, float)) and delay_mins != 0:
        intent_r["shift_seconds"] = int(round(float(delay_mins) * 60))
    else:
        rel_m = re.fullmatch(rf"(?P<delay>{TIMER_DURATION})(?:之?后|以后)", new_time)
        if rel_m:
            intent_r["seconds"] = duration_seconds(rel_m["delay"])
        else:
            try:
                dt_new = _parse_brain_datetime(new_time)
            except ValueError as exc:
                return str(exc)
            intent_r.update(day=dt_new.date().isoformat(), hour=dt_new.hour, minute=dt_new.minute, day_offset=0)
    with moss.db:
        reply_r = moss.timers.execute(intent_r, chat_id=ctx.turn["chat_id"], turn_id=ctx.turn["id"])
        moss.db.execute("UPDATE state SET revision=revision+1 WHERE id=1")
    moss._notify()
    await moss.sync_reminders()
    return reply_r


async def _h_cancel_timer(ctx: BrainTurnContext, args: dict) -> str:
    if not ctx.is_host_owner:
        return "当前为非管理员授权身份，不能取消主机个人待办提醒。"
    moss = ctx.moss
    tid = _safe_int(args.get("timer_id"), 0)
    kw = str(args.get("keyword") or "").strip()
    if tid <= 0 and kw:
        tid, err = _resolve_timer_id(moss, kw, ctx.turn["chat_id"], "cancel_timer")
        if err:
            return err
    intent_c = {
        "action": "cancel", "all": False, "id": tid if tid > 0 else None,
        "latest": tid <= 0 and not kw, "next": False, "label": "" if tid > 0 else kw,
    }
    with moss.db:
        reply_c = moss.timers.execute(intent_c, chat_id=ctx.turn["chat_id"], turn_id=ctx.turn["id"])
        moss.db.execute("UPDATE state SET revision=revision+1 WHERE id=1")
    moss._notify()
    await moss.sync_reminders()
    return reply_c


def _find_future_calendar_event(moss, kw: str, date_filter: str) -> tuple[dict | None, str | None]:
    now_ts = int(epoch_now())
    if date_filter:
        try:
            d_target = _parse_iso_date(date_filter)
        except Exception as exc:
            return None, f"日期过滤格式无效：{exc}"
        win_start = int(datetime.combine(d_target, dt_time.min).astimezone().timestamp())
        win_end = int(datetime.combine(d_target + timedelta(days=1), dt_time.min).astimezone().timestamp())
        rows = moss.db.execute(
            "SELECT operation_id, title, start, end, slot FROM calendar_events "
            "WHERE end > ? AND start < ? AND end > ? ORDER BY start",
            (now_ts, win_end, win_start),
        ).fetchall()
    else:
        rows = moss.db.execute(
            "SELECT operation_id, title, start, end, slot FROM calendar_events WHERE end > ? ORDER BY start LIMIT 50",
            (now_ts,),
        ).fetchall()
    exact = [r for r in rows if r[1].casefold() == kw.casefold()]
    matched = exact or [r for r in rows if kw.casefold() in r[1].casefold()]
    if not matched:
        if date_filter:
            all_future = moss.db.execute(
                "SELECT operation_id, title, start, end, slot FROM calendar_events WHERE end > ? ORDER BY start LIMIT 50",
                (now_ts,),
            ).fetchall()
            alt = [r for r in all_future if r[1].casefold() == kw.casefold()] or [
                r for r in all_future if kw.casefold() in r[1].casefold()
            ]
            if alt:
                listing = "；".join(
                    f"「{r[1]}」（{datetime.fromtimestamp(r[2]).strftime('%Y-%m-%d %H:%M')}-{datetime.fromtimestamp(r[3]).strftime('%H:%M')}）"
                    for r in alt[:3]
                )
                return None, f"{date_filter} 当天未找到「{kw}」，但未来日程中存在：{listing}。如需操作它，请省略 date 或传入准确日期后重试。"
        return None, f"没有找到尚未结束且包含「{kw}」的本机日程。"
    if len(matched) > 1:
        listing = "；".join(f"「{r[1]}」（{r[4]}）" for r in matched[:5])
        return None, f"找到多条匹配「{kw}」的日程（{listing}），请补充具体日期。"
    snap = calendar_event_snapshot(moss.db, matched[0][0])
    if not snap:
        return None, f"日程「{kw}」已不存在。"
    return snap, None


async def _h_query_calendar(ctx: BrainTurnContext, args: dict) -> str:
    if not ctx.is_host_owner:
        return "当前为非管理员授权身份，不能读取主机个人日程。"
    day_key = str(args.get("day") or "today")
    today_d = date.today()
    if day_key == "this_week":
        start_d = today_d - timedelta(days=today_d.weekday())
        end_d = start_d + timedelta(days=7)
        detail = f"本周（{start_d.isoformat()} 至 {(end_d - timedelta(days=1)).isoformat()}）"
    elif day_key == "next_week":
        start_d = today_d - timedelta(days=today_d.weekday()) + timedelta(days=7)
        end_d = start_d + timedelta(days=7)
        detail = f"下周（{start_d.isoformat()} 至 {(end_d - timedelta(days=1)).isoformat()}）"
    elif day_key == "next_7_days":
        start_d = today_d
        end_d = today_d + timedelta(days=7)
        detail = f"未来 7 天（{start_d.isoformat()} 至 {(end_d - timedelta(days=1)).isoformat()}）"
    else:
        offset = {"today": 0, "tomorrow": 1, "day_after_tomorrow": 2}.get(day_key, 0)
        label = {"today": "今天", "tomorrow": "明天", "day_after_tomorrow": "后天"}.get(day_key, "今天")
        start_d = today_d + timedelta(days=offset)
        end_d = start_d + timedelta(days=1)
        detail = f"{label}（{start_d.isoformat()}）"
    start_ts = int(datetime.combine(start_d, dt_time.min).astimezone().timestamp())
    end_ts = int(datetime.combine(end_d, dt_time.min).astimezone().timestamp())
    intent_cal = {"start": start_ts, "end": end_ts, "detail": detail}
    return calendar_list_reply(intent_cal, list_events(ctx.moss.db, intent_cal))


async def _h_create_calendar_event(ctx: BrainTurnContext, args: dict) -> str:
    if not ctx.is_host_owner:
        return "当前为非管理员授权身份，不能修改主机个人日程。"
    moss = ctx.moss
    title_c = str(args.get("title") or "").strip().strip("「」《》\"'")
    start_raw = str(args.get("start_time") or "").strip()
    end_raw = str(args.get("end_time") or "").strip()
    if not title_c or len(title_c) > 100:
        return "请提供 1 至 100 字的日程名称。"
    try:
        start_dt = _parse_brain_datetime(start_raw)
        end_dt = _parse_brain_datetime(end_raw, reference_date=start_dt.date())
    except ValueError as exc:
        return f"日程时间格式有误：{exc}"
    if end_dt <= start_dt:
        return "日程结束时间必须晚于开始时间。"
    start_ts, end_ts = int(start_dt.timestamp()), int(end_dt.timestamp())
    if start_ts <= epoch_now():
        return "日程开始时间已过去，请提供未来的起止时间。"
    slot = calendar_slot(start_dt, end_dt)
    overlaps = [
        ev for ev in list_events(moss.db, {"start": start_ts, "end": end_ts})
        if not (ev["title"] == title_c and ev["start"] == start_ts and ev["end"] == end_ts)
    ]
    intent_add = {"action": "calendar_create", "title": title_c, "start": start_ts, "end": end_ts, "slot": slot}
    try:
        receipt = create_calendar_db_event(moss.db, uuid4().hex, intent_add)
    except ValueError as exc:
        return f"创建日程失败：{exc}"
    with moss.db:
        moss.db.execute("UPDATE state SET revision=revision+1 WHERE id=1")
    moss.sync_calendar_sag(receipt)
    moss._notify()
    msg = calendar_reply(receipt)
    if overlaps:
        conflict_desc = "；".join(f"「{ev['title']}」（{ev['slot']}）" for ev in overlaps[:3])
        msg += f" 注意：该时段与已有日程存在时间冲突：{conflict_desc}。"
    return msg


async def _h_reschedule_calendar_event(ctx: BrainTurnContext, args: dict) -> str:
    if not ctx.is_host_owner:
        return "当前为非管理员授权身份，不能修改主机个人日程。"
    moss = ctx.moss
    kw = str(args.get("keyword") or "").strip().strip("「」《》\"'")
    if not kw:
        return "请提供要改期的日程名称或关键词。"
    try:
        start_dt = _parse_brain_datetime(str(args.get("new_start_time") or "").strip())
        end_dt = _parse_brain_datetime(str(args.get("new_end_time") or "").strip(), reference_date=start_dt.date())
    except ValueError as exc:
        return f"新日程时间格式有误：{exc}"
    if end_dt <= start_dt:
        return "新结束时间必须晚于新开始时间。"
    start_ts, end_ts = int(start_dt.timestamp()), int(end_dt.timestamp())
    if start_ts <= epoch_now():
        return "新日程开始时间已过去，请提供未来的起止时间。"
    snap, err = _find_future_calendar_event(moss, kw, str(args.get("date") or "").strip())
    if err or not snap:
        return err or "未找到目标日程。"
    new_slot = calendar_slot(start_dt, end_dt)
    try:
        receipt = apply_calendar_mutation(
            moss.db,
            uuid4().hex,
            {"action": "calendar_reschedule", "before": snap, "after": {"start": start_ts, "end": end_ts, "slot": new_slot}},
        )
    except ValueError as exc:
        return f"改期日程失败：{exc}"
    with moss.db:
        moss.db.execute("UPDATE state SET revision=revision+1 WHERE id=1")
    moss.sync_calendar_sag(receipt)
    moss._notify()
    return calendar_reply(receipt)


async def _h_cancel_calendar_event(ctx: BrainTurnContext, args: dict) -> str:
    if not ctx.is_host_owner:
        return "当前为非管理员授权身份，不能修改主机个人日程。"
    moss = ctx.moss
    kw = str(args.get("keyword") or "").strip().strip("「」《》\"'")
    if not kw:
        return "请提供要取消的日程名称或关键词。"
    snap, err = _find_future_calendar_event(moss, kw, str(args.get("date") or "").strip())
    if err or not snap:
        return err or f"日程「{kw}」已不存在。"
    try:
        receipt = apply_calendar_mutation(moss.db, uuid4().hex, {"action": "calendar_cancel", "before": snap})
    except ValueError as exc:
        return f"取消日程失败：{exc}"
    with moss.db:
        moss.db.execute("UPDATE state SET revision=revision+1 WHERE id=1")
    moss.sync_calendar_sag({**receipt, "action": "calendar_cancel"})
    moss._notify()
    return calendar_reply(receipt)


# ── Memory / Notes / SAG / Soul / RSI Tools ─────────────────────────────────

async def _h_save_note(ctx: BrainTurnContext, args: dict) -> str:
    if not ctx.is_host_owner:
        return "当前为非管理员授权身份，不能写入主机便签。"
    moss = ctx.moss
    note_text = str(args.get("text") or "").strip()
    if not note_text or len(note_text) > 1200:
        return "便签正文需在 1 至 1200 字之间。"
    with moss.db:
        created = moss.notes.add(note_text)
        moss.db.execute("UPDATE state SET revision=revision+1 WHERE id=1")
    moss.sync_note_sag(created, archived=False)
    moss._notify()
    return f"已记入本机便签（#{created['id']}）并同步至 SAG 知识库：{created['text'][:80]}"


async def _h_search_notes(ctx: BrainTurnContext, args: dict) -> str:
    if not ctx.is_host_owner or ctx.ignore_memory:
        return "本轮不读取主机个人便签。"
    q = str(args.get("query") or "").strip()
    items = ctx.moss.notes.page(q, archived=False, before=0, limit=8).get("items") or []
    if not items:
        return f"便签里没有找到包含「{q}」的记录。" if q else "目前还没有保存任何便签。"
    return "\n".join(f"#{n['id']} {n['text']}" for n in items)


async def _h_manage_note(ctx: BrainTurnContext, args: dict) -> str:
    if not ctx.is_host_owner:
        return "当前为非管理员授权身份，不能修改主机便签。"
    moss = ctx.moss
    act = str(args.get("action") or "").strip()
    nid = _safe_int(args.get("note_id"), 0)
    new_text = args.get("text")
    if nid <= 0 or act not in {"edit", "archive", "restore"}:
        return "请提供有效的便签编号与操作类型（edit/archive/restore）。"
    try:
        cur_note = moss.notes.get(nid)
        op = NoteUpdate(
            version=cur_note["version"], action=act,
            text=str(new_text).strip() if act == "edit" and new_text is not None else None,
        )
        with moss.db:
            updated_note = moss.notes.update(nid, op)
            moss.db.execute("UPDATE state SET revision=revision+1 WHERE id=1")
        moss.sync_note_sag(updated_note, archived=(act == "archive"))
        moss._notify()
        verb = {"edit": "已更新", "archive": "已归档", "restore": "已恢复"}[act]
        return f"{verb}便签 #{updated_note['id']}：{updated_note['text'][:80]}"
    except Exception as exc:
        return f"操作便签失败：{exc}"


async def _h_save_user_memory(ctx: BrainTurnContext, args: dict) -> str:
    if ctx.ignore_memory:
        return "本轮要求不使用个人记忆，未写入长期记忆。"
    moss = ctx.moss
    fact = str(args.get("fact") or "").strip()
    replace_kw = str(args.get("replace_keyword") or "").strip()
    if len(fact) < 2 or len(fact) > 160:
        return "长期记忆内容需在 2 至 160 字之间。"
    retired_texts = []
    retired_ids = []
    before_map = {m["id"]: m["text"] for m in moss.list_memories(ctx.turn_owner)}
    if replace_kw:
        for mid_old, mtext_old in before_map.items():
            if replace_kw.casefold() in mtext_old.casefold() and mtext_old.strip() != fact:
                retired_ids.append(mid_old)
                retired_texts.append(mtext_old)
    with moss.db:
        for old_id in retired_ids:
            remove_memory(moss.db, old_id, owner_code=ctx.turn_owner)
        key, change = save_memory(moss.db, fact, source="chat", chat=ctx.turn["chat_id"], turn=ctx.turn["id"], owner_code=ctx.turn_owner)
        moss.db.execute("UPDATE state SET revision=revision+1 WHERE id=1")
    saved_mem = find_memory(moss.db, key, owner_code=ctx.turn_owner)
    try:
        for old_id in retired_ids:
            moss.cascade_forget_memory(old_id, before_map.get(old_id, ""), extra_keyword=replace_kw, owner_code=ctx.turn_owner)
        if change == "updated" and key in before_map and before_map[key].strip() != fact:
            moss.cascade_forget_memory(key, before_map[key], extra_keyword=replace_kw, owner_code=ctx.turn_owner)
        moss.sag.upsert_event(
            content=saved_mem["text"], title=f"长期记忆 {saved_mem['text'][:32]}",
            source="memory", source_ref=f"memory:{key}", importance=1.25, pointer_ref=f"memory:{key}",
            owner_code=ctx.turn_owner,
        )
        soul = getattr(moss, "soul", None)
        if soul is not None:
            soul.sync_memories_to_sag_and_profile(owner_code=ctx.turn_owner)
    except Exception:
        pass
    moss._notify(ctx.turn_owner)
    await moss._sync_index(owner_code=ctx.turn_owner)
    cur_row = moss._get(ctx.turn["id"])
    mv = dict(cur_row.get("_memory_versions") or {})
    mids = list(cur_row.get("memory_ids") or [])
    for old_id in retired_ids:
        mv.pop(old_id, None)
        if old_id in mids:
            mids = [x for x in mids if x != old_id]
    if saved_mem:
        mv[key] = saved_mem.get("updated_at") or saved_mem.get("created_at")
        if key not in mids:
            mids.append(key)
        moss._update(cur_row, _memory_versions=mv, memory_ids=mids)
    verb = {"created": "已写入长期记忆", "updated": "已更新长期记忆", "unchanged": "长期记忆已存在"}[change]
    if retired_texts:
        replaced_desc = "；".join(f"「{t}」" for t in retired_texts[:3])
        return f"{verb}：{saved_mem['text']}（同时已移除旧记忆：{replaced_desc}）"
    return f"{verb}：{saved_mem['text']}"


async def _h_forget_user_memory(ctx: BrainTurnContext, args: dict) -> str:
    if ctx.ignore_memory:
        return "本轮要求忽略个人记忆，未修改长期记忆。"
    moss = ctx.moss
    kw = str(args.get("keyword") or "").strip()
    if len(kw) < 1:
        return "请提供要遗忘的记忆关键词。"
    all_mems = moss.list_memories(ctx.turn_owner)
    matched = [m for m in all_mems if kw.casefold() in m["text"].casefold()]
    if not matched:
        terms = [tok.casefold() for tok in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9]{2,}", kw)]
        if terms:
            matched = [m for m in all_mems if all(tok in m["text"].casefold() for tok in terms)]
    if not matched:
        return f"长期记忆中没有找到包含「{kw}」的条目。"
    if len(matched) > 3:
        preview = "；".join(f"「{m['text'][:40]}」" for m in matched[:5])
        return f"关键词「{kw}」匹配到 {len(matched)} 条长期记忆（{preview}），请给出更具体的关键词避免误删。"
    with moss.db:
        for item in matched:
            remove_memory(moss.db, item["id"], owner_code=ctx.turn_owner)
        moss.db.execute("UPDATE state SET revision=revision+1 WHERE id=1")
    for item in matched:
        moss.cascade_forget_memory(item["id"], item["text"], extra_keyword=kw, owner_code=ctx.turn_owner)
    moss._notify(ctx.turn_owner)
    await moss._sync_index(owner_code=ctx.turn_owner)
    cur_row = moss._get(ctx.turn["id"])
    mv = dict(cur_row.get("_memory_versions") or {})
    mids = list(cur_row.get("memory_ids") or [])
    del_ids = {item["id"] for item in matched}
    if del_ids.intersection(mv) or del_ids.intersection(mids):
        for did in del_ids:
            mv.pop(did, None)
        mids = [x for x in mids if x not in del_ids]
        moss._update(cur_row, _memory_versions=mv, memory_ids=mids)
    removed_desc = "；".join(f"「{item['text']}」" for item in matched)
    return f"已从长期记忆和知识库彻底删除 {len(matched)} 条记录：{removed_desc}"


async def _h_search_user_memory(ctx: BrainTurnContext, args: dict) -> str:
    if ctx.ignore_memory or not ctx.allow_own_personal:
        return "本轮未授权检索个人记忆。"
    q = str(args.get("query") or "").strip()
    all_mems = ctx.moss.list_memories(ctx.turn_owner)
    matched = select_memories(q, all_mems) if q else all_mems
    lines = [f"- [固定记忆] {m['text']}" for m in matched[:10]]
    soul = getattr(ctx.moss, "soul", None)
    if soul is not None:
        q_low = q.casefold()
        for tr in soul.list_profile(limit=24, owner_code=ctx.turn_owner):
            if tr.get("source") == "memory_mirror":
                continue
            if not q or q_low in tr["key"].casefold() or q_low in tr["value"].casefold() or q_low in tr["category"].casefold():
                lines.append(f"- [用户画像:{tr['category']}] {tr['key']} = {tr['value']}")
    if not lines:
        return f"长期记忆与用户画像中没有与「{q}」相关的记录。" if q else "当前还没有保存任何长期记忆或用户画像。"
    return "\n".join(lines[:16])


async def _h_recall_turn_context(ctx: BrainTurnContext, args: dict) -> str:
    moss = ctx.moss
    ptr = str(args.get("pointer") or "").strip()
    if not ptr:
        return "请提供来源指针，例如 turn:a1b2c3d4 或 E#12。"
    m_ev = re.fullmatch(r"E#?(\d{1,9})|#(\d{1,9})", ptr, re.I)
    if m_ev:
        eid = int(m_ev.group(1) or m_ev.group(2))
        row = moss.db.execute(
            "SELECT id, source, title, content, updated_at, pointer_ref, COALESCE(owner_code, 'MOSS') FROM sag_events WHERE id=?",
            (eid,),
        ).fetchone()
        if not row:
            return f"未找到知识条目 E#{eid}。"
        row_owner = moss.normalize_owner(row[6])
        allowed_owner = (
            row_owner == ctx.turn_owner
            or (ctx.turn_owner != moss.DEFAULT_OWNER and row_owner == moss.DEFAULT_OWNER and row[1] in SAG_PUBLIC_SOURCES)
        )
        if not allowed_owner:
            return f"未找到知识条目 E#{eid}。"
        if row[1] not in SAG_PUBLIC_SOURCES:
            if row_owner != ctx.turn_owner or ctx.ignore_memory or not ctx.allow_own_personal:
                return "本轮不读取私人记忆详情。"
        return f"[E#{row[0]}/{row[1]} {row[4]}] {row[2]}\n原文：{row[3]}"
    if ctx.ignore_memory:
        return "本轮不回查历史私人对话原文。"
    tid_prefix = re.sub(r"^turn[:#]?", "", ptr, flags=re.I).strip()
    if not re.fullmatch(r"[a-f0-9]{4,32}", tid_prefix, re.I):
        return f"指针格式无效：{ptr}"
    esc = tid_prefix.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = moss.db.execute(
        "SELECT t.id, t.doc FROM turns t LEFT JOIN chats c ON c.id = t.chat_id "
        "WHERE t.id LIKE ? ESCAPE '\\' AND COALESCE(c.owner_code, 'MOSS')=? ORDER BY t.rowid DESC LIMIT 2",
        (esc + "%", ctx.turn_owner),
    ).fetchall()
    if not rows:
        return f"未找到前缀为 {tid_prefix} 的历史对话。"
    out = []
    for tid, raw_doc in rows:
        doc = json.loads(raw_doc)
        out.append(f"[turn:{tid[:8]} {doc.get('created_at', '')}]\n问：{doc.get('user', '')}\n答：{doc.get('reply', '')}")
    return "\n---\n".join(out)


async def _h_record_self_lesson(ctx: BrainTurnContext, args: dict) -> str:
    if not ctx.is_host_owner:
        return "当前为非管理员授权身份，未修改系统级经验准则。"
    moss = ctx.moss
    action, lesson = moss.rsi.promote_lesson(
        scope=str(args.get("scope") or "fact"),
        trigger=str(args.get("trigger") or ctx.text[:48]),
        rule=str(args.get("rule") or ""),
        mistake=str(args.get("mistake") or ""),
        confidence=0.86,
        source="explicit_teach",
        source_turn_id=ctx.turn["id"],
    )
    if lesson:
        moss.rsi.record_episode(
            chat_id=ctx.turn["chat_id"],
            failed_turn=ctx.earlier if isinstance(ctx.earlier, dict) else None,
            correction_turn=ctx.turn,
            user_prompt=str((ctx.earlier or {}).get("user") or ctx.text),
            rejected_reply=str((ctx.earlier or {}).get("reply") or ""),
            user_correction=ctx.text,
            chosen_reply="",
            signal_type="explicit_teach",
            lesson_id=lesson["id"],
            verified=False,
        )
        verb = "新增" if action == "added" else "合并强化"
        try:
            moss.sag.upsert_event(
                content=f"触发：{lesson['trigger']}；规则：{lesson['rule']}",
                title=f"RSI规则 L#{lesson['id']} [{lesson['scope']}]",
                source="rsi",
                source_ref=f"rsi:{lesson['id']}",
            )
        except Exception:
            pass
        return f"已通过 RSI 校验并{verb}经验 L#{lesson['id']}（{lesson['scope']}）：{lesson['rule']}"
    return f"该条候选经验未通过校验（{action}），未写入长期规则。"


async def _h_review_self_lessons(ctx: BrainTurnContext, args: dict) -> str:
    if not ctx.is_host_owner:
        return "当前为非管理员授权身份，不能查看或修改管理员级自我迭代经验。"
    moss = ctx.moss
    act = str(args.get("action") or "list")
    if act == "iterate":
        cyc = await moss.run_rsi_cycle(trigger="assistant_tool")
        return cyc["summary"]
    if act == "retire":
        lid = _safe_int(args.get("lesson_id"), 0)
        if lid > 0 and moss.rsi.retire_lesson(lid, reason="user_requested"):
            return f"已淘汰经验规则 L#{lid}。"
        return f"未找到生效中的经验规则 L#{lid}。"
    snap = moss.rsi.status_snapshot()
    if not snap["lessons"]:
        return (
            f"当前尚未沉淀生效的自我迭代经验（已淘汰 {snap['retired_count']} 条，"
            f"已采集 DPO 样本 {snap['dpo_export_count']} 条）。当你在对话中纠正我或教我习惯时，我会自动提炼并验证入库。"
        )
    lines = [
        f"当前生效 RSI 经验 {snap['active_count']} 条（已淘汰 {snap['retired_count']} 条，"
        f"累计采集 {snap['episode_count']} 条纠错样本，DPO 导出 {snap['dpo_export_count']} 条）："
    ]
    for item in snap["lessons"][:10]:
        lines.append(f"- L#{item['id']} [{item['scope']} 置信{item['confidence']:.2f} 命中{item['applied_count']}次] {item['rule']}")
    return "\n".join(lines)


async def _h_search_knowledge(ctx: BrainTurnContext, args: dict) -> str:
    q = str(args.get("query") or "").strip()
    res = ctx.moss.sag.search(q, limit=6, hops=1, owner_code=ctx.turn_owner, include_public=True)
    if ctx.ignore_memory or not ctx.allow_own_personal:
        d_pub = [x for x in (res.get("direct") or []) if x.get("source") in SAG_PUBLIC_SOURCES]
        h_pub = [x for x in (res.get("hyperedge") or []) if x.get("source") in SAG_PUBLIC_SOURCES]
        if not d_pub and not h_pub:
            return f"公开文档知识库中暂未检索到与「{q}」相关的条目。"
        return "\n".join(f"- [E#{x['id']}/{x['source']}] {x['title']}：{x['content'][:220]}" for x in (d_pub + h_pub)[:6])
    return res["evidence_text"] or f"本机 SAG 知识库中暂未检索到与「{q}」直接相关或超边关联的条目。"


async def _h_ingest_knowledge(ctx: BrainTurnContext, args: dict) -> str:
    if not ctx.is_host_owner:
        return "当前为非管理员授权身份，不能写入公共知识库。"
    title = str(args.get("title") or "").strip()
    content = str(args.get("content") or "").strip()
    if not content:
        return "知识正文不能为空。"
    ev = ctx.moss.sag.upsert_event(content=content, title=title or content[:40], source="doc")
    ents = "、".join(ev["entities"][:6]) if ev["entities"] else "无"
    return f"已入库 SAG 知识节点 E#{ev['id']}《{ev['title']}》，自动关联实体：{ents}。"


async def _h_get_situational_briefing(ctx: BrainTurnContext, args: dict) -> str:
    moss = ctx.moss
    sample = await _device_cache().read()
    if not ctx.is_host_owner or ctx.ignore_memory:
        diag = moss.self_diagnostics(ctx.turn_owner)
        cpu_s = f"CPU {sample['cpu_percent']:.0f}%" if isinstance(sample.get("cpu_percent"), (int, float)) else ""
        mem = sample.get("memory") or {}
        ram_s = f"可用内存 {mem['available']/GIB:.1f} GiB" if isinstance(mem.get("available"), (int, float)) else ""
        hw_str = "、".join(x for x in (cpu_s, ram_s) if x) or "运行正常"
        return (
            f"{_ab('local_clock_reply', local_clock_reply)()} 当前运行核心：{diag['brain']['active_model']}（{diag['brain']['active_provider']}），"
            f"主机状态：{hw_str}，未读取管理员私人日程或便签。"
        )
    wm_snap = moss.consolidator.latest_snapshot()
    brief = moss.pulse.generate_briefing(
        device_sample=sample,
        timers_count=len(moss.timers.snapshot()),
        notes_count=len((moss.notes.page("", archived=False, before=0, limit=50)).get("items") or []),
        rsi_active=moss.rsi.status_snapshot()["active_count"],
        sag_events=moss.sag.status_snapshot()["event_count"],
        world_model=(wm_snap or {}).get("world_model"),
    )
    return f"{brief['title']}：{brief['text']}"


async def _h_consolidate_world_model(ctx: BrainTurnContext, args: dict) -> str:
    if not ctx.is_host_owner or ctx.ignore_memory:
        return "当前身份或记忆限制策略下不执行主机世界模型整理。"
    res = await ctx.moss.consolidator.run_consolidation_async(trigger="assistant_tool")
    wm = res["world_model"]
    extra = "\n待核对冲突：" + "；".join(c["detail"] for c in wm["conflicts"][:3]) if wm.get("conflicts") else ""
    return res["summary"] + extra


# ── Background Jobs / FUI / Skills / Routines ───────────────────────────────

async def _h_submit_background_job(ctx: BrainTurnContext, args: dict) -> str:
    if not ctx.is_host_owner:
        return "当前为非管理员授权身份，不能提交后台任务。"
    moss = ctx.moss
    kind = str(args.get("kind") or "web_research")
    title = str(args.get("title") or ctx.text[:40]).strip()
    target = str(args.get("topic_or_path") or "").strip()
    params = {"topic": target or title} if kind == "web_research" else {"path": target}
    avail_cloud = moss.model_id != "local" and moss.model_available(moss.model_id)
    job = moss.jobs.submit_job(
        kind=kind, title=title, params=params,
        search_fn=run_web_search if kind == "web_research" else None,
        llm_client=moss.model_client() if avail_cloud else None,
        llm_model=moss.model_name if avail_cloud else "",
    )
    return f"已提交后台异步任务 J#{job['id']}《{job['title']}》（类型：{kind}），执行进度将实时刷新到小屏，完成后自动写入 SAG 知识库并推送提示。"


async def _h_render_fui_widget(ctx: BrainTurnContext, args: dict) -> str:
    kind = str(args.get("kind") or "metrics")
    title = str(args.get("title") or "交互面板").strip()[:60]
    subtitle = str(args.get("subtitle") or "").strip()[:120]
    raw_spec = args.get("spec_json")
    try:
        spec = json.loads(raw_spec) if isinstance(raw_spec, str) else (raw_spec if isinstance(raw_spec, dict) else {})
    except json.JSONDecodeError:
        return "组件 spec_json 不是合法 JSON，请修正后重试。"
    widget = {"kind": kind, "title": title, "subtitle": subtitle, "spec": spec}
    ctx.moss._update(ctx.current_row(), fui_widget=widget)
    return f"已在对话界面生成可交互 FUI 组件《{title}》（类型：{kind}），请结合该组件向用户给出一两句简洁说明。"


_SAFE_SKILL_UNLOCK_ALLOWLIST = frozenset({
    "get_time", "get_volume", "get_device_status", "get_weather",
    "get_news_headlines", "web_search", "fetch_web_page",
    "calculate_math", "calculate_date", "list_local_directory", "read_local_file",
    "play_song", "list_media_catalog", "set_music_sleep_timer",
    "list_timers", "query_calendar", "search_notes", "search_knowledge",
    "search_user_memory", "get_situational_briefing", "consolidate_world_model",
    "review_self_lessons", "ha_list_devices", "ha_control_device",
    "list_recurring_tasks", "render_fui_widget", "list_capabilities",
})
_PRIVATE_SKILL_TOOLS = frozenset({"search_notes", "search_user_memory"})


async def _h_save_custom_skill(ctx: BrainTurnContext, args: dict) -> str:
    if not ctx.is_host_owner:
        return "当前为非管理员授权身份，不能创建专属技能。"
    req_tools = args.get("required_tools") if isinstance(args.get("required_tools"), list) else None
    sk = ctx.moss.jobs.save_skill(
        name=str(args.get("name") or ""),
        description=str(args.get("description") or ""),
        prompt=str(args.get("prompt") or ""),
        required_tools=req_tools,
    )
    return f"已沉淀专属技能「{sk['name']}」（#{sk['id']}，依赖工具：{', '.join(sk.get('required_tools') or ()) or '自动推断'}），现在可在大脑中枢中一键触发，也可直接对我说“运行{sk['name']}”。"


async def _h_run_custom_skill(ctx: BrainTurnContext, args: dict) -> str:
    if not ctx.is_host_owner:
        return "当前为非管理员授权身份，不能执行管理员专属技能。"
    skills = ctx.moss.jobs.list_skills()
    needle = str(args.get("name_or_id") or args.get("name") or args.get("id") or "").strip().lower()
    if not skills:
        return "当前还没有保存任何专属技能。"
    if not needle:
        return "已保存的专属技能：\n" + "\n".join(f"- #{s['id']} {s['name']}：{s.get('description') or s['prompt'][:50]}" for s in skills[:12])
    matched = next((s for s in skills if str(s["id"]) == needle or needle in s["name"].lower() or s["name"].lower() in needle), None)
    if not matched:
        return f"未找到名为「{needle}」的专属技能。当前可用技能：{'、'.join(s['name'] for s in skills[:8])}。"
    ctx.moss.jobs.mark_skill_used(matched["id"])
    unlocked_names: list[str] = []
    unlock_fn = RUNTIME_TOOL_UNLOCK.get(None)
    if unlock_fn is not None:
        schemas = []
        for tname in (matched.get("required_tools") or ()):
            if tname not in _SAFE_SKILL_UNLOCK_ALLOWLIST:
                continue
            if ctx.ignore_memory and tname in _PRIVATE_SKILL_TOOLS:
                continue
            spec = TOOL_BY_NAME.get(tname)
            if spec is not None:
                schemas.append(spec.schema)
        if schemas:
            unlocked_names = unlock_fn(schemas)
    unlock_note = f"（已为本轮安全解锁工具：{', '.join(unlocked_names)}）" if unlocked_names else ""
    return f"已调出专属技能「{matched['name']}」{unlock_note}，请立即在本轮按下列指令依次调用工具完成并向用户汇报结果：\n{matched['prompt']}"


async def _h_create_recurring_task(ctx: BrainTurnContext, args: dict) -> str:
    if not ctx.is_host_owner:
        return "当前为非管理员授权身份，不能创建周期任务。"
    task = ctx.moss.routines.create_task(
        name=str(args.get("name") or ""),
        schedule=str(args.get("schedule") or ""),
        action_kind=str(args.get("action_kind") or "reminder"),
        payload=str(args.get("payload") or ""),
    )
    return f"已创建周期例行任务 #{task['id']}「{task['name']}」，计划：{task['schedule_label']}（类型：{task['action_kind']}）。"


async def _h_list_recurring_tasks(ctx: BrainTurnContext, args: dict) -> str:
    if not ctx.is_host_owner:
        return "当前为非管理员授权身份，不能读取主机周期任务。"
    items = ctx.moss.routines.list_tasks()
    if not items:
        return "当前还没有设置任何周期例行任务。"
    lines = [
        f"- #{t['id']} {t['name']}｜{t['schedule_label']}｜{'启用' if t['enabled'] else '暂停'}"
        + (f"｜上次执行 {t['last_run_at']}" if t.get("last_run_at") else "")
        for t in items[:15]
    ]
    return f"当前共有 {len(items)} 条周期例行任务：\n" + "\n".join(lines)


async def _h_delete_recurring_task(ctx: BrainTurnContext, args: dict) -> str:
    if not ctx.is_host_owner:
        return "当前为非管理员授权身份，不能删除周期任务。"
    removed = ctx.moss.routines.delete_task(str(args.get("target") or ""))
    if not removed:
        return "未找到匹配的周期例行任务。"
    return f"已删除周期例行任务 #{removed['id']}「{removed['name']}」（{removed['schedule_label']}）。"


# ── IoT / Physical HW / Media / Weather / Jev ───────────────────────────────

async def _h_ha_list_devices(ctx: BrainTurnContext, args: dict) -> str:
    if not ctx.is_host_owner:
        return "当前为非管理员授权身份，不能读取主机智能家居设备列表。"
    res = await ctx.moss.iot.ha_list_entities(str(args.get("domain") or ""))
    if res["mode"] == "live_error":
        return f"无法连接 Home Assistant：{res.get('error')}"
    items = res.get("entities") or []
    if not items:
        return "当前没有查到匹配的 Home Assistant 设备。"
    tag = "实时 HA" if res["mode"] == "live" else "HA 演练模式(dry_run)"
    lines = [f"Home Assistant 设备状态（{tag}，共 {len(items)} 项）："]
    for e in items[:15]:
        unit = (e.get("attributes") or {}).get("unit_of_measurement", "")
        extra = ""
        attrs = e.get("attributes") or {}
        if "brightness" in attrs:
            extra = f" 亮度{round(attrs['brightness'] / 2.55)}%"
        elif "temperature" in attrs:
            extra = f" 设定{attrs['temperature']}°C"
        lines.append(f"- {e['friendly_name']} ({e['entity_id']})：{e['state']}{unit}{extra}")
    return "\n".join(lines)


async def _h_ha_control_device(ctx: BrainTurnContext, args: dict) -> str:
    if not ctx.is_host_owner:
        return "当前为非管理员授权身份，不能控制智能家居设备。"
    raw_data = args.get("data_json")
    data = {}
    if isinstance(raw_data, dict):
        data = raw_data
    elif isinstance(raw_data, str) and raw_data.strip():
        try:
            parsed = json.loads(raw_data)
            if not isinstance(parsed, dict):
                return "Home Assistant 参数 data_json 必须是 JSON 对象（例如 {\"brightness_pct\": 60}）。"
            data = parsed
        except json.JSONDecodeError as exc:
            return f"Home Assistant 参数 data_json 格式无效（{exc}），请改为合法 JSON 对象后重试。"
    res = await ctx.moss.iot.ha_call_service(str(args.get("entity_id") or ""), str(args.get("service") or ""), data)
    note = f"（{res['note']}）" if res.get("note") else ""
    return f"已执行 Home Assistant 指令 `{res['service']}` → `{res['entity_id']}`{note}。"


async def _h_control_physical_moss(ctx: BrainTurnContext, args: dict) -> str:
    if not ctx.is_host_owner:
        return "当前为非管理员授权身份，不能控制实体硬件。"
    frame = ctx.moss.iot.push_hardware_frame(
        mode=args.get("mode"), yaw_deg=args.get("yaw_deg"),
        pitch_deg=args.get("pitch_deg"), gesture=args.get("gesture"),
        screen_line2=args.get("screen_text"),
    )
    return (
        f"已向实体 550W 协议桥（{frame.get('protocol', 'moss-hw-v2')}）下发第 #{frame['seq']} 帧："
        f"灯态={frame['mode']}，舵机=({frame['servo']['yaw_deg']}°, {frame['servo']['pitch_deg']}°)，动作={frame['servo']['gesture']}。"
    )


async def _h_inspect_desktop_ui(ctx: BrainTurnContext, args: dict) -> str:
    if not ctx.is_host_owner:
        return "当前为非管理员授权身份，不能读取主机桌面窗口 UIA 结构。"
    from desktop_uia import inspect_foreground_uia

    limit = max(5, min(50, _safe_int(args.get("max_elements"), 24)))
    res = await inspect_foreground_uia(max_elements=limit)
    if not res.get("ok"):
        return str(res.get("error") or "无法读取前景窗口 UIA 控件树。")
    return str(res.get("summary_text") or "")


async def _h_list_media_catalog(ctx: BrainTurnContext, args: dict) -> str:
    catalog_text, _ = await ctx.moss.media.execute({"action": "catalog"}, ctx.moss._media_context(ctx.turn.get("chat_id")), ctx.turn)
    return catalog_text


async def _h_manage_music_pref(ctx: BrainTurnContext, args: dict) -> str:
    if not ctx.is_host_owner:
        return "当前为非管理员授权身份，不能查看或修改宿主音乐偏好。"
    moss = ctx.moss
    action = str(args.get("action") or "list")
    if action == "list":
        favs = moss.music_prefs.list_by_status("favorite")
        if not favs:
            return "目前还没有收藏任何歌曲。"
        return f"已收藏 {len(favs)} 首歌曲：\n" + "\n".join(
            f"{i}. {f['title'] or f['media_id']}" + (f" · {f['artist']}" if f.get("artist") else "")
            for i, f in enumerate(favs[:20], 1)
        )
    mid = str(args.get("media_id") or "").strip()
    title = ""
    artist = ""
    if not mid:
        latest_media = (ctx.current_row().get("media") or {}).get("selected") or (
            (ctx.earlier.get("media") or {}).get("selected") if isinstance(ctx.earlier, dict) else None
        )
        if isinstance(latest_media, dict):
            mid = str(latest_media.get("media_id") or "")
            title = str(latest_media.get("title") or "")
            artist = str(latest_media.get("artist") or "")
    if not mid:
        return "当前没有正在播放或刚点播的歌曲，请先放一首歌或指定曲目。"
    moss.music_prefs.set_status(mid, action, title=title, artist=artist)
    label = f"《{title}》" if title else mid
    if action == "favorite":
        return f"已将 {label} 加入你的音乐收藏。"
    if action == "blocked":
        return f"已将 {label} 加入跳过黑名单，后续推荐会避开这首。"
    return f"已清除 {label} 的收藏/跳过标记。"


async def _h_set_music_sleep_timer(ctx: BrainTurnContext, args: dict) -> str:
    raw = args.get("minutes", 0)
    if isinstance(raw, bool):
        return "请提供 0 到 240 之间的整数分钟数。"
    try:
        mins = int(raw)
    except (TypeError, ValueError):
        return "请提供 0 到 240 之间的整数分钟数（0 表示取消）。"
    if not (0 <= mins <= 240):
        return "请提供 0 到 240 之间的整数分钟数（0 表示取消）。"
    ctx.moss._update(ctx.current_row(), sleep_timer={"minutes": mins})
    return "已取消音乐睡眠定时。" if mins == 0 else f"已开启音乐睡眠定时，{mins} 分钟后自动渐弱停播。"


async def _h_play_song(ctx: BrainTurnContext, args: dict) -> str:
    moss = ctx.moss
    fallback_listen = {"action": "listen", "kind": "audio", "query": "", "online": False, "mode": "track", "random_song": True}
    parsed_online = parse_online_music_request(ctx.text)
    raw_source = str(args.get("source") or "").strip().lower()
    valid_sources = {"", "auto", "local", "online", "navidrome", "audius", "archive", "radiobrowser", "jamendo"}
    if raw_source not in valid_sources:
        raw_source = "auto"
    explicit_online = bool(
        (parsed_online and parsed_online.get("online_only"))
        or raw_source in {"online", "navidrome", "audius", "archive", "radiobrowser", "jamendo"}
    )
    if ctx.named and not explicit_online:
        intent = ctx.named
    else:
        follow_play = bool(re.search(
            r"怎么没放|没放出来|没播|放一下|快放|放啊|放吧|来一首|来首|先来|听听歌|放一?首歌|点歌|听歌|再听|换一首|打开.*(?:电台|广播|收音机)|"
            r"(?:播|放|搜|找|打开|来).*?\b(?:audius|navidrome|archive|jamendo)\b",
            ctx.colloquial_text, re.I,
        ))
        if not (ctx.song_requested or follow_play or explicit_online):
            return "提示：用户本轮没有要求放歌，无需播放任何音乐，也不要在回复中提播放工具或这句提示，请直接自然回应用户刚才说的话。"
        title = str(args.get("title") or "").strip().strip("《》")
        artist = str(args.get("artist") or "").strip()
        category = str(args.get("category") or "").strip()
        if len(title) > 100 or len(artist) > 80:
            raise ValueError("歌名或歌手名过长。")
        if re.fullmatch(r"(?:(?:随便|随机|任意|好听|流行|推荐)(?:的)?)?(?:一?首)?(?:歌(?:曲|单)?|音乐|曲(?:子|目)?|随便|随机|推荐)", title, re.IGNORECASE):
            title = ""
        if category and not any(pattern.search(ctx.colloquial_text) for pattern, _, _ in MOOD_CATEGORIES):
            category = ""
        if artist and track_key(artist) not in track_key(ctx.text):
            artist = ""
        requested = bool(ctx.offered_song or follow_play or re.search(
            r"想听|我要听|想试听|来一首|来首|来个|开个|点歌|播放|[放播](?:一?首|点|一下|[^心弃松假])|给我放|听(?:一?[首手]|个)|打开.*(?:电台|广播)",
            ctx.colloquial_text,
        ))
        play = (
            args.get("play", True) is True
            and requested
            and not re.search(r"别(?:给我)?(?:播|放)|不要(?:播|放)|不(?:要|用)播放|只(?:想)?(?:看|查|搜)", ctx.text)
        )
        provider_choice = (
            raw_source if raw_source in {"navidrome", "audius", "archive", "radiobrowser", "jamendo"}
            else (parsed_online.get("provider") if parsed_online else None)
        )
        is_radio = bool((parsed_online and parsed_online.get("explicit_radio")) or provider_choice == "radiobrowser")
        cat_folders = {"助眠": "2-纯音乐", "纯音乐": "2-纯音乐", "流行": "1-流行", "国风": "3-国风", "儿歌": "4-儿歌"}
        if explicit_online:
            online_q = " ".join(p for p in (title, artist, category) if p).strip()
            if not online_q and parsed_online:
                online_q = parsed_online.get("query") or ""
            intent = {
                "action": "listen" if play else "search", "kind": "audio", "query": online_q,
                "online": True, "online_only": True, "explicit_radio": is_radio,
                "provider": provider_choice, "play": play, "mode": "track",
            }
        elif category and category in cat_folders and not title:
            calm = category == "助眠"
            intent = {
                "action": "listen" if play else "search", "kind": "audio", "query": "", "folder_hint": cat_folders[category],
                "category_label": category, "online": False, "mode": "track", "random_song": True, "play": play,
                "exclude_keywords": CALM_EXCLUDE_KEYWORDS if calm else (),
                "prefer_keywords": CALM_PREFER_KEYWORDS if calm else (),
            }
        elif artist and not title:
            intent = {"action": "listen" if play else "search", "kind": "audio", "query": artist, "artist": artist, "online": False, "mode": "track"}
        elif title:
            intent = {"action": "named_song", "title": title, "artist": artist, "play": play, "mode": "track"}
        else:
            intent = dict(fallback_listen)
    local_check_text = f"只查本地 {ctx.text}" if raw_source == "local" else ctx.text
    moss._update(ctx.current_row(), media_in_flight=True)
    try:
        reply, view = await moss.resolve_media_execution(intent, moss._media_context(ctx.turn.get("chat_id")), ctx.turn, user_text=local_check_text)
    finally:
        if not moss.closing:
            moss._update(moss._get(ctx.turn["id"]), media_in_flight=False)
    moss._update(ctx.current_row(), source="media", media=view, media_actions=[intent["action"]])
    return reply


async def _h_get_weather(ctx: BrainTurnContext, args: dict) -> str:
    row = ctx.current_row()
    city = args.get("city", "")
    if not isinstance(city, str):
        city = ""
    if re.fullmatch(
        r"(?:今儿个?|今个|明儿个?|明个|后儿个?|后个|大后儿个?|大后儿|昨儿个?|昨个|前儿个?|前个|"
        r"今天|明天|后天|大后天|昨天|前天|这儿|这里|那儿|那里|哪儿|本地|当地|当前位置|我这儿?)",
        city.strip(),
    ):
        city = ""
    topic = WEATHER_TOPIC.search(ctx.weather_text)
    referring = ctx.previous_weather and (ctx.day_follow or ctx.short_follow or ctx.follow_forecast)
    if NO_LOOKUP.search(ctx.text) or not (topic or referring):
        return "本轮没有要求查询天气。"
    if topic:
        city = mentioned_district(ctx.weather_text) or city
    period = args.get("period", "current")
    if OUTSIDE_FORECAST_DAY.search(ctx.weather_text):
        return FORECAST_RANGE_REPLY
    explicit_days = [value for label, value in WEATHER_DAYS.items() if label in ctx.weather_text]
    if len(explicit_days) == 1:
        period = explicit_days[0]
    if referring:
        if ctx.follow_forecast:
            city = ctx.previous_weather["city"]
            default = ctx.previous_weather["period"]
            period = period_from(ctx.text, "today" if default == "current" and "带伞" in ctx.text else default)
        elif ctx.day_follow:
            if ctx.previous_weather["city"]:
                city = ctx.previous_weather["city"]
            elif not city or city.removesuffix("市") not in ctx.previous_weather["previous_reply"]:
                return "要查哪个城市的天气？"
            if ctx.day_follow[1] == "现在":
                period = "current"
        elif not topic and not explicit_days:
            if not city or city.removesuffix("市") not in ctx.text:
                return "请提供要查询的城市名。"
            period = ctx.previous_weather["period"]
    selected_place = ctx.location or (ctx.nearby_location if not city and not referring else None)
    if ctx.day_follow and ctx.previous_weather and "latitude" in ctx.previous_weather:
        selected_place = {
            "name": ctx.previous_weather["city"],
            "kind": ctx.previous_weather.get("location_kind", "city"),
            "parent": ctx.previous_weather.get("parent"),
            "latitude": ctx.previous_weather["latitude"],
            "longitude": ctx.previous_weather["longitude"],
        }
    lookup_previous = ctx.previous_weather if ctx.previous_weather and not selected_place and len(districts(city)) > 1 else None
    source_versions = (ctx.weather_versions or {}) if referring or lookup_previous else {}
    versions = {**(row.get("_memory_versions") or {}), **source_versions}
    ctx.retain_weather_memory(source_versions)
    reply, context = await ctx.lookup_weather(city, period, versions, location=selected_place, previous=lookup_previous)
    ctx.current_row()
    reply, changes = weather_result(
        reply, context, city, period, location=selected_place,
        previous=lookup_previous, simple=simple_forecast(ctx.text, city, ctx.previous_weather),
    )
    if context or "_weather_retry" in changes:
        ctx.used.append("get_weather")
        changes["holo_tools"] = list(ctx.used)
    if city or selected_place:
        ctx.retain_weather_memory(versions, _weather_memory_versions=versions, **changes)
    return reply


async def _h_draft_chat_reply(ctx: BrainTurnContext, args: dict) -> str:
    if ctx.jev is None:
        raise ValueError("本轮没有对应的拟回复上下文。")
    reply, meta = await _ab("draft_reply", draft_reply)(ctx.jev, ctx.client, ctx.moss.model_name, provider_id=ctx.moss.model_id)
    ctx.moss._update(ctx.current_row(), jev=meta)
    return reply


TOOL_HANDLERS: dict[str, Callable[[BrainTurnContext, dict], Any]] = {
    "get_time": _h_get_time,
    "get_volume": _h_get_volume,
    "list_timers": _h_list_timers,
    "create_timer": _h_create_timer,
    "reschedule_timer": _h_reschedule_timer,
    "cancel_timer": _h_cancel_timer,
    "web_search": _h_web_search,
    "fetch_web_page": _h_fetch_web_page,
    "get_news_headlines": _h_get_news_headlines,
    "calculate_math": _h_calculate_math,
    "calculate_date": _h_calculate_date,
    "read_local_file": _h_read_local_file,
    "list_local_directory": _h_list_local_directory,
    "save_note": _h_save_note,
    "search_notes": _h_search_notes,
    "manage_note": _h_manage_note,
    "save_user_memory": _h_save_user_memory,
    "forget_user_memory": _h_forget_user_memory,
    "search_user_memory": _h_search_user_memory,
    "recall_turn_context": _h_recall_turn_context,
    "record_self_lesson": _h_record_self_lesson,
    "review_self_lessons": _h_review_self_lessons,
    "search_knowledge": _h_search_knowledge,
    "ingest_knowledge": _h_ingest_knowledge,
    "get_situational_briefing": _h_get_situational_briefing,
    "consolidate_world_model": _h_consolidate_world_model,
    "submit_background_job": _h_submit_background_job,
    "render_fui_widget": _h_render_fui_widget,
    "save_custom_skill": _h_save_custom_skill,
    "run_custom_skill": _h_run_custom_skill,
    "create_recurring_task": _h_create_recurring_task,
    "list_recurring_tasks": _h_list_recurring_tasks,
    "delete_recurring_task": _h_delete_recurring_task,
    "ha_list_devices": _h_ha_list_devices,
    "ha_control_device": _h_ha_control_device,
    "control_physical_moss": _h_control_physical_moss,
    "inspect_desktop_ui": _h_inspect_desktop_ui,
    "manage_music_pref": _h_manage_music_pref,
    "set_music_sleep_timer": _h_set_music_sleep_timer,
    "query_calendar": _h_query_calendar,
    "create_calendar_event": _h_create_calendar_event,
    "reschedule_calendar_event": _h_reschedule_calendar_event,
    "cancel_calendar_event": _h_cancel_calendar_event,
    "list_media_catalog": _h_list_media_catalog,
    "list_capabilities": _h_list_capabilities,
    "get_weather": _h_get_weather,
    "get_device_status": _h_get_device_status,
    "draft_chat_reply": _h_draft_chat_reply,
    "play_song": _h_play_song,
}
