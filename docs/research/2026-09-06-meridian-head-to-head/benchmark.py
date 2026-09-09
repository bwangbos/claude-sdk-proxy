import json
import os
import time
import uuid
from pathlib import Path

import httpx

OUT = Path(os.environ.get("H2H_OUTPUT", "/tmp/claude-proxy-h2h/results.json"))
records = []
client = httpx.Client(timeout=150)


def save(row):
    records.append(row)
    OUT.write_text(json.dumps(records, indent=2))
    print(json.dumps(row), flush=True)


def post(base, body):
    path = "/v1/chat/completions"
    if os.environ.get("H2H_DIALECT") == "anthropic":
        path = "/v1/messages"
        converted = []
        for m in body["messages"]:
            if m["role"] == "tool":
                block = {
                    "type": "tool_result",
                    "tool_use_id": m["tool_call_id"],
                    "content": m["content"],
                }
                if (
                    converted
                    and converted[-1]["role"] == "user"
                    and isinstance(converted[-1]["content"], list)
                ):
                    converted[-1]["content"].append(block)
                else:
                    converted.append({"role": "user", "content": [block]})
            elif m.get("tool_calls"):
                converted.append(
                    {
                        "role": "assistant",
                        "content": (
                            [{"type": "text", "text": m["content"]}]
                            if m.get("content")
                            else []
                        )
                        + [
                            {
                                "type": "tool_use",
                                "id": c["id"],
                                "name": c["function"]["name"],
                                "input": json.loads(c["function"]["arguments"]),
                            }
                            for c in m["tool_calls"]
                        ],
                    }
                )
            else:
                converted.append(m)
        body = {**body, "messages": converted}
        if "tools" in body:
            body["tools"] = [
                {
                    "name": t["function"]["name"],
                    "description": t["function"]["description"],
                    "input_schema": t["function"]["parameters"],
                }
                for t in body["tools"]
            ]
    t = time.monotonic()
    r = client.post(base + path, json=body)
    data = r.json()
    if path == "/v1/messages" and r.status_code == 200:
        raw = data
        m = {
            "role": "assistant",
            "content": "".join(
                b["text"] for b in raw["content"] if b["type"] == "text"
            ),
        }
        calls = [
            {
                "id": b["id"],
                "type": "function",
                "function": {"name": b["name"], "arguments": json.dumps(b["input"])},
            }
            for b in raw["content"]
            if b["type"] == "tool_use"
        ]
        if calls:
            m["tool_calls"] = calls
        data = {"choices": [{"message": m}], "raw_anthropic": raw}
    return r.status_code, data, round(time.monotonic() - t, 3)


def body(messages, **kw):
    return dict(model="sonnet", messages=messages, max_tokens=512, **kw)


def msg(data):
    return data["choices"][0]["message"]


for name, port in [("ours", 18317), ("meridian", 18318)]:
    base = f"http://127.0.0.1:{port}"
    for case in os.environ.get(
        "H2H_CASES", "stream,import,parallel_tools,replay"
    ).split(","):
        try:
            marker = "H2H-" + uuid.uuid4().hex[:10]
            if case == "stream":
                b = body(
                    [{"role": "user", "content": "Reply with exactly: " + marker}],
                    stream=True,
                )
                start = time.monotonic()
                first = None
                chunks = []
                events = []
                with client.stream("POST", base + "/v1/chat/completions", json=b) as r:
                    status = r.status_code
                    for line in r.iter_lines():
                        if not line.startswith("data: ") or line == "data: [DONE]":
                            continue
                        e = json.loads(line[6:])
                        events.append(e)
                        for c in e.get("choices", []):
                            s = c.get("delta", {}).get("content")
                            if s:
                                if first is None:
                                    first = round(time.monotonic() - start, 3)
                                chunks.append(s)
                save(
                    dict(
                        project=name,
                        case=case,
                        passed=status == 200 and "".join(chunks).strip() == marker,
                        status=status,
                        ttft=first,
                        seconds=round(time.monotonic() - start, 3),
                        content_chunks=len(chunks),
                        events=events,
                    )
                )
            elif case == "import":
                b = body(
                    [
                        {"role": "user", "content": "Remember the secret marker."},
                        {"role": "assistant", "content": "The marker is " + marker},
                        {
                            "role": "user",
                            "content": (
                                "Return only the marker from the previous "
                                "assistant message."
                            ),
                        },
                    ]
                )
                status, data, sec = post(base, b)
                save(
                    dict(
                        project=name,
                        case=case,
                        passed=status == 200 and marker in msg(data).get("content", ""),
                        status=status,
                        seconds=sec,
                        response=data,
                    )
                )
            elif case == "parallel_tools":
                tools = [
                    {
                        "type": "function",
                        "function": {
                            "name": "lookup",
                            "description": "Look up the secret value for a key.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "key": {"type": "string", "enum": ["alpha", "beta"]}
                                },
                                "required": ["key"],
                                "additionalProperties": False,
                            },
                        },
                    }
                ]
                messages = [
                    {
                        "role": "user",
                        "content": (
                            "Call lookup for alpha and beta in parallel in this "
                            "response. Do not guess their values. After receiving "
                            "both results, output alpha=value;beta=value. Run "
                            + marker
                        ),
                    }
                ]
                b = body(messages, tools=tools)
                status, data, sec = post(base, b)
                calls = msg(data).get("tool_calls", []) if status == 200 else []
                parallel = (
                    len(calls) == 2
                    and {json.loads(c["function"]["arguments"])["key"] for c in calls}
                    == {"alpha", "beta"}
                    and len({c["id"] for c in calls}) == 2
                )
                if parallel:
                    messages.append(msg(data))
                    values = {
                        "alpha": "A-" + uuid.uuid4().hex[:8],
                        "beta": "B-" + uuid.uuid4().hex[:8],
                    }
                    for c in reversed(calls):
                        key = json.loads(c["function"]["arguments"])["key"]
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": c["id"],
                                "content": values[key],
                            }
                        )
                    status2, data2, sec2 = post(base, body(messages, tools=tools))
                    text = msg(data2).get("content", "") if status2 == 200 else ""
                    expected = "alpha=" + values["alpha"] + ";beta=" + values["beta"]
                    ok = status2 == 200 and text.strip() == expected
                else:
                    status2, data2, sec2, ok = None, None, 0, False
                save(
                    dict(
                        project=name,
                        case=case,
                        passed=ok,
                        parallel=parallel,
                        status=status,
                        seconds=sec + sec2,
                        response=data,
                        continuation_status=status2,
                        continuation=data2,
                        submitted_messages=messages,
                    )
                )
            else:
                b = body([{"role": "user", "content": "Reply with exactly " + marker}])
                status, data, sec = post(base, b)
                status2, data2, sec2 = post(base, b)
                save(
                    dict(
                        project=name,
                        case=case,
                        passed=status == status2 == 200 and msg(data) == msg(data2),
                        status=status,
                        seconds=sec,
                        replay_seconds=sec2,
                        exact_response=data == data2,
                        response=data,
                        replay=data2,
                    )
                )
        except Exception as e:
            save(dict(project=name, case=case, passed=False, error=str(e)))
