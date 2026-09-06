from __future__ import annotations

import argparse
import ipaddress
import math
from collections.abc import Sequence

import uvicorn

from claude_sdk_proxy.app import create_app


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
    parser = argparse.ArgumentParser(description="Private localhost Claude gateway")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=_port, default=8317)
    parser.add_argument("--model", action="append")
    parser.add_argument("--max-sessions", type=_positive, default=8)
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
    try:
        host = ipaddress.ip_address(args.host)
    except ValueError:
        parser.error("--host must be a loopback IP address")
    if not host.is_loopback:
        parser.error("--host must be a loopback IP address")
    models = tuple(args.model or ("sonnet",))
    try:
        app = create_app(
            models=models,
            max_sessions=args.max_sessions,
            tool_result_timeout_seconds=args.tool_result_timeout,
        )
    except ValueError as error:
        parser.error(str(error))
    uvicorn.run(app, host=str(host), port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
