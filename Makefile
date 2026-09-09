.DEFAULT_GOAL := check

PYTEST_RELEASE_FLAGS := --strict-markers --forbid-skips -W error
CLANG := $(shell xcrun --find clang)
SDKROOT := $(shell xcrun --show-sdk-path)
C17_FLAGS := -std=c17 -Wall -Wextra -Werror -pedantic

.PHONY: native unit darwin gateway integration openai-subscription release-offline live-core live-tools live-openai check

native: build/bin/darwin-probe build/lib/libquaylet_lifecycle.dylib build/lib/libquaylet_lifecycle_fault.dylib build/bin/quaylet-supervisor build/bin/quaylet-supervisor-probe build/bin/quaylet-anchor build/bin/quaylet-probe-child build/bin/quaylet-task6-test-cli

build/bin:
	mkdir -p $@

build/lib:
	mkdir -p $@

build/bin/darwin-probe: native/darwin_probe.c | build/bin
	$(CLANG) $(C17_FLAGS) -isysroot $(SDKROOT) $< -o $@ -lproc

build/lib/libquaylet_lifecycle.dylib: native/lifecycle.c native/lifecycle.h | build/lib
	$(CLANG) $(C17_FLAGS) -isysroot $(SDKROOT) -dynamiclib -pthread native/lifecycle.c -o $@ -install_name @rpath/libquaylet_lifecycle.dylib -lproc

build/lib/libquaylet_lifecycle_fault.dylib: native/lifecycle.c native/lifecycle.h | build/lib
	$(CLANG) $(C17_FLAGS) -isysroot $(SDKROOT) -dynamiclib -pthread -DCPL_ENABLE_FAULT_INJECTION native/lifecycle.c -o $@ -install_name @rpath/libquaylet_lifecycle_fault.dylib -lproc

build/bin/quaylet-supervisor: native/quaylet_supervisor.c native/lifecycle.h build/lib/libquaylet_lifecycle.dylib | build/bin
	$(CLANG) $(C17_FLAGS) -isysroot $(SDKROOT) native/quaylet_supervisor.c -Lbuild/lib -lquaylet_lifecycle -Wl,-rpath,@loader_path/../lib -o $@ -lproc

build/bin/quaylet-supervisor-probe: native/quaylet_supervisor.c native/lifecycle.h build/lib/libquaylet_lifecycle.dylib | build/bin
	$(CLANG) $(C17_FLAGS) -isysroot $(SDKROOT) -DCPL_ENABLE_TASK5_INJECTIONS native/quaylet_supervisor.c -Lbuild/lib -lquaylet_lifecycle -Wl,-rpath,@loader_path/../lib -o $@ -lproc

build/bin/quaylet-anchor: native/quaylet_anchor.c native/lifecycle.h build/lib/libquaylet_lifecycle.dylib | build/bin
	$(CLANG) $(C17_FLAGS) -isysroot $(SDKROOT) native/quaylet_anchor.c -Lbuild/lib -lquaylet_lifecycle -Wl,-rpath,@loader_path/../lib -o $@

build/bin/quaylet-probe-child: native/quaylet_probe_child.c | build/bin
	$(CLANG) $(C17_FLAGS) -isysroot $(SDKROOT) native/quaylet_probe_child.c -o $@

build/bin/quaylet-task6-test-cli: native/quaylet_task6_test_cli.c | build/bin
	$(CLANG) $(C17_FLAGS) -isysroot $(SDKROOT) native/quaylet_task6_test_cli.c -o $@

unit: native
	uv run pytest $(PYTEST_RELEASE_FLAGS) tests/unit

darwin: native
	uv run pytest $(PYTEST_RELEASE_FLAGS) tests/darwin

gateway:
	uv run pytest $(PYTEST_RELEASE_FLAGS) tests/gateway

integration:
	uv run pytest $(PYTEST_RELEASE_FLAGS) tests/integration

openai-subscription:
	uv run pytest $(PYTEST_RELEASE_FLAGS) tests/openai_subscription

release-offline: unit darwin gateway integration openai-subscription
	uv run ruff check .
	uv run mypy src/quaylet

live-core:
	uv run pytest $(PYTEST_RELEASE_FLAGS) tests/live -m live

live-tools:
	uv run pytest $(PYTEST_RELEASE_FLAGS) tests/live -m live -k tool

live-openai:
	uv run pytest $(PYTEST_RELEASE_FLAGS) tests/live_openai -m live

check: unit darwin gateway openai-subscription
	uv run ruff check .
	uv run mypy src/quaylet
