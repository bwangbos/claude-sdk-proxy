.DEFAULT_GOAL := check

PYTEST_RELEASE_FLAGS := --strict-markers --forbid-skips -W error
CLANG := $(shell xcrun --find clang)
C17_FLAGS := -std=c17 -Wall -Wextra -Werror -pedantic

.PHONY: native unit darwin live-core live-tools check

native:
	printf 'int claude_sdk_proxy_native_smoke;\n' | $(CLANG) $(C17_FLAGS) -fsyntax-only -x c -

unit:
	uv run pytest $(PYTEST_RELEASE_FLAGS) tests/unit

darwin:
	uv run pytest $(PYTEST_RELEASE_FLAGS) tests/darwin

live-core:
	uv run pytest $(PYTEST_RELEASE_FLAGS) tests/live -m live

live-tools:
	uv run pytest $(PYTEST_RELEASE_FLAGS) tests/live -m live -k tool

check: native unit
	uv run ruff check .
	uv run mypy src/claude_sdk_proxy
