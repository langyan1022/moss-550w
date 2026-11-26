import ast
import asyncio
from contextvars import ContextVar
import json
import re
from time import perf_counter
from uuid import uuid4


RUNTIME_TOOL_UNLOCK: ContextVar = ContextVar("moss_runtime_tool_unlock", default=None)


def _trim_system_slots(sys_prompt: str, max_chars: int) -> str:
    """Trim injected system prompt by semantic priority slots rather than raw mid-rule slicing."""
    text = str(sys_prompt or "")
    if len(text) <= max_chars:
        return text
    lines = text.splitlines()
    if len(lines) <= 1:
        return text[:max_chars]

    # Classify lines from lowest eviction priority (dropped first) to highest (kept last)
    def _tier(ln: str) -> int:
        if any(tag in ln for tag in ("说话节奏", "语气参考", "请直接给")):
            return 0
        if ln.startswith(("- [E#", "【SAG", "个人世界模型", "近期关键协作人", "近期核心关注主题")):
            return 1
        if ln.startswith(("- [L#", "历史自我纠错经验")):
            return 2
        if ln.startswith(("用户画像", "长期协作信念")):
            return 3
        return 4  # Core MOSS instructions, safety gates, tool-output mandate, user memories

    indexed = [{"idx": i, "text": ln, "tier": _tier(ln), "drop": False} for i, ln in enumerate(lines)]
    total = sum(len(x["text"]) + 1 for x in indexed) - 1
    for tier in (0, 1, 2, 3):
        if total <= max_chars:
            break
        for item in reversed(indexed):
            if total <= max_chars:
                break
            if not item["drop"] and item["tier"] == tier:
                item["drop"] = True
                total -= len(item["text"]) + 1
    kept = "\n".join(x["text"] for x in indexed if not x["drop"])
    if len(kept) <= max_chars:
        return kept
    return kept[:max_chars]


def _repair_tool_args(raw: str, schema: dict | None = None) -> tuple[dict | None, str | None]:
    """Parse tool arguments JSON with deterministic 4B repair & schema type coercion."""
    txt = str(raw or "").strip()
    if not txt:
        parsed: dict = {}
    else:
        fenced = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", txt)
        if fenced:
            txt = fenced.group(1).strip()
        start = txt.find("{")
        end = txt.rfind("}")
        if start >= 0 and end > start:
            txt = txt[start:end + 1]
        candidates = [txt]
        cleaned = re.sub(r",\s*([}\]])", r"\1", txt)
        candidates.append(cleaned)
        if cleaned.count("{") > cleaned.count("}") and (cleaned.count("{") - cleaned.count("}")) <= 2:
            candidates.append(cleaned + "}" * (cleaned.count("{") - cleaned.count("}")))
        parsed = None
        last_err = None
        for cand in candidates:
            try:
                obj = json.loads(cand)
                if isinstance(obj, dict):
                    parsed = obj
                    break
            except json.JSONDecodeError as exc:
                last_err = exc
        if parsed is None:
            py_cand = re.sub(r"\btrue\b", "True", cleaned)
            py_cand = re.sub(r"\bfalse\b", "False", py_cand)
            py_cand = re.sub(r"\bnull\b", "None", py_cand)
            try:
                obj = ast.literal_eval(py_cand)
                if isinstance(obj, dict):
                    parsed = obj
            except (SyntaxError, ValueError):
                pass
        if parsed is None:
            return None, str(last_err or "需要合法 JSON 对象")

    if not isinstance(schema, dict):
        return parsed, None
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    for key, prop in props.items():
        if key not in parsed or not isinstance(prop, dict):
            continue
        val = parsed[key]
        ptype = prop.get("type")
        if ptype == "integer" and isinstance(val, str) and re.fullmatch(r"-?\d+", val.strip()):
            parsed[key] = int(val.strip())
        elif ptype == "number" and isinstance(val, str) and re.fullmatch(r"-?\d+(?:\.\d+)?", val.strip()):
            parsed[key] = float(val.strip())
        elif ptype == "boolean" and isinstance(val, str) and val.strip().lower() in {"true", "false"}:
            parsed[key] = val.strip().lower() == "true"

    missing = [k for k in (schema.get("required") or ()) if parsed.get(k) in (None, "")]
    if missing:
        return None, f"缺少必需参数：{', '.join(missing)}"
    for key, val in list(parsed.items()):
        prop = props.get(key)
        if isinstance(prop, dict) and isinstance(prop.get("enum"), list) and val not in prop["enum"]:
            return None, f"参数 {key}={val!r} 无效，可选值：{prop['enum']}"
    return parsed, None


