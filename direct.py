import argparse
import asyncio
import json
import os
import urllib.request
from pathlib import Path

from lab_common import PROMPT, SYSTEM, TOOL_DESCRIPTION, Trial


def complete(messages):
    tool = {"type": "function", "function": {
        "name": "save_draft", "description": TOOL_DESCRIPTION,
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    }}
    body = {"model": os.environ["MOSS_MODEL"], "messages": messages, "tools": [tool],
            "temperature": 0, "max_tokens": 160, "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request(os.environ["MOSS_BASE_URL"] + "/chat/completions",
                                 data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=120) as response:
        return json.load(response)


async def run(out):
    trial = Trial(out, "draft")
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": PROMPT}]
    usage = []
    trial.start()
    try:
        first = complete(messages)
        usage.append(first["usage"])
        choice = first["choices"][0]["message"]
        call, = choice["tool_calls"]
        if call["function"]["name"] != "save_draft":
            raise ValueError(call)
        receipt = await trial.save_draft(**json.loads(call["function"]["arguments"]))
        messages.extend([choice, {"role": "tool", "tool_call_id": call["id"], "content": receipt}])
        second = complete(messages)
        usage.append(second["usage"])
        await trial.finish(reply=second["choices"][0]["message"]["content"], usage=usage)
    except Exception as error:
        await trial.finish(error=repr(error), usage=usage)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["draft"])
    parser.add_argument("--out", type=Path, required=True)
    asyncio.run(run(parser.parse_args().out))
