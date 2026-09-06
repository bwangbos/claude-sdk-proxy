from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path

from tests.fixtures.image_data import solid_png
from tests.integration.pi_gateway_support import communicate_or_reap


def image_provider(base_url: str) -> dict:
    return {
        "baseUrl": base_url,
        "api": "anthropic-messages",
        "apiKey": "local-placeholder",
        "headers": {"anthropic-beta": ""},
        "compat": {
            "supportsEagerToolInputStreaming": False,
            "supportsStrictTools": False,
            "supportsCacheControlOnTools": False,
        },
        "models": [
            {
                "id": "sonnet",
                "name": "Claude local images",
                "reasoning": False,
                "input": ["text", "image"],
                "contextWindow": 200000,
                "maxTokens": 4096,
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            }
        ],
    }


async def run_pi_image(base_url: str, directory: Path) -> str:
    config = directory / "pi-config"
    config.mkdir()
    (config / "models.json").write_text(
        json.dumps({"providers": {"proxy-images": image_provider(base_url)}})
    )
    fixture = directory / "color.png"
    fixture.write_bytes(base64.b64decode(solid_png()))
    process = await asyncio.create_subprocess_exec(
        "pi",
        "--provider",
        "proxy-images",
        "--model",
        "sonnet",
        "--no-session",
        "--no-extensions",
        "--no-skills",
        "--no-context-files",
        "--no-prompt-templates",
        "--tools",
        "read",
        "--system-prompt",
        "Use the read tool when requested.",
        "-p",
        f"Use read on {fixture}, then name the solid color you see in the image. "
        "Reply with one lowercase color word.",
        cwd=directory,
        env={**os.environ, "PI_CODING_AGENT_DIR": str(config), "PI_OFFLINE": "1"},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await communicate_or_reap(process, timeout_seconds=90)
    assert process.returncode == 0, stderr.decode()
    return stdout.decode()
