#!/usr/bin/env python3
from __future__ import annotations

import json
import sys


def write(message: object) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def main() -> None:
    if "-v" in sys.argv:
        print("2.1.0")
        return
    for line in sys.stdin:
        incoming = json.loads(line)
        if incoming.get("type") == "control_request":
            write(
                {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": incoming["request_id"],
                        "response": {},
                    },
                }
            )
            continue
        if incoming.get("type") != "user":
            continue
        text = "\x00" * (256 * 1024)
        write(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": f"sdk-tool-{index}",
                            "content": text,
                            "is_error": False,
                        }
                        for index in range(4)
                    ],
                },
            }
        )
        write(
            {
                "type": "result",
                "subtype": "success",
                "duration_ms": 0,
                "duration_api_ms": 0,
                "is_error": False,
                "num_turns": 1,
                "session_id": "sdk-1",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        )


if __name__ == "__main__":
    main()
