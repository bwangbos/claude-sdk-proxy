from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import math
import sys
import webbrowser
from collections.abc import Sequence

import uvicorn

from quaylet.app import create_app
from quaylet.diagnostics import close_logging, configure_logging
from quaylet.openai_subscription.auth import CredentialManager


def _port(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _positive(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return number


def _positive_finite_float(value: str) -> float:
    number = float(value)
    if number <= 0 or not math.isfinite(number):
        raise argparse.ArgumentTypeError("value must be a positive finite number")
    return number


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Quaylet local API gateway for Claude and ChatGPT subscriptions"
    )
    commands = parser.add_subparsers(dest="command")
    for command in ("login", "logout", "auth-status"):
        subparser = commands.add_parser(command)
        subparser.add_argument("provider", choices=("openai",))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=_port, default=8317)
    parser.add_argument(
        "--model",
        action="append",
        help="Model to expose; required for serving, repeatable",
    )
    parser.add_argument("--refusal-fallback", choices=("off", "auto"), default="off")
    parser.add_argument("--max-sessions", type=_positive, default=8)
    parser.add_argument(
        "--log", metavar="PATH", help="Append redacted diagnostics to a file"
    )
    parser.add_argument(
        "--log-json",
        action="store_true",
        help="Emit JSON diagnostics (stderr unless --log is set)",
    )
    parser.add_argument(
        "--tool-result-timeout",
        type=_positive_finite_float,
        default=300.0,
        metavar="SECONDS",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command is not None:
        return asyncio.run(_auth_command(args.command))
    if not args.model:
        parser.error("at least one --model is required to start the server")
    try:
        host = ipaddress.ip_address(args.host)
    except ValueError:
        parser.error("--host must be a loopback IP address")
    if not host.is_loopback:
        parser.error("--host must be a loopback IP address")
    models = tuple(args.model)
    try:
        app = create_app(
            models=models,
            max_sessions=args.max_sessions,
            tool_result_timeout_seconds=args.tool_result_timeout,
            refusal_fallback=args.refusal_fallback,
        )
    except ValueError as error:
        parser.error(str(error))
    handler = None
    try:
        if args.log is not None or args.log_json:
            handler = configure_logging(args.log, args.log_json)
        uvicorn.run(app, host=str(host), port=args.port)
    finally:
        if handler is not None:
            close_logging(handler, file_output=args.log is not None)
    return 0


async def _auth_command(command: str) -> int:
    manager = CredentialManager()
    try:
        if command == "login":
            await manager.login(open_browser=_open_browser)
            print("OpenAI subscription login saved for this proxy.")
        elif command == "logout":
            await manager.logout()
            print("Proxy OpenAI subscription credentials removed.")
        else:
            print(json.dumps(await manager.status()))
        return 0
    except Exception:
        print("OpenAI authentication operation failed.", file=sys.stderr)
        return 1
    finally:
        await manager.close()


def _open_browser(url: str) -> None:
    webbrowser.open(url)


if __name__ == "__main__":
    raise SystemExit(main())
