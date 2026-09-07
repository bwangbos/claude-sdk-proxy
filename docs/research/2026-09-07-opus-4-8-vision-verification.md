# Opus 4.8 vision verification

On 2026-09-07, the production gateway at `a8c4274` was tested with the exact
`opus-4.8` route (`claude-opus-4-8`), fallback disabled, and the locked Agent SDK
0.2.152. The tests used only generated solid-color PNGs, never personal images
or screenshots. No proxy runtime change was required.

## Live API checks

```bash
CLAUDE_PROXY_LIVE=1 CLAUDE_PROXY_LIVE_MODEL=opus-4.8 \
  .venv/bin/pytest -q --strict-markers --forbid-skips -W error \
  tests/live/test_gateway_images.py::test_live_image_understanding
```

The four cases exercise direct image inputs and synthetic screenshot-tool
results through Anthropic Messages and OpenAI Chat Completions. Each requires
recognizing a red image, then imports rewritten history with a blue image and
requires recognizing blue rather than copying the old answer. OpenAI image
tool results use the proxy's documented extension.

Initial result: **3 passed, 1 failed**. All four recognized the initial red
image, including both tool-result paths. The OpenAI tool-result case failed at
the final rebased answer assertion because assistant text was null, with another
tool callback pending; teardown canceled that callback. It was not an HTTP
error or an image rejection. No validator or assertion was weakened.

A single repeat of that exact case passed:

```bash
CLAUDE_PROXY_LIVE=1 CLAUDE_PROXY_LIVE_MODEL=opus-4.8 \
  .venv/bin/pytest -q --strict-markers --forbid-skips -W error \
  'tests/live/test_gateway_images.py::test_live_image_understanding[True-openai]'
```

Result: **1 passed**. This records live model variability, not a claim that the
first full run was green or that a model always stops at the expected boundary.

## Stock Pi check

A temporary isolated harness launched the production app on an ephemeral
localhost port and stock Pi with the Anthropic provider, model `opus-4.8`, and
`input: ["text", "image"]`. Only Pi's `read` tool was enabled; extensions,
skills, context files, prompt templates, and session persistence were disabled.
Pi read a generated red PNG and returned `red`; exit status was zero. The image
and temporary provider configuration were removed when the check finished.

This confirms the recommended Pi image-tool path, not Pi's OpenAI image-tool
layout, which remains unsupported as described in the root README.

## Configuration and scope

Both existing local proxy providers now advertise Opus 4.8 as text/image and no
longer label it text-only. Only that model's display name and `input` fields
changed; a normalized comparison confirmed other models, providers, credentials,
URLs, thinking maps, and compatibility settings were unchanged. Restart Pi to
reload its configuration. The running proxy was not restarted or modified.

The checks establish basic PNG understanding, tool-result transport, and
image-history import on direct Opus 4.8 sessions. They do not verify every image
format, complex visual reasoning, or an Opus 5 → Opus 4.8 fallback involving
images. Existing image limits and the Anthropic-provider recommendation remain
unchanged. The README examples now reflect this capability evidence.
