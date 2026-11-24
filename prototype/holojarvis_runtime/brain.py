import asyncio
import json
from time import perf_counter


def _to_openai_tool(t):
    return {
        "type": "function",
        "function": {
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
        },
    }


class Brain:
    def __init__(self, system, history, tools, stream_once, dispatch, *,
                 parallel_tools=(), on_tool_trace=None):
        self._system = system
        self._messages = list(history)
        self._tools = [_to_openai_tool(t) for t in tools]
        self._tool_names = {t["name"] for t in tools}
        self._parallel_tools = frozenset(parallel_tools or ())
        self._on_tool_trace = on_tool_trace
        self._stream_once = stream_once
        self._dispatch = dispatch

    def _emit_trace(self, entry):
        if self._on_tool_trace:
            try:
                self._on_tool_trace(entry)
            except Exception:
                pass

    async def _exec_one(self, tc_id, name, args, *, is_parallel=False):
        started = perf_counter()
        self._emit_trace({"id": tc_id, "name": name, "status": "running", "ms": 0, "parallel": is_parallel})
        try:
            result = await self._dispatch(name, args)
            output = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
            ms = round((perf_counter() - started) * 1000)
            self._emit_trace({"id": tc_id, "name": name, "status": "ok", "ms": ms, "parallel": is_parallel})
            return output
        except Exception as exc:
            ms = round((perf_counter() - started) * 1000)
            self._emit_trace({"id": tc_id, "name": name, "status": "error", "ms": ms, "parallel": is_parallel, "error": str(exc)[:120]})
            return f"工具「{name}」执行失败：{exc}"

    async def _run_tools(self, tool_calls):
        prepared = []
        for tc in tool_calls:
            fn = tc["function"]
            if fn["name"] not in self._tool_names:
                prepared.append((tc["id"], None, f"工具「{fn['name']}」不在本轮可用列表中，请改用已提供工具或直接回答。"))
                continue
            try:
                args = json.loads(fn["arguments"] or "{}")
                if not isinstance(args, dict):
                    raise ValueError("参数必须是 JSON 对象")
            except (json.JSONDecodeError, ValueError) as exc:
                prepared.append((tc["id"], None, f"工具参数 JSON 格式错误（{exc}），请修正参数后重试。"))
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

        for i, (tc_id, call, err_msg) in enumerate(prepared):
            content = outputs[i] if call is not None else err_msg
            self._messages.append({"role": "tool", "tool_call_id": tc_id, "content": content})

    async def ask_stream(self, user_text, *, on_text=None, required_tool=None, terminal_tools=(), finish_after_tool=None,
                         stream_prose=False):
        if required_tool is not None and required_tool not in self._tool_names:
            raise ValueError("指定的首轮工具未注册")
        self._messages.append({"role": "user", "content": user_text})
        seen_ids = set()
        for iteration in range(8):
            content = ""
            acc = {}
            required = required_tool if iteration == 0 else None
            messages = [{"role": "system", "content": self._system}, *self._messages]
            finish = None
            preview = on_text if (not self._tools or stream_prose or iteration > 0) and not required else None
            async for choice in self._stream_once(messages, self._tools, required):
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
            if acc:
                tcs = [{"id": a["id"], "type": "function",
                        "function": {"name": a["name"], "arguments": a["arguments"]}}
                       for _, a in sorted(acc.items())]
                ids = {tc["id"] for tc in tcs}
                if "" in ids or len(ids) != len(tcs) or ids & seen_ids:
                    raise ValueError("模型工具调用编号缺失或重复")
                if required and [tc["function"]["name"] for tc in tcs] != [required]:
                    raise ValueError("模型未按指定工具发出调用")
                terminal = any(tc["function"]["name"] in terminal_tools for tc in tcs)
                if terminal and len(tcs) != 1:
                    raise ValueError("本轮即时回执工具需要单独调用")
                seen_ids.update(ids)
                self._messages.append({"role": "assistant", "content": content, "tool_calls": tcs})
                await self._run_tools(tcs)
                if terminal or (finish_after_tool and len(tcs) == 1 and finish_after_tool(tcs[0]["function"]["name"])):
                    return self._messages[-1]["content"]
                continue
            if required or finish == "tool_calls":
                raise ValueError("模型没有返回本轮需要的工具调用")
            if not content.strip():
                raise ValueError("模型没有返回文字")
            self._messages.append({"role": "assistant", "content": content})
            return content.strip()

        raise RuntimeError("工具调用已达本轮上限")
