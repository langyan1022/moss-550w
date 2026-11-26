"""Top-level Brain conversation orchestrator (`answer`)."""
from __future__ import annotations

from datetime import datetime
import os
import re
import sys
from time import monotonic
import unicodedata

from brain.handlers import BrainTurnContext, TOOL_HANDLERS
from brain.prompts import (
    DAY_FOLLOWUP,
    FAREWELL_RE,
    FIRST_SENTENCE_END,
    OFFER_ACCEPT,
    ONE_ACTION_GUIDANCE,
    PARAGRAPH_END,
    SHORT_FOLLOWUP,
    build_system_prompt,
    conversational_cadence_guidance,
    filter_unverified_media_reply,
    strip_echoed_user_prefix,
    wants_one_action,
)
from brain.registry import CAPABILITIES, CLOCK, IDEMPOTENT_READONLY_TOOLS, JEV, PARALLEL_SAFE_TOOLS, SONG, WEATHER, ab as _ab
from brain.router import (
    COMPOUND_OTHER_INTENT,
    DIET_CADENCE_ONLY_TOPIC,
    SONG_TOOL_REQUEST,
    has_any_tool_topic,
    is_compound_request,
    select_tools_for_turn,
)
from brain.weather_flow import (
    BROWSER_POSITION_REQUIRED,
    WEATHER_CLARIFICATION_LIFETIME,
    WEATHER_PLACE_REQUIRED,
    WEATHER_TOPIC,
    active_weather_clarification,
    get_weather,
    weather_input,
    weather_result,
    weather_retry_intent,
)
from colloquial_zh import normalize_colloquial_erhua, normalize_weather_erhua
from holo_kernel import run_holo
from local_overview import LOCAL_CLOCK
from media_chat import NEGATION
from memory_store import ignore_personal_memory, other_person_query
from personality import personality_prompt
from reply_length import reply_length
from weather_forecast import WEATHER_DAYS, forecast_focus
from weather_location import (
    BROWSER_REFERENCE,
    DEVICE_LOCATION,
    FACT_END,
    FORECAST_RANGE_REPLY,
    NO_LOOKUP,
    OTHER_REQUEST,
    OUTSIDE_FORECAST_DAY,
    PART_OF_DAY,
    PREFIX,
    coordinates,
    districts,
    forecast_details,
    forecast_reference,
    mentioned_district,
    period_from,
    place_text,
    referenced_kind,
    weather_coordinate_answer,
    weather_place_answer,
)
from weather_places import known_city


