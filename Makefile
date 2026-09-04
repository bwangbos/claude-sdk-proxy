.DEFAULT_GOAL := check

PYTEST_RELEASE_FLAGS := --strict-markers --forbid-skips -W error
CLANG := $(shell xcrun --find clang)
SDKROOT := $(shell xcrun --show-sdk-path)
C17_FLAGS := -std=c17 -Wall -Wextra -Werror -pedantic

.PHONY: native unit darwin gateway integration release-offline live-core live-tools check

native: build/bin/darwin-probe build/lib/libclaude_proxy_lifecycle.dylib build/lib/libclaude_proxy_lifecycle_fault.dylib build/bin/claude-proxy-supervisor build/bin/claude-proxy-supervisor-probe build/bin/claude-proxy-anchor build/bin/claude-proxy-probe-child build/bin/claude-proxy-task6-test-cli

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

build/bin/claude-proxy-supervisor-probe: native/claude_supervisor.c native/lifecycle.h build/lib/libclaude_proxy_lifecycle.dylib | build/bin
	$(CLANG) $(C17_FLAGS) -isysroot $(SDKROOT) -DCPL_ENABLE_TASK5_INJECTIONS native/claude_supervisor.c -Lbuild/lib -lclaude_proxy_lifecycle -Wl,-rpath,@loader_path/../lib -o $@ -lproc

build/bin/claude-proxy-anchor: native/claude_anchor.c native/lifecycle.h build/lib/libclaude_proxy_lifecycle.dylib | build/bin
	$(CLANG) $(C17_FLAGS) -isysroot $(SDKROOT) native/claude_anchor.c -Lbuild/lib -lclaude_proxy_lifecycle -Wl,-rpath,@loader_path/../lib -o $@

build/bin/claude-proxy-probe-child: native/claude_probe_child.c | build/bin
	$(CLANG) $(C17_FLAGS) -isysroot $(SDKROOT) native/claude_probe_child.c -o $@

build/bin/claude-proxy-task6-test-cli: native/claude_task6_test_cli.c | build/bin
	$(CLANG) $(C17_FLAGS) -isysroot $(SDKROOT) native/claude_task6_test_cli.c -o $@

unit: native
	uv run pytest $(PYTEST_RELEASE_FLAGS) tests/unit

darwin: native
	uv run pytest $(PYTEST_RELEASE_FLAGS) tests/darwin

gateway:
	uv run pytest $(PYTEST_RELEASE_FLAGS) tests/gateway

integration:
	uv run pytest $(PYTEST_RELEASE_FLAGS) tests/integration

release-offline: unit darwin gateway integration
	uv run ruff check .
	uv run mypy src/claude_sdk_proxy

live-core:
	uv run pytest $(PYTEST_RELEASE_FLAGS) tests/live -m live

live-tools:
	uv run pytest $(PYTEST_RELEASE_FLAGS) tests/live -m live -k tool

check: unit darwin gateway
	uv run ruff check .
	uv run mypy src/claude_sdk_proxy
