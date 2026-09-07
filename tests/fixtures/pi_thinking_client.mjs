import path from "node:path";
import { pathToFileURL } from "node:url";

const scenario = process.argv[2];
const dialect = process.argv[3];
const moduleName = process.env.PI_AI_COMPAT_MODULE;
const baseUrl = process.env.PROXY_BASE_URL;
const firstModelId = process.env.PI_FIRST_MODEL ?? "sonnet-5";
const firstReasoning = process.env.PI_FIRST_REASONING ?? "high";
const secondModelId = process.env.PI_SECOND_MODEL ?? "opus-5";
const secondReasoning = process.env.PI_SECOND_REASONING ?? "low";

if (!moduleName || !baseUrl) {
  throw new Error("PI_AI_COMPAT_MODULE and PROXY_BASE_URL are required");
}
if (!["flow", "probe", "roundtrip"].includes(scenario)) {
  throw new Error(`unknown scenario: ${scenario}`);
}
if (!['openai', 'anthropic'].includes(dialect)) {
  throw new Error(`unknown dialect: ${dialect}`);
}

const moduleSpecifier = path.isAbsolute(moduleName)
  ? pathToFileURL(moduleName).href
  : moduleName;
const { streamSimple } = await import(moduleSpecifier);

const thinkingLevelMap = {
  off: "none",
  minimal: null,
  low: "low",
  medium: "medium",
  high: "high",
  xhigh: "xhigh",
  max: "max",
};

function model(id) {
  return {
    id,
    name: `Claude subscription ${id}`,
    api: dialect === "openai" ? "openai-completions" : "anthropic-messages",
    provider: `claude-subscription-${dialect}`,
    baseUrl: dialect === "openai" ? `${baseUrl}/v1` : baseUrl,
    reasoning: true,
    thinkingLevelMap,
    input: ["text", "image"],
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    contextWindow: 200000,
    maxTokens: 4096,
    compat:
      dialect === "openai"
        ? {
            supportsDeveloperRole: false,
            supportsReasoningEffort: true,
            supportsStore: true,
            supportsUsageInStreaming: true,
            supportsStrictMode: false,
            maxTokensField: "max_tokens",
          }
        : {
            forceAdaptiveThinking: true,
            supportsEagerToolInputStreaming: false,
            supportsStrictTools: false,
            supportsCacheControlOnTools: false,
          },
  };
}

function publicModel(value) {
  return {
    id: value.id,
    api: value.api,
    reasoning: value.reasoning,
    input: value.input,
    thinkingLevelMap: value.thinkingLevelMap,
  };
}

const models = [model(firstModelId)];
if (secondModelId !== firstModelId) {
  models.push(model(secondModelId));
}
const requestPayloads = [];
const customSessionHeaders = [];
const nativeFetch = globalThis.fetch;

async function recordingFetch(input, init) {
  const request = new Request(input, init);
  for (const name of request.headers.keys()) {
    if (name.toLowerCase() === "x-claude-proxy-session") {
      customSessionHeaders.push(name);
    }
  }
  return nativeFetch(input, init);
}

function capturePayload(payload) {
  requestPayloads.push({
    model: payload.model,
    reasoningEffort: payload.reasoning_effort,
    thinking: payload.thinking,
    effort: payload.output_config?.effort,
    messages: payload.messages,
  });
}

function user(text) {
  return { role: "user", content: text, timestamp: Date.now() };
}

function toolResult(call, text) {
  return {
    role: "toolResult",
    toolCallId: call.id,
    toolName: call.name,
    content: [{ type: "text", text }],
    details: {},
    isError: false,
    timestamp: Date.now(),
  };
}

function content(block) {
  if (block.type === "thinking") {
    return {
      type: "thinking",
      thinking: block.thinking,
      ...(block.thinkingSignature
        ? { thinkingSignature: block.thinkingSignature }
        : {}),
      ...(block.redacted ? { redacted: true } : {}),
    };
  }
  if (block.type === "text") {
    return { type: "text", text: block.text };
  }
  return {
    type: "toolCall",
    id: block.id,
    name: block.name,
    arguments: block.arguments,
  };
}