async def answer(
    moss,
    turn,
    token,
    *,
    history,
    instructions,
    named=None,
    jev=None,
    browser_location=None,
    configured_location=None,
    allowed_tools: set[str] | None = None,
):
    turn_started = monotonic()
    text = turn["user"]
    weather_text = normalize_weather_erhua(text)
    colloquial_text = normalize_colloquial_erhua(text)
    compound_request = is_compound_request(colloquial_text, weather_text)
    nearby_location = configured_location or browser_location
    required = JEV if jev is not None else SONG if named else None
    day_follow = DAY_FOLLOWUP.fullmatch(weather_text.strip())
    short_follow = SHORT_FOLLOWUP.fullmatch(text.strip())
    earlier = next(moss._recent_turns(turn["chat_id"], before=turn["id"]), {}) if (not required or compound_request) else {}
    pending = active_weather_clarification(earlier, turn)
    ignore_memory = ignore_personal_memory(text)
    turn_owner = turn.get("owner_code") or (moss.chat_owner(turn["chat_id"]) if hasattr(moss, "chat_owner") else "MOSS")
    is_host_owner = (turn_owner or "MOSS") == "MOSS"
    allow_own_personal = not (ignore_memory or other_person_query(text))
    allow_personal = is_host_owner and allow_own_personal
    previous_weather, choices, weather_versions = weather_input(moss, earlier, ignore_memory)
    location = None
    follow_forecast = False

    def _record_done(*, personal_ctx: bool = False, extra_tokens: dict | None = None):
        row = moss._get(turn["id"])
        patch_d: dict = {"reply_ms": round((monotonic() - turn_started) * 1000, 1)}
        if extra_tokens and extra_tokens.get("total", 0) > 0:
            patch_d["token_usage"] = dict(extra_tokens)
        if personal_ctx:
            patch_d["_used_personal_context"] = True
        return moss._update(row, **patch_d)

    def retain_weather_memory(versions, **changes):
        if versions or changes:
            row = moss._get(turn["id"])
            if versions:
                changes["_memory_versions"] = {**(row.get("_memory_versions") or {}), **versions}
            moss._update(row, **changes)

    def request_place(reply, part, focus):
        default = "today" if part or focus else "current"
        lifetime = _ab("WEATHER_CLARIFICATION_LIFETIME", WEATHER_CLARIFICATION_LIFETIME)
        metadata = {
            "period": period_from(text, default),
            "part_of_day": part,
            "focus": focus,
            "expires_at": (datetime.fromisoformat(turn["created_at"]) + lifetime).isoformat(),
        }
        retain_weather_memory({}, weather_clarification=metadata)
        return reply

    async def lookup(city, period, versions, *, location=None, previous=None, part_of_day=None, question=""):
        def current_location():
            return not versions or moss._memory_current({"_memory_versions": versions}, owner_code=turn_owner)

        if not current_location():
            return WEATHER_PLACE_REQUIRED, None
        options = {"part_of_day": part_of_day, "question": question} if part_of_day or question else {}
        result = await _ab("get_weather", get_weather)(city, period, location=location, previous=previous, **options)
        return result if current_location() else (WEATHER_PLACE_REQUIRED, None)

    async def finish_weather(city, period, versions, *, location=None, previous=None, part_of_day=None, focus=()):
        reply, result = await lookup(city, period, versions, location=location, previous=previous, part_of_day=part_of_day, question="、".join(focus))
        if not token.cancelled and not moss.closing:
            reply, changes = weather_result(
                reply, result, city, period, location=location,
                previous=previous, part_of_day=part_of_day, focus=focus,
            )
            retain_weather_memory(versions, holo_tools=["get_weather"], _weather_memory_versions=versions, **changes)
        return reply

    if not required and weather_retry_intent(earlier, turn) == "retry":
        retry = earlier["_weather_retry"]
        versions = moss._memory_provenance(earlier, "_weather_memory_versions", owner_code=turn_owner)
        if versions is None or (versions and ignore_memory):
            return WEATHER_PLACE_REQUIRED
        place = retry.get("location")
        if retry["kind"] == "browser":
            if not nearby_location:
                clarification = {key: retry[key] for key in ("period", "part_of_day", "focus", "expires_at")}
                retain_weather_memory(versions, _weather_retry=retry, _weather_memory_versions=versions, weather_clarification=clarification)
                return BROWSER_POSITION_REQUIRED
            place = nearby_location
        reply_out = await finish_weather(
            retry["city"], retry["period"], versions, location=place,
            previous=retry.get("previous"), part_of_day=retry["part_of_day"], focus=retry["focus"],
        )
        _record_done()
        return reply_out

    plain = unicodedata.normalize("NFKC", text).strip()
    qualifier = plain.strip(" 。？?!").removesuffix("的")
    pending_browser = bool(pending and BROWSER_REFERENCE.fullmatch(qualifier))
    if not required and not pending_browser and DEVICE_LOCATION.fullmatch(plain):
        if pending:
            retain_weather_memory({}, weather_clarification=pending)
        if configured_location:
            return f"当前设定城市是{configured_location['name']}。"
        if not browser_location:
            return "我还没收到浏览器的定位。可以在设置点「天气定位」重试。"
        lat, lon = (round(browser_location[axis], 1) or 0.0 for axis in ("latitude", "longitude"))
        retain_weather_memory({}, browser_position_available=True)
        return f"浏览器定位约在纬度 {lat:.1f}°、经度 {lon:.1f}°。"

    if not required and not NO_LOOKUP.search(text):
        unprefixed = PREFIX.sub("", weather_text.strip())
        lookup_text = re.sub(r"^((?:今天|明天|后天|现在|当前|这会儿)?\s*)(?:本地|当地)", r"\1这里", unprefixed)
        if lookup_text == unprefixed:
            lookup_text = weather_text
        explicit_place, follow_forecast = forecast_reference(lookup_text)
        structured_name, part_of_day, direct_forecast = forecast_details(lookup_text)
        matched_pending = False
        pending_name = ""
        if pending and not direct_forecast:
            if day_follow:
                if day_follow[1] == "大后天":
                    return FORECAST_RANGE_REPLY
                retain_weather_memory({}, weather_clarification={**pending, "period": period_from(text, pending["period"])})
                return WEATHER_PLACE_REQUIRED
            if pending_browser:
                if not nearby_location:
                    retain_weather_memory({}, weather_clarification=pending)
                    return BROWSER_POSITION_REQUIRED
                location = nearby_location
            else:
                try:
                    location = weather_coordinate_answer(plain)
                except ValueError as error:
                    retain_weather_memory({}, weather_clarification=pending)
                    return str(error)
                if not location:
                    pending_name = weather_place_answer(plain)
            matched_pending = bool(location or pending_name)
        follow_forecast = follow_forecast or bool(explicit_place and direct_forecast and not structured_name)
        kind = referenced_kind(text) if explicit_place and follow_forecast else None
        if follow_forecast and explicit_place and (not previous_weather or kind and previous_weather.get("location_kind") != kind):
            earlier = moss._last_weather_turn(turn["chat_id"], turn["id"], kind)
            previous_weather, choices, weather_versions = weather_input(moss, earlier, ignore_memory)
        if choices and not explicit_place and earlier.get("replied_at") and (
            datetime.fromisoformat(turn["created_at"]) - datetime.fromisoformat(earlier["replied_at"]) > _ab("WEATHER_CLARIFICATION_LIFETIME", WEATHER_CLARIFICATION_LIFETIME)
        ):
            choices = {}
        if choices and not matched_pending:
            q_match = text.strip(" 。，、？?！!").removesuffix("的")
            selected = [item for item in choices["choices"] if q_match and q_match in item["name"]]
            if len(selected) == 1:
                location = {**selected[0], "kind": selected[0].get("kind", "district")}
        choice_matched = bool(location and not matched_pending)
        part_followup = bool(
            previous_weather and part_of_day
            and re.fullmatch(r"(?:那|那么)?(?:今天|明天|后天)?(?:" + PART_OF_DAY.pattern + r")(?:怎么样|如何|呢)?[？?。！!]*", text.strip())
        )
        if part_followup:
            follow_forecast = True
        if WEATHER_TOPIC.search(text) and not (explicit_place and follow_forecast):
            try:
                explicit_coordinates = coordinates(text)
            except ValueError as error:
                moss._update(moss._get(turn["id"]), weather_request_kind="coordinates")
                return str(error)
            if explicit_coordinates:
                location = explicit_coordinates
                choice_matched = False
        name = pending_name or (structured_name if direct_forecast else place_text(lookup_text))
        if explicit_place and follow_forecast:
            name = ""
        elif FACT_END.search(text):
            name = mentioned_district(text) or name
        if BROWSER_REFERENCE.search(name):
            name = ""
        asks_browser = bool(not explicit_place and BROWSER_REFERENCE.search(lookup_text) and (direct_forecast or WEATHER_TOPIC.search(text)))
        if asks_browser and not (name or location):
            if not nearby_location:
                return request_place(BROWSER_POSITION_REQUIRED, part_of_day, forecast_focus(text))
            location = nearby_location
        anchored = bool(explicit_place or day_follow or part_followup or text.lstrip().startswith(("那", "再看")))
        if configured_location and direct_forecast and not anchored and not (name or location) and not ignore_memory:
            location = configured_location
        unresolved_forecast = (
            follow_forecast and not (name or location or nearby_location and not explicit_place)
            and (not previous_weather or explicit_place and not previous_weather.get("city"))
        )
        if unresolved_forecast or (day_follow and choices):
            if choices and choices.get("choices"):
                choices = {**choices, "period": period_from(text, choices["period"])}
                names = "、".join(item["name"] for item in choices["choices"][:5])
                retain_weather_memory(
                    weather_versions, weather_choices=choices,
                    weather_request_kind=earlier.get("weather_request_kind"), _weather_memory_versions=weather_versions,
                )
                return f"上次查到了多个同名地点，包括{names}。你指的是哪一个？"
            return request_place(WEATHER_PLACE_REQUIRED, part_of_day, forecast_focus(text))
        if day_follow and weather_versions is None and not asks_browser:
            return request_place(WEATHER_PLACE_REQUIRED, part_of_day, forecast_focus(text))
        place_follow = short_follow and previous_weather and not day_follow and short_follow[1]
        if place_follow and (known_city(place_follow) or districts(place_follow, previous_weather) or place_follow.endswith(("区", "县", "市", "旗"))):
            name = place_follow
        cleared_preference = (
            not (name or location) and not anchored and direct_forecast and previous_weather
            and previous_weather.get("location_kind") == "configured"
        )
        if cleared_preference:
            location = browser_location
            if not location:
                return request_place(WEATHER_PLACE_REQUIRED, part_of_day, forecast_focus(text))
        elif not (name or location) and previous_weather and previous_weather.get("city") and (
            follow_forecast or direct_forecast or day_follow and previous_weather.get("simple", True)
        ):
            name = previous_weather["city"]
            if previous_weather.get("location_kind") == "browser":
                location = browser_location if explicit_place else nearby_location
                if not location:
                    return request_place(
                        BROWSER_POSITION_REQUIRED, part_of_day or previous_weather.get("part_of_day"),
                        forecast_focus(text) or previous_weather.get("focus", []),
                    )
            elif "latitude" in previous_weather:
                location = {
                    "name": name, "latitude": previous_weather["latitude"], "longitude": previous_weather["longitude"],
                    "kind": previous_weather.get("location_kind", "city"), "parent": previous_weather.get("parent"),
                }
        if not compound_request and OUTSIDE_FORECAST_DAY.search(weather_text) and WEATHER_TOPIC.search(weather_text) and not OTHER_REQUEST.search(colloquial_text):
            return FORECAST_RANGE_REPLY
        if not (name or location or explicit_place or day_follow or choices or previous_weather) and nearby_location and direct_forecast:
            location = nearby_location
        if direct_forecast and not (name or location):
            return request_place(WEATHER_PLACE_REQUIRED, part_of_day, forecast_focus(text))
        if not compound_request and (name or location) and (direct_forecast or not OTHER_REQUEST.search(text)):
            if OUTSIDE_FORECAST_DAY.search(weather_text):
                return FORECAST_RANGE_REPLY
            lookup_previous = previous_weather if previous_weather and not location and len(districts(name)) > 1 else None
            preference_used = configured_location is not None and location is configured_location
            from_previous = previous_weather and not asks_browser and not preference_used and not cleared_preference and (
                day_follow or place_follow or follow_forecast or lookup_previous or direct_forecast and not structured_name
            )
            versions = (weather_versions or {}) if choice_matched or from_previous else {}
            inherited = (
                pending["period"] if matched_pending else choices.get("period") if choice_matched else
                previous_weather["period"] if from_previous else
                "today" if part_of_day or re.search(r"下[雨雪]|带伞|雨量|降雨|降水|穿衣|穿什么|怎么穿", text) else "current"
            )
            if matched_pending:
                part_of_day = pending["part_of_day"]
            elif choice_matched and not part_of_day:
                part_of_day = choices.get("part_of_day")
            if day_follow and not part_of_day:
                part_of_day = previous_weather.get("part_of_day") if previous_weather and not asks_browser else None
            period = inherited if matched_pending else period_from(weather_text, inherited)
            if part_of_day and period == "current":
                period = "today"
            focus = pending["focus"] if matched_pending else forecast_focus(weather_text) if direct_forecast else choices.get("focus", []) if choice_matched else []
            if not focus and from_previous and (day_follow or part_followup):
                focus = previous_weather.get("focus") or []
            part = part_of_day if direct_forecast or matched_pending or choice_matched or part_followup or day_follow else None
            reply_out = await finish_weather(name, period, versions, location=location, previous=lookup_previous, part_of_day=part, focus=focus)
            _record_done()
            return reply_out
        if follow_forecast and location and OTHER_REQUEST.search(text):
            required = WEATHER
        if day_follow and not previous_weather and earlier.get("media"):
            return f"{day_follow[1]}要查什么？说一下地点或具体安排。"

    if previous_weather and (day_follow or short_follow):
        retain_weather_memory(weather_versions)
        city_note = previous_weather["city"] or "见上一轮的天气回复"
        instructions += (
            f"\n上一轮实际查到的天气地点为{city_note}，时段为{previous_weather['period']}。"
            "追问日期时沿用地点，追问另一个城市时沿用时段。查询新的预报后回答。"
        )
        if day_follow:
            required = WEATHER
    if not required and re.fullmatch(r"(?:你|MOSS)?(?:现在|目前)?(?:都)?(?:能做什么|会做什么|有哪些(?:工具|功能)|能用哪些工具)[？?。！!]*", text.strip(), re.I):
        required = CAPABILITIES
    if not required and LOCAL_CLOCK.fullmatch(text.strip()):
        required = CLOCK
    weather_lookup = re.match(r"^(?:(?:请你?|帮我|给我|麻烦你?|实际|现在)\s*)*查[^。！？]{0,24}(?:天气|气温|降水|会下雨)", weather_text.strip())
    if not required and not compound_request and weather_lookup and not NO_LOOKUP.search(text) and any(day in weather_text for day in WEATHER_DAYS):
        required = WEATHER

    earlier_reply = (earlier.get("reply") or "") if isinstance(earlier, dict) else ""
    offered_song = bool(re.search(r"《[^《》\n]{1,40}》", earlier_reply) and re.search(r"放|播|听|凑合|要不要|要不", earlier_reply) and OFFER_ACCEPT.fullmatch(text.strip()))
    song_requested = bool((SONG_TOOL_REQUEST.search(colloquial_text) or offered_song) and not NEGATION.search(text))
    follow_play_hint = bool(re.search(r"怎么没放|没放出来|没播|放一下|快放|放啊|放吧|来一首|来首|先来|听听歌|放一?首歌|放歌|点歌|听歌|音乐|曲子|歌单|曲库|歌手", colloquial_text))
    earlier_music = bool(re.search(r"歌|音乐|曲|播放|没放|曲库", earlier_reply))
    literal_repeat = bool(re.match(r"(?:请)?(?:原样复述|逐字复述|把刚才的安排压成|用三百字描述|只回复|翻译以下文字)", text))

    used: list[str] = []
    last_write = 0.0
    published_preview = ""
    sent_paragraph_end = False
    sent_first_sentence = False

    def current():
        row = moss._get(turn["id"])
        if token.cancelled or moss.closing or row["status"] != "thinking":
            raise ValueError("本轮已结束。")
        return row

    if moss.model_id == "local" and moss.start_local:
        await moss.start_local()
        current()
    client = moss.model_client()

    has_topic = has_any_tool_topic(colloquial_text, weather_text, text, earlier if isinstance(earlier, dict) else None)
    plain_prose = not (required or has_topic or day_follow or (previous_weather and short_follow))
    one_action = plain_prose and wants_one_action(text)
    length = reply_length(text) if not required else None
    tools = select_tools_for_turn(
        moss.model_id, text, earlier=earlier if isinstance(earlier, dict) else None,
        required=required, ignore_memory=ignore_memory, is_host_owner=is_host_owner,
        song_requested=song_requested, follow_play_hint=follow_play_hint, earlier_music=earlier_music,
        day_follow=bool(day_follow), previous_weather=previous_weather, literal_repeat=literal_repeat,
        compound_request=compound_request, one_action=one_action, length=length, plain_prose=plain_prose,
        allowed_tools=allowed_tools,
    )
    if allowed_tools is not None:
        tools = [t for t in tools if (t.get("name") or (t.get("function") or {}).get("name")) in allowed_tools]
        if required and (required.get("name") not in allowed_tools):
            required = None

    ctx = BrainTurnContext(
        moss=moss, turn=turn, token=token, client=client, text=text,
        colloquial_text=colloquial_text, weather_text=weather_text,
        earlier=earlier if isinstance(earlier, dict) else {}, turn_owner=turn_owner,
        is_host_owner=is_host_owner, ignore_memory=ignore_memory, allow_personal=allow_personal,
        allow_own_personal=allow_own_personal,
        named=named, jev=jev, previous_weather=previous_weather, weather_versions=weather_versions,
        nearby_location=nearby_location, location=location, day_follow=day_follow, short_follow=short_follow,
        follow_forecast=follow_forecast, song_requested=song_requested, offered_song=offered_song,
        used=used, current_row=current, retain_weather_memory=retain_weather_memory, lookup_weather=lookup,
    )

    async def dispatch(name, args):
        row = current()
        if row.get("conversation_replaceable"):
            moss._update(row, conversation_replaceable=False)
        if allowed_tools is not None and name not in allowed_tools:
            raise ValueError(f"当前受限会话禁止调用工具「{name}」。")
        handler = TOOL_HANDLERS.get(name)
        if handler is None:
            raise ValueError("本轮没有对应的工具。")
        res_text = await handler(ctx, args or {})
        if name != "get_weather":
            used.append(name)
            moss._update(current(), holo_tools=list(used))
        if moss.model_id == "local" and isinstance(res_text, str) and len(res_text) > 1800:
            res_text = res_text[:1800] + "\n…(内容较长，已截断以适配本机上下文)"
        return res_text

    def on_text(content):
        nonlocal last_write, published_preview, sent_paragraph_end, sent_first_sentence
        if content and filter_unverified_media_reply(content) != content:
            content = ""
        if content == published_preview:
            return
        stamp = monotonic()
        stripped = content.strip()
        paragraph_end = not sent_paragraph_end and bool(stripped) and PARAGRAPH_END.search(content) is not None
        sentence_end = not sent_first_sentence and bool(stripped) and FIRST_SENTENCE_END.match(stripped) is not None
        if not content or paragraph_end or sentence_end or stamp - last_write >= 0.15:
            moss._update(current(), partial_reply=content)
            published_preview = content
            last_write = stamp
            sent_paragraph_end |= paragraph_end
            sent_first_sentence |= sentence_end

    def finish_after_tool(name):
        if name != "get_weather" or compound_request:
            return False
        weather_state = current().get("weather")
        return bool(weather_state and weather_state.get("simple", False))

    if named and not compound_request and (allowed_tools is None or "play_song" in allowed_tools):
        reply_out = await dispatch("play_song", {})
        _record_done()
        return reply_out
    if required in (CAPABILITIES, CLOCK) and (allowed_tools is None or required["name"] in allowed_tools):
        reply_out = await dispatch(required["name"], {})
        _record_done()
        return reply_out
    if jev is not None and (allowed_tools is None or "draft_chat_reply" in allowed_tools):
        reply_out = await dispatch("draft_chat_reply", {})
        _record_done()
        return reply_out

    is_local_model = moss.model_id == "local"
    playbook_block = ""
    if not literal_repeat and not required:
        playbook_block, _ = moss.rsi.select_playbook(
            text, allow_personal=allow_own_personal, owner_code=turn_owner,
        )

    personality_id = turn.get("personality_id") or getattr(moss, "personality_id", "partner")
    personality_block = personality_prompt(personality_id) if not literal_repeat else ""
    allow_diet_cadence = bool(plain_prose or DIET_CADENCE_ONLY_TOPIC.fullmatch(plain))
    cadence = (
        conversational_cadence_guidance(text, personality_id=personality_id)
        if (allow_diet_cadence and not one_action and not length and not literal_repeat and not required)
        else ""
    )

    soul_block = ""
    if not literal_repeat and not required:
        try:
            if getattr(moss, "soul", None) is not None:
                soul_block = moss.soul.build_soul_prompt(
                    text, allow_personal=allow_own_personal,
                    already_injected_memory_ids=set(turn.get("memory_ids") or ()),
                    owner_code=turn_owner,
                    max_chars=360 if is_local_model else 1100,
                )
        except Exception as exc:
            getattr(moss, "sag", None) and moss.sag._log(f"soul prompt build failed: {exc}", level="warn")
            soul_block = ""

    sag_auto_block = ""
    if not literal_repeat and not required and not one_action and not FAREWELL_RE.fullmatch(plain) and len(plain) >= 6:
        try:
            if getattr(moss, "sag", None) is not None:
                sag_res = moss.sag.auto_recall(
                    text, allow_private=allow_own_personal,
                    exclude_sources={"memory", "profile"} if turn.get("memory_ids") else (),
                    limit=3, min_score=0.38, max_chars=400,
                    owner_code=turn_owner,
                )
                sag_auto_block = sag_res.get("evidence_text") or ""
        except Exception as exc:
            getattr(moss, "sag", None) and moss.sag._log(f"sag auto_recall failed: {exc}", level="warn")
            sag_auto_block = ""

    wm_skill_lines = []
    if compound_request and not literal_repeat:
        wm_skill_lines.append("多步核对要求：本轮包含多个子请求，请在结束回答前逐项核对是否全部已执行并给出每项结果，切勿遗漏后半句。")
    if not literal_repeat and not required and allow_own_personal:
        try:
            wm_snap = moss.consolidator.latest_snapshot(owner_code=turn_owner) or {}
            wm = wm_snap.get("world_model") or {}
            raw_projs = wm.get("active_projects") or wm.get("projects") or []
            projs = [p if isinstance(p, str) else p.get("name", "") for p in raw_projs[:5]]
            projs = [p for p in projs if p]
            conflicts = [c.get("detail") for c in (wm.get("conflicts") or [])[:2] if isinstance(c, dict) and c.get("detail")]
            collaborators = [str(p) for p in (wm.get("key_collaborators") or [])[:5] if p]
            topics = [str(t) for t in (wm.get("focus_topics") or [])[:6] if t]
            if projs and re.search(r"项目|进度|工作|冲突|周报|日程|安排|计划|总结", text):
                wm_skill_lines.append("个人世界模型活跃项目：" + "、".join(projs))
            if collaborators and re.search(r"谁|联系|协作|同事|负责|汇报|开会|团队", text):
                wm_skill_lines.append("近期关键协作人：" + "、".join(collaborators))
            if topics and re.search(r"关注|方向|重点|周报|总结|工作|领域", text):
                wm_skill_lines.append("近期核心关注主题：" + "、".join(topics))
            if conflicts and re.search(r"冲突|排期|日程|项目|简报|风险", text):
                wm_skill_lines.append("已检出潜在排期冲突：" + "；".join(conflicts))
            if is_host_owner:
                live_timers = [str(it.get("label") or it.get("id")) for it in (moss.timers.snapshot() or [])[:4] if isinstance(it, dict)]
                if live_timers and re.search(r"项目|提醒|待办|日程|安排|计划|简报", text):
                    wm_skill_lines.append("待执行提醒：" + "、".join(live_timers))
                skills_all = moss.jobs.list_skills()
                if skills_all and re.search(r"技能|流程|早报|晚报|周报|快捷|跑一下|执行一下|skill", text, re.I):
                    wm_skill_lines.append("用户已保存专属技能（可调用 run_custom_skill 执行）：" + "、".join(s["name"] for s in skills_all[:10]))
        except Exception as exc:
            getattr(moss, "sag", None) and moss.sag._log(f"world model injection failed: {exc}", level="warn")

    def _fit_lines_budget(block: str, max_chars: int) -> str:
        if not block or len(block) <= max_chars:
            return block
        out_lines = []
        used_len = 0
        for ln in block.splitlines():
            cost = len(ln) + (1 if out_lines else 0)
            if used_len + cost > max_chars:
                if not out_lines:
                    out_lines.append(ln[:max_chars])
                break
            out_lines.append(ln)
            used_len += cost
        return "\n".join(out_lines)

    def _fit_memory_json_budget(block: str, max_chars: int) -> str:
        if not block or len(block) <= max_chars:
            return block
        import json as _json
        search_from = 0
        while True:
            idx = block.find("[", search_from)
            if idx < 0:
                break
            prefix = block[:idx]
            try:
                items, end_idx = _json.JSONDecoder().raw_decode(block, idx)
            except Exception:
                search_from = idx + 1
                continue
            suffix = block[end_idx:]
            if isinstance(items, list):
                while items and len(prefix + _json.dumps(items, ensure_ascii=False) + suffix) > max_chars:
                    items.pop()
                if items:
                    return prefix + _json.dumps(items, ensure_ascii=False) + suffix
                clean_tail = suffix.lstrip(" ,}]")
                if clean_tail and not clean_tail.startswith('"'):
                    return _fit_lines_budget(clean_tail, max_chars)
                if prefix.lstrip().startswith("{"):
                    return ""
                return _fit_lines_budget(suffix.lstrip(), max_chars) if suffix.strip() else ""
            search_from = idx + 1
        return _fit_lines_budget(block, max_chars)

    local_wide_ctx = int(os.environ.get("MOSS_LOCAL_CTX_SIZE", "16384") or 16384) >= 12288
    eff_soul = _fit_lines_budget(soul_block, 640 if local_wide_ctx else 360) if (is_local_model and soul_block) else soul_block
    eff_sag = _fit_lines_budget(sag_auto_block, 560 if local_wide_ctx else 280) if (is_local_model and sag_auto_block) else sag_auto_block
    eff_pers = _fit_lines_budget(personality_block, 320 if local_wide_ctx else 220) if (is_local_model and personality_block) else personality_block
    eff_playbook = _fit_lines_budget(playbook_block, 960 if local_wide_ctx else 620) if (is_local_model and playbook_block) else playbook_block
    if eff_playbook and not turn.get("_scheduled_readonly"):
        kept_ids = [int(m) for m in re.findall(r"\[L#(\d+)/", eff_playbook)]
        if kept_ids:
            moss.rsi.mark_applied(kept_ids, owner_code=turn_owner)
            moss._update(current(), _rsi_applied_ids=kept_ids)
    eff_instructions = _fit_memory_json_budget(instructions, 820 if local_wide_ctx else 460) if (is_local_model and instructions) else instructions
    eff_wm = _fit_lines_budget("\n".join(wm_skill_lines), 520 if local_wide_ctx else 320) if is_local_model else "\n".join(wm_skill_lines)

    base_sys = build_system_prompt(
        model_id=moss.model_id,
        personality_id=personality_id,
        is_host_owner=is_host_owner,
        available_tools=tools,
    )
    system = "\n".join(filter(None, (
        base_sys, eff_soul, eff_sag, eff_pers, eff_playbook,
        eff_wm, eff_instructions, cadence,
        ONE_ACTION_GUIDANCE if one_action else "",
        length.guidance(text) if length else "",
    )))

    if plain_prose and not song_requested:
        moss._update(current(), conversation_replaceable=True)
    terminal = set()
    if not compound_request:
        terminal = {"play_song", "draft_chat_reply"}
        if required is CAPABILITIES:
            terminal.add("list_capabilities")

    tool_trace_list: list[dict] = []

    def on_tool_trace(entry):
        idx = next((i for i, e in enumerate(tool_trace_list) if e["id"] == entry["id"]), None)
        if idx is None:
            tool_trace_list.append(dict(entry))
        else:
            tool_trace_list[idx] = dict(entry)
        try:
            moss._update(current(), tool_trace=list(tool_trace_list))
        except Exception:
            pass

    def on_failover(from_pid, to_pid, to_model, reason):
        try:
            moss._update(current(), failover_provider={"from": from_pid, "to": to_pid, "model": to_model, "reason": reason[:120]})
        except Exception:
            pass

    usage_totals = {"prompt": 0, "completion": 0, "total": 0}

    def on_usage(chunk_usage):
        if not isinstance(chunk_usage, dict):
            return
        usage_totals["prompt"] += int(chunk_usage.get("prompt") or 0)
        usage_totals["completion"] += int(chunk_usage.get("completion") or 0)
        usage_totals["total"] += int(chunk_usage.get("total") or 0)

    local_temp = 0.28 if (is_local_model and plain_prose and not tools and personality_id != "rational") else None
    holo_fn = _ab("run_holo", run_holo)
    run_kwargs = dict(
        client=client, provider_id=moss.model_id, model=moss.model_name,
        system=system, history=history, text=text, tools=tools, dispatch=dispatch,
        on_text=on_text, stream_prose=plain_prose,
        required_tool=required["name"] if required else None,
        terminal_tools=terminal, finish_after_tool=finish_after_tool,
        parallel_tools=PARALLEL_SAFE_TOOLS, idempotent_tools=IDEMPOTENT_READONLY_TOOLS,
        on_tool_trace=on_tool_trace, failover_clients=moss.failover_candidates(),
        on_failover=on_failover, health_tracker=getattr(moss, "provider_health", None),
        cancellation_token=token, temperature=local_temp,
        local_wake_fn=moss.start_local if is_local_model else None,
    )
    if holo_fn is run_holo:
        run_kwargs["on_usage"] = on_usage
        if hasattr(moss, "cascade_candidates"):
            run_kwargs["cascade_clients"] = moss.cascade_candidates()
    answer_text = await holo_fn(**run_kwargs)
    if not literal_repeat and answer_text:
        answer_text = strip_echoed_user_prefix(text, answer_text)
    _personal_tools = {
        "search_user_memory", "save_user_memory", "forget_user_memory",
        "search_notes", "save_note", "manage_note", "search_knowledge",
        "recall_turn_context", "get_situational_briefing", "consolidate_world_model",
    }
    used_personal = bool(
        (set(used) & _personal_tools)
        or (allow_own_personal and (sag_auto_block or wm_skill_lines or ("用户画像档案" in soul_block) or ("长期协作信念" in soul_block)))
    )
    row = _record_done(personal_ctx=used_personal, extra_tokens=usage_totals)
    return answer_text if ("play_song" in used and bool((row.get("media") or {}).get("items"))) else filter_unverified_media_reply(answer_text, row.get("media"))