def _to_openai_tool(t, *, compact: bool = False):
    desc = str(t.get("description") or "")
    if compact and len(desc) > 84:
        desc = desc[:81].rstrip() + "…"
    return {
        "type": "function",
        "function": {
            "name": t["name"],
            "description": desc,
            "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
        },
    }


class Brain:
    def __init__(self, system, history, tools, stream_once, dispatch, *,
                 parallel_tools=(), idempotent_tools=None, on_tool_trace=None,
                 cancellation_token=None, tool_budget_chars: int = 7200,
                 compact_tools: bool = False):
        self._system = system
        self._messages = list(history)
        self._compact_tools = bool(compact_tools)
        self._tools = [_to_openai_tool(t, compact=self._compact_tools) for t in tools]
        self._tool_names = {t["name"] for t in tools}
        self._tool_schemas = {t["name"]: (t.get("input_schema") or {}) for t in tools}
        self._parallel_tools = frozenset(parallel_tools or ())
        self._idempotent_tools = frozenset(self._parallel_tools if idempotent_tools is None else idempotent_tools)
        self._on_tool_trace = on_tool_trace
        self._cancellation_token = cancellation_token
        self._tool_budget_chars = max(1200, int(tool_budget_chars or 7200))
        self._stream_once = stream_once
        self._dispatch = dispatch
        self._ro_cache = {}
        self._duplicate_hits = 0

    def unlock_tools(self, new_tools: list[dict]) -> list[str]:
        """Dynamically attach allowlisted tools mid-turn (used by run_custom_skill) for subsequent iterations."""
        cap = (16 if self._compact_tools else 24)
        added = []
        for t in new_tools or ():
            if len(self._tools) >= cap:
                break
            name = t.get("name") if isinstance(t, dict) else None
            if not name or name in self._tool_names:
                continue
            self._tools.append(_to_openai_tool(t, compact=self._compact_tools))
            self._tool_names.add(name)
            self._tool_schemas[name] = t.get("input_schema") or {}
            added.append(name)
        return added

    def _is_cancelled(self) -> bool:
        return bool(self._cancellation_token and getattr(self._cancellation_token, "cancelled", False))

    def _emit_trace(self, entry):
        if self._on_tool_trace:
            try:
                self._on_tool_trace(entry)
            except Exception:
                pass

    @staticmethod
    def _sanitize_args_preview(args: dict) -> str:
        if not isinstance(args, dict) or not args:
            return ""
        parts = []
        for k, v in list(args.items())[:4]:
            ks = str(k)
            if any(w in ks.lower() for w in ("token", "secret", "password", "key", "auth")):
                val_s = "***"
            else:
                val_s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
                val_s = str(val_s).replace("\n", " ").strip()[:28]
            parts.append(f"{ks}={val_s}")
        return ", ".join(parts)[:80]

    async def _exec_one(self, tc_id, name, args, *, is_parallel=False):
        if self._is_cancelled():
            raise asyncio.CancelledError("turn cancelled")
        started = perf_counter()
        args_preview = self._sanitize_args_preview(args)
        self._emit_trace({"id": tc_id, "name": name, "status": "running", "ms": 0, "parallel": is_parallel, "args": args_preview})
        try:
            result = await self._dispatch(name, args)
            output = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
            ms = round((perf_counter() - started) * 1000)
            self._emit_trace({"id": tc_id, "name": name, "status": "ok", "ms": ms, "parallel": is_parallel, "args": args_preview})
            return output
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._is_cancelled():
                raise asyncio.CancelledError("turn cancelled") from exc
            ms = round((perf_counter() - started) * 1000)
            self._emit_trace({"id": tc_id, "name": name, "status": "error", "ms": ms, "parallel": is_parallel, "args": args_preview, "error": str(exc)[:120]})
            return f"工具「{name}」执行失败：{exc}"

    async def _run_tools(self, tool_calls):
        prepared = []
        for tc in tool_calls:
            fn = tc["function"]
            if fn["name"] not in self._tool_names:
                prepared.append((tc["id"], None, f"工具「{fn['name']}」不在本轮可用列表中，请改用已提供工具或直接回答。"))
                continue
            args, repair_err = _repair_tool_args(fn["arguments"] or "{}", self._tool_schemas.get(fn["name"]))
            if repair_err or args is None:
                prepared.append((tc["id"], None, f"工具参数错误（{repair_err}），请修正参数后重试。"))
                continue
            if fn["name"] in self._idempotent_tools:
                cache_key = (fn["name"], json.dumps(args, sort_keys=True, ensure_ascii=False))
                if cache_key in self._ro_cache:
                    self._duplicate_hits += 1
                    prev = str(self._ro_cache[cache_key])
                    prepared.append((
                        tc["id"],
                        None,
                        f"本轮已使用相同参数调用过工具「{fn['name']}」，无需重复查询，请直接结合以下结果回答或改用其他工具：\n{prev[:600]}",
                    ))
                    continue
            prepared.append((tc["id"], (fn["name"], args), None))

        valid_indices = [i for i, (_, call, _) in enumerate(prepared) if call is not None]
        can_parallel = (
            len(valid_indices) > 1
            and bool(self._parallel_tools)
            and all(prepared[i][1][0] in self._parallel_tools for i in valid_indices)
        )

        outputs = {}
        if can_parallel:
            tasks = [
                self._exec_one(prepared[i][0], prepared[i][1][0], prepared[i][1][1], is_parallel=True)
                for i in valid_indices
            ]
            results = await asyncio.gather(*tasks)
            for i, out in zip(valid_indices, results):
                outputs[i] = out
        else:
            for i in valid_indices:
                tc_id, (name, args), _ = prepared[i]
                outputs[i] = await self._exec_one(tc_id, name, args, is_parallel=False)

        for i in valid_indices:
            name, args = prepared[i][1]
            out_s = str(outputs[i] or "").strip()
            is_err = (not out_s) or out_s.startswith((
                f"工具「{name}」执行失败", "查询失败", "网络请求失败", "无法连接", "请求超时", "读取失败",
                "抓取网页失败", "网页返回状态码", "当前无法连接", "当前为非管理员", "拒绝", "链接内容类型",
                "无法解析", "无法计算", "未找到", "暂未", "不支持", "格式有误", "请提供",
            ))
            if name in self._idempotent_tools and not is_err:
                cache_key = (name, json.dumps(args, sort_keys=True, ensure_ascii=False))
                self._ro_cache[cache_key] = outputs[i]

        if any(prepared[i][1][0] not in self._idempotent_tools for i in valid_indices):
            self._ro_cache.clear()

        for i, (tc_id, call, err_msg) in enumerate(prepared):
            content = outputs[i] if call is not None else err_msg
            self._messages.append({"role": "tool", "tool_call_id": tc_id, "content": content})

    @staticmethod
    def _merge_alternating(msgs):
        merged = []
        for msg in msgs:
            role = msg.get("role")
            txt = str(msg.get("content") or "").strip()
            if not txt:
                continue
            if merged and merged[-1]["role"] == role:
                merged[-1] = {"role": role, "content": merged[-1]["content"] + "\n\n" + txt}
            else:
                merged.append({"role": role, "content": txt})
        while len(merged) > 1 and merged[0].get("role") != "user":
            merged.pop(0)
        return merged

    def _tool_free_messages(self):
        """Flatten prior tool_calls / tool turns into plain alternating messages with adaptive per-tool budget."""
        tool_total = sum(1 for msg in self._messages if msg.get("role") == "tool" and (msg.get("content") or "").strip())
        per_tool_cap = max(420, min(2400, self._tool_budget_chars // max(1, tool_total)))
        out = []
        tool_notes = []
        for msg in self._messages:
            role = msg.get("role")
            if role == "assistant" and msg.get("tool_calls"):
                txt = (msg.get("content") or "").strip()
                if txt:
                    out.append({"role": "assistant", "content": txt})
                continue
            if role == "tool":
                c = (msg.get("content") or "").strip()
                if c:
                    tool_notes.append(c[:per_tool_cap])
                continue
            if tool_notes:
                out.append({"role": "user", "content": "【本轮已获取的工具结果】\n" + "\n---\n".join(tool_notes)})
                tool_notes.clear()
            out.append(msg)
        if tool_notes:
            out.append({"role": "user", "content": "【本轮已获取的工具结果】\n" + "\n---\n".join(tool_notes)})
        return self._merge_alternating(out)

    async def ask_stream(self, user_text, *, on_text=None, required_tool=None, terminal_tools=(), finish_after_tool=None,
                         stream_prose=False):
        if required_tool is not None and required_tool not in self._tool_names:
            raise ValueError("指定的首轮工具未注册")
        unlock_tok = RUNTIME_TOOL_UNLOCK.set(self.unlock_tools)
        try:
            return await self._ask_stream_impl(
                user_text, on_text=on_text, required_tool=required_tool,
                terminal_tools=terminal_tools, finish_after_tool=finish_after_tool,
                stream_prose=stream_prose,
            )
        finally:
            RUNTIME_TOOL_UNLOCK.reset(unlock_tok)

    async def _ask_stream_impl(self, user_text, *, on_text=None, required_tool=None, terminal_tools=(),
                               finish_after_tool=None, stream_prose=False):
        self._messages.append({"role": "user", "content": user_text})
        seen_ids = set()
        ran_any_tool = False
        tool_call_counts = {}
        retried_required = False
        retried_empty = False
        max_iterations = 8
        for iteration in range(max_iterations):
            if self._is_cancelled():
                raise asyncio.CancelledError("turn cancelled")
            content = ""
            acc = {}
            runaway_tool = ran_any_tool and any(cnt >= max_iterations - 1 for cnt in tool_call_counts.values())
            force_synthesis = ran_any_tool and (
                iteration == max_iterations - 1 or self._duplicate_hits >= 2 or runaway_tool
            )
            active_tools = [] if force_synthesis else self._tools
            required = required_tool if ((iteration == 0 or retried_required) and not force_synthesis) else None
            sys_prompt = (
                self._system + "\n请严格依据本轮已有工具结果直接给出最终中文文字答复，不要再调用任何工具；未查到的事实如实说明。"
                if force_synthesis
                else self._system
            )
            if force_synthesis:
                body_msgs = self._tool_free_messages()
            elif iteration >= 2 and sum(len(str(m.get("content") or "")) for m in self._messages) > int(self._tool_budget_chars * 1.45):
                body_msgs = self._tool_free_messages()
            else:
                body_msgs = self._messages
            if self._compact_tools:
                schema_chars = sum(len(json.dumps(t, ensure_ascii=False)) for t in active_tools)
                total_chars = len(sys_prompt) + schema_chars + sum(len(str(m.get("content") or "")) for m in body_msgs)
                char_limit = self._tool_budget_chars * 2
                if total_chars > char_limit:
                    body_msgs = self._tool_free_messages()
                    while len(body_msgs) > 2 and (len(sys_prompt) + schema_chars + sum(len(str(m.get("content") or "")) for m in body_msgs)) > char_limit:
                        body_msgs = body_msgs[1:]
                    while len(body_msgs) > 1 and body_msgs[0].get("role") != "user":
                        body_msgs = body_msgs[1:]
                    rem_budget = char_limit - schema_chars - sum(len(str(m.get("content") or "")) for m in body_msgs)
                    if rem_budget >= 320 and len(sys_prompt) > rem_budget:
                        sys_prompt = _trim_system_slots(sys_prompt, rem_budget)
            messages = [{"role": "system", "content": sys_prompt}, *body_msgs]
            finish = None
            preview = on_text if (not active_tools or stream_prose or iteration > 0) and not required else None
            async for choice in self._stream_once(messages, active_tools, required):
                if self._is_cancelled():
                    raise asyncio.CancelledError("turn cancelled")
                delta = choice.delta
                if delta.tool_calls and preview:
                    if content:
                        preview("")
                    preview = None
                if delta.content:
                    content += delta.content
                    if preview:
                        preview(content)
                for tc in delta.tool_calls or ():
                    part = acc.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                    part["id"] += tc.id or ""
                    if tc.function:
                        part["name"] += tc.function.name or ""
                        part["arguments"] += tc.function.arguments or ""
                if choice.finish_reason:
                    finish = choice.finish_reason

            if finish not in ("stop", "tool_calls", "length"):
                raise RuntimeError(f"模型未完整返回本轮内容（{finish or '流中断'}）")
            if finish == "length" and acc and content.strip():
                acc.clear()
            if acc and active_tools:
                tcs = []
                batch_ids = set()
                for idx, (_, a) in enumerate(sorted(acc.items())):
                    raw_id = str(a["id"] or "").strip()
                    if not raw_id or raw_id in batch_ids or raw_id in seen_ids:
                        raw_id = f"tc_{iteration}_{idx}_{uuid4().hex[:6]}"
                    batch_ids.add(raw_id)
                    tcs.append({"id": raw_id, "type": "function",
                                "function": {"name": a["name"], "arguments": a["arguments"]}})
                ids = batch_ids
                names = [tc["function"]["name"] for tc in tcs]
                terminal = any(n in terminal_tools for n in names)
                if terminal and len(tcs) != 1:
                    raise ValueError("本轮即时回执工具需要单独调用")
                if required and required not in names:
                    if iteration < max_iterations - 1 and not retried_required:
                        retried_required = True
                        self._messages.append({"role": "user", "content": f"请先调用必需工具 `{required}` 完成本轮核查，再作答。"})
                        continue
                    raise ValueError("模型未按指定工具发出调用")
                retried_required = False
                seen_ids.update(ids)
                for n in names:
                    tool_call_counts[n] = tool_call_counts.get(n, 0) + 1
                self._messages.append({"role": "assistant", "content": content, "tool_calls": tcs})
                await self._run_tools(tcs)
                ran_any_tool = True
                if terminal or (finish_after_tool and len(tcs) == 1 and finish_after_tool(tcs[0]["function"]["name"])):
                    return self._messages[-1]["content"]
                continue
            if required or (active_tools and finish == "tool_calls"):
                if iteration < max_iterations - 1 and not retried_required:
                    retried_required = True
                    hint = f"请立即调用工具 `{required}` 获取数据后再回答。" if required else "请重新发起工具调用或直接输出明确回答。"
                    self._messages.append({"role": "user", "content": hint})
                    continue
                raise ValueError("模型没有返回本轮需要的工具调用")
            if not content.strip():
                if force_synthesis:
                    # Fallback to latest non-empty tool result if model produced empty synthesis
                    last_tool = next((m.get("content", "").strip() for m in reversed(self._messages) if m.get("role") == "tool" and (m.get("content") or "").strip()), "")
                    if last_tool:
                        return last_tool
                if iteration < max_iterations - 1 and not retried_empty:
                    retried_empty = True
                    self._messages.append({"role": "user", "content": "请直接用简洁中文回复刚才的问题。"})
                    continue
                raise ValueError("模型没有返回文字")
            self._messages.append({"role": "assistant", "content": content})
            return content.strip()

        raise RuntimeError("工具调用已达本轮上限")
