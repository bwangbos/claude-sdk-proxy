import path from "node:path";
import { pathToFileURL } from "node:url";

const scenario = process.argv[2];
const moduleName = process.env.PI_AI_MODULE;
const baseUrl = process.env.PROXY_BASE_URL;

if (!moduleName || !baseUrl) {
  throw new Error("PI_AI_MODULE and PROXY_BASE_URL are required");
}
if (!["linear", "retry", "abort", "timeout", "rebase"].includes(scenario)) {
  throw new Error(`unknown scenario: ${scenario}`);
}

const moduleSpecifier = path.isAbsolute(moduleName)
  ? pathToFileURL(moduleName).href
  : moduleName;
const { streamSimple } = await import(moduleSpecifier);

const model = {
  id: "sonnet",
  name: "Claude subscription proxy",
  api: "openai-completions",
  provider: "claude-proxy",
  baseUrl,
  reasoning: false,
  input: ["text"],
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  contextWindow: 200000,
  maxTokens: 4096,
  compat: {
    supportsDeveloperRole: false,
    supportsReasoningEffort: false,
    supportsStore: true,
    supportsUsageInStreaming: true,
    supportsStrictMode: false,
    maxTokensField: "max_tokens",
  },
};

const sessionId = `pi-${scenario}`;
const requestPayloads = [];

function user(text) {
  return { role: "user", content: text, timestamp: Date.now() };
}

async function runTurn(context, { abortAfterText = false } = {}) {
  const controller = new AbortController();
  const turn = { text: "", finishReason: null, error: null, usage: null };
  let assistant = null;
  const events = streamSimple(model, context, {
    apiKey: "local-placeholder",
    headers:
      scenario === "rebase"
        ? undefined
        : { "X-Claude-Proxy-Session": sessionId },
    signal: controller.signal,
    maxRetries: 0,
    onPayload(payload) {
      requestPayloads.push({
        maxTokens: payload?.max_tokens,
        store: payload?.store,
        includeUsage: payload?.stream_options?.include_usage,
        hasTemperature: payload?.temperature !== undefined,
        hasReasoningEffort: payload?.reasoning_effort !== undefined,
      });
    },
  });

  try {
    for await (const event of events) {
      if (event.type === "text_delta") {
        turn.text += event.delta;
        if (abortAfterText) {
          controller.abort();
        }
      } else if (event.type === "done") {
        turn.finishReason = event.reason;
        assistant = event.message;
        turn.usage = {
          input: event.message.usage.input,
          output: event.message.usage.output,
          totalTokens: event.message.usage.totalTokens,
        };
      } else if (event.type === "error") {
        turn.finishReason = event.reason;
        turn.error = event.error.errorMessage || "provider stream error";
      }
    }
  } catch (error) {
    turn.finishReason = controller.signal.aborted ? "aborted" : "error";
    turn.error = error instanceof Error ? error.message : String(error);
  }
  return { turn, assistant };
}

const turns = [];
if (scenario === "linear" || scenario === "rebase") {
  const context = { systemPrompt: "", messages: [user("first")] };
  const first = await runTurn(context);
  turns.push(first.turn);
  if (!first.assistant) {
    throw new Error(`${scenario} first turn did not complete`);
  }
  if (scenario === "rebase") {
    context.messages = [
      user(
        "The conversation history before this point was compacted into " +
          "the following summary:\n\n<summary>\nThe first answer was " +
          "recorded. The retained marker is PI_COMPACTION_CYAN_913.\n</summary>",
      ),
      user("Return only the exact retained marker."),
    ];
  } else {
    context.messages.push(first.assistant, user("second"));
  }
  turns.push((await runTurn(context)).turn);
} else if (scenario === "retry") {
  const context = { systemPrompt: "", messages: [user("retry")] };
  turns.push((await runTurn(context)).turn);
  turns.push((await runTurn(context)).turn);
} else {
  const context = { systemPrompt: "", messages: [user(scenario)] };
  turns.push(
    (await runTurn(context, { abortAfterText: scenario === "abort" })).turn,
  );
}

process.stdout.write(
  JSON.stringify({ scenario, turns, requestPayloads }) + "\n",
);