async function runTurn(modelValue, context, reasoning) {
  const eventsSeen = [];
  let assistant = null;
  const options = {
    apiKey: "local-placeholder",
    fetch: recordingFetch,
    maxRetries: 0,
    timeoutMs: 60000,
    onPayload(payload) {
      capturePayload(payload);
    },
  };
  if (reasoning !== "off") {
    options.reasoning = reasoning;
  }
  const events = streamSimple(modelValue, context, options);
  for await (const event of events) {
    const observed = { type: event.type };
    if (event.type === "thinking_delta" || event.type === "text_delta") {
      observed.delta = event.delta;
    } else if (event.type === "toolcall_end") {
      observed.toolCall = {
        name: event.toolCall.name,
        arguments: event.toolCall.arguments,
      };
    } else if (event.type === "done") {
      observed.reason = event.reason;
      assistant = event.message;
    } else if (event.type === "error") {
      throw new Error(
        `${modelValue.id}/${reasoning}: ${event.error.errorMessage ?? event.reason}`,
      );
    }
    eventsSeen.push(observed);
  }
  if (!assistant) {
    throw new Error(`${modelValue.id}/${reasoning} did not complete`);
  }
  return {
    assistant,
    record: {
      model: assistant.model,
      responseModel: assistant.responseModel,
      providerThinkingLevel: assistant.providerThinkingLevel,
      finishReason: assistant.stopReason,
      content: assistant.content.map(content),
      events: eventsSeen,
    },
  };
}

const firstModel = models[0];
if (scenario === "probe") {
  const context = {
    systemPrompt: "",
    messages: [user("Reply with exactly PI_PROBE_OK.")],
  };
  const turn = await runTurn(firstModel, context, firstReasoning);
  process.stdout.write(
    JSON.stringify({
      scenario,
      dialect,
      models: models.map(publicModel),
      customSessionHeaders,
      requestPayloads,
      turns: [turn.record],
      toolResults: [],
    }) + "\n",
  );
  process.exit(0);
}

const echo = {
  name: "echo",
  description: "Echo the requested fixture value.",
  parameters: {
    type: "object",
    properties: { value: { type: "string" } },
    additionalProperties: false,
  },
};
const context = {
  systemPrompt: "",
  messages: [
    user(
      "Remember the context marker PI_CONTEXT_A7. In your next response, call " +
        "echo exactly twice in parallel: once with " +
        "value first and once with value second. " +
        "After both results, return only the integer equal to two plus two.",
    ),
  ],
  tools: [echo],
};
const turns = [];
const first = await runTurn(firstModel, context, firstReasoning);
turns.push(first.record);
if (first.assistant.stopReason !== "toolUse") {
  throw new Error(`first request did not request tools: ${first.assistant.stopReason}`);
}
const calls = first.assistant.content.filter((block) => block.type === "toolCall");
if (calls.length === 0) {
  throw new Error("first request completed without a tool call");
}
const toolResults = calls.map((call, index) => ({
  name: call.name,
  content: index === 0 ? "first" : "second",
}));
context.messages.push(
  first.assistant,
  ...calls.map((call, index) => toolResult(call, toolResults[index].content)),
);
const completed = await runTurn(firstModel, context, firstReasoning);
turns.push(completed.record);
if (completed.assistant.stopReason !== "stop") {
  throw new Error(
    `tool continuation did not complete: ${completed.assistant.stopReason}`,
  );
}
context.messages.push(
  completed.assistant,
  user("Return only the integer equal to three plus four."),
);
const switched = await runTurn(model(secondModelId), context, secondReasoning);
turns.push(switched.record);
context.messages.push(
  switched.assistant,
  user(
    scenario === "roundtrip"
      ? "Return only the integer equal to seven plus eight."
      : "Return only the integer equal to five plus six.",
  ),
);
if (scenario === "flow") {
  const unchanged = await runTurn(model(secondModelId), context, secondReasoning);
  turns.push(unchanged.record);
  context.messages.push(
    unchanged.assistant,
    user("Return only the integer equal to seven plus eight."),
  );
}
const switchedBack = await runTurn(firstModel, context, firstReasoning);
turns.push(switchedBack.record);

process.stdout.write(
  JSON.stringify({
    scenario,
    dialect,
    models: models.map(publicModel),
    customSessionHeaders,
    requestPayloads,
    turns,
    toolResults,
  }) + "\n",
);
