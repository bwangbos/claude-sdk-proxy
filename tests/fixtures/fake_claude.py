#!/usr/bin/env python3
import json
import os
import signal
import sys
from pathlib import Path

capture_path = Path(os.environ["FAKE_CLAUDE_CAPTURE"])
capture_path.write_text(
    json.dumps(
        {
            "argv": sys.argv[1:],
            "stdin": sys.stdin.readline(),
            "cwd": os.getcwd(),
            "cwd_entries": sorted(os.listdir()),
            "environment_names": sorted(os.environ),
            "pid": os.getpid(),
        }
    ),
    encoding="utf-8",
)

mode = os.environ.get("FAKE_CLAUDE_MODE", "success")
if mode == "hang":
    marker = Path(os.environ["FAKE_CLAUDE_TERM_MARKER"])

    def mark_termination(_signum: int, _frame: object) -> None:
        marker.touch()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, mark_termination)
    print(
        '{"type":"stream_event","event":{"type":"content_block_delta",'
        '"delta":{"type":"text_delta","text":"hel"}}}',
        flush=True,
    )
    signal.pause()
elif mode == "stderr":
    sys.stderr.write("x" * (64 * 1024 + 1))
    sys.stderr.flush()
    raise SystemExit(7)
elif mode == "malformed":
    print("not-json", flush=True)
elif mode == "noresult":
    print(
        '{"type":"stream_event","event":{"type":"content_block_delta",'
        '"delta":{"type":"text_delta","text":"hel"}}}',
        flush=True,
    )
elif mode == "tool":
    print(
        '{"type":"stream_event","event":{"type":"content_block_delta",'
        '"delta":{"type":"tool_use"}}}',
        flush=True,
    )
else:
    print(
        '{"type":"stream_event","event":{"type":"content_block_delta",'
        '"delta":{"type":"text_delta","text":"hel"}}}',
        flush=True,
    )
    print(
        '{"type":"stream_event","event":{"type":"content_block_delta",'
        '"delta":{"type":"text_delta","text":"lo"}}}',
        flush=True,
    )
    print(
        '{"type":"result","subtype":"success","is_error":false,'
        '"duration_ms":0,"duration_api_ms":0,"num_turns":1,'
        '"result":"hello","session_id":"fake-session",'
        '"total_cost_usd":0,"stop_reason":"end_turn",'
        '"usage":{"output_tokens":1}}',
        flush=True,
    )
    if mode == "late":
        print(
            '{"type":"stream_event","event":{"type":"content_block_delta",'
            '"delta":{"type":"text_delta","text":"late"}}}',
            flush=True,
        )
    elif mode == "duplicate":
        print(
            '{"type":"result","subtype":"success","is_error":false,'
            '"duration_ms":0,"duration_api_ms":0,"num_turns":1,'
            '"result":"hello","session_id":"fake-session-2",'
            '"total_cost_usd":0,"stop_reason":"end_turn",'
            '"usage":{"output_tokens":1}}',
            flush=True,
        )
