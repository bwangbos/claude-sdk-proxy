.DEFAULT_GOAL := check

PYTEST_RELEASE_FLAGS := --strict-markers --forbid-skips -W error
CLANG := $(shell xcrun --find clang)
SDKROOT := $(shell xcrun --show-sdk-path)
C17_FLAGS := -std=c17 -Wall -Wextra -Werror -pedantic

.PHONY: native unit darwin live-core live-tools check

native: build/bin/darwin-probe build/lib/libclaude_proxy_lifecycle.dylib build/lib/libclaude_proxy_lifecycle_fault.dylib build/bin/claude-proxy-supervisor build/bin/claude-proxy-anchor

build/bin:
	mkdir -p $@

build/lib:
	mkdir -p $@

build/bin/darwin-probe: native/darwin_probe.c | build/bin
	$(CLANG) $(C17_FLAGS) -isysroot $(SDKROOT) $< -o $@ -lproc

build/lib/libclaude_proxy_lifecycle.dylib: native/lifecycle.c native/lifecycle.h | build/lib
	$(CLANG) $(C17_FLAGS) -isysroot $(SDKROOT) -dynamiclib -pthread native/lifecycle.c -o $@ -install_name @rpath/libclaude_proxy_lifecycle.dylib -lproc

build/lib/libclaude_proxy_lifecycle_fault.dylib: native/lifecycle.c native/lifecycle.h | build/lib
	$(CLANG) $(C17_FLAGS) -isysroot $(SDKROOT) -dynamiclib -pthread -DCPL_ENABLE_FAULT_INJECTION native/lifecycle.c -o $@ -install_name @rpath/libclaude_proxy_lifecycle_fault.dylib -lproc

build/bin/claude-proxy-supervisor: native/claude_supervisor.c native/lifecycle.h build/lib/libclaude_proxy_lifecycle.dylib | build/bin
	$(CLANG) $(C17_FLAGS) -isysroot $(SDKROOT) native/claude_supervisor.c -Lbuild/lib -lclaude_proxy_lifecycle -Wl,-rpath,@loader_path/../lib -o $@ -lproc

build/bin/claude-proxy-anchor: native/claude_anchor.c native/lifecycle.h build/lib/libclaude_proxy_lifecycle.dylib | build/bin
	$(CLANG) $(C17_FLAGS) -isysroot $(SDKROOT) native/claude_anchor.c -Lbuild/lib -lclaude_proxy_lifecycle -Wl,-rpath,@loader_path/../lib -o $@

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
