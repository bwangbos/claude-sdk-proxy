import { readFile } from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";

const aiModuleName = process.env.PI_AI_MODULE;
const agentModuleName = process.env.PI_AGENT_MODULE;
const codingAgentModuleName = process.env.PI_CODING_AGENT_MODULE;
const baseUrl = process.env.PROXY_BASE_URL;

if (!aiModuleName || !agentModuleName || !codingAgentModuleName || !baseUrl) {
  throw new Error(
    "PI_AI_MODULE, PI_AGENT_MODULE, PI_CODING_AGENT_MODULE, and " +
      "PROXY_BASE_URL are required",
  );
}

function moduleSpecifier(moduleName) {
  return path.isAbsolute(moduleName)
    ? pathToFileURL(moduleName).href
    : moduleName;
}

async function installedPackageVersion(moduleName, expectedName) {
  if (!path.isAbsolute(moduleName)) {
    throw new Error(`version discovery requires an absolute path: ${moduleName}`);
  }
  let directory = path.dirname(moduleName);
  while (true) {
    try {
      const manifest = JSON.parse(
        await readFile(path.join(directory, "package.json"), "utf8"),
      );
      if (manifest.name === expectedName) {
        if (typeof manifest.version !== "string") {
          throw new Error(`${expectedName} package version is invalid`);
        }
        return manifest.version;
      }
    } catch (error) {
      if (error?.code !== "ENOENT") {
        throw error;
      }
    }
    const parent = path.dirname(directory);
    if (parent === directory) {
      throw new Error(`could not find ${expectedName} package.json`);
    }
    directory = parent;
  }
}

const packageVersions = {
  "pi-coding-agent": await installedPackageVersion(
    codingAgentModuleName,
    "@earendil-works/pi-coding-agent",
  ),
  "pi-agent-core": await installedPackageVersion(
    agentModuleName,
    "@earendil-works/pi-agent-core",
  ),
  "pi-ai": await installedPackageVersion(
    aiModuleName,
    "@earendil-works/pi-ai",
  ),
};

const { streamSimple } = await import(moduleSpecifier(aiModuleName));
const { Agent } = await import(moduleSpecifier(agentModuleName));

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

const executions = [];
const echo = {
  name: "echo",
  label: "Echo",
  description: "Echo a value",
  parameters: {
    type: "object",
    properties: { value: { type: "string" } },
    required: ["value"],
    additionalProperties: false,
  },
  async execute(_toolCallId, params) {
    executions.push({ value: params.value });
    return {
      content: [{ type: "text", text: `echo:${params.value}` }],
      details: {},
    };
  },
};

const requests = [];
const customSessionHeaders = [];
const nativeFetch = globalThis.fetch;

async function recordingFetch(input, init) {
  const request = new Request(input, init);
  const headers = Object.fromEntries(request.headers.entries());
  for (const name of Object.keys(headers)) {
    if (name.toLowerCase() === "x-claude-proxy-session") {
      customSessionHeaders.push(name);
    }
  }
  const body = await request.clone().json();
  requests.push({
    messageRoles: body.messages.map((message) => message.role),
    messages: body.messages,
    tools: body.tools,
    maxTokens: body.max_tokens,
    store: body.store,
    includeUsage: body.stream_options?.include_usage,
  });
  return nativeFetch(input, init);
}

function agentStream(modelValue, context, options = {}) {
  return streamSimple(modelValue, context, {
    ...options,
    apiKey: "local-placeholder",
    fetch: recordingFetch,
    maxRetries: 0,
  });
}

const agent = new Agent({
  initialState: {
    systemPrompt: "",
    model,
    thinkingLevel: "off",
    tools: [echo],
  },
  streamFn: agentStream,
});

const toolPrompt =
  process.env.PI_TOOL_PROMPT ?? "run the echo tool twice";

await agent.prompt({
  role: "user",
  content: toolPrompt,
  timestamp: Date.now(),
});

const assistantMessages = agent.state.messages.filter(
  (message) => message.role === "assistant",
);
const finalAssistant = assistantMessages.at(-1);
const finalText = finalAssistant.content
  .filter((block) => block.type === "text")
  .map((block) => block.text)
  .join("");

const completedRequest = requests.at(-1);
const namesByCallId = new Map();
const nativeCalls = [];
for (const message of completedRequest.messages) {
  for (const call of message.tool_calls ?? []) {
    namesByCallId.set(call.id, call.function.name);
    nativeCalls.push({
      name: call.function.name,
      arguments: JSON.parse(call.function.arguments),
    });
  }
}
const nativeResults = completedRequest.messages
  .filter((message) => message.role === "tool")
  .map((message) => ({
    name: namesByCallId.get(message.tool_call_id),
    content: message.content,
  }));

const replayContext = {
  systemPrompt: "",
  messages: agent.state.messages.slice(0, -1),
  tools: [echo],
};
let replayAssistant = null;
let replayError = null;
for await (const event of agentStream(model, replayContext)) {
  if (event.type === "done") {
    replayAssistant = event.message;
  } else if (event.type === "error") {
    replayError = event.error;
  }
}
if (!replayAssistant) {
  throw new Error(
    `completed replay did not finish: ${JSON.stringify({
      replayError,
      executions,
      agentMessages: agent.state.messages,
      completed: requests.at(-2),
      replay: requests.at(-1),
    })}`,
  );
}
const replayText = replayAssistant.content
  .filter((block) => block.type === "text")
  .map((block) => block.text)
  .join("");

process.stdout.write(
  JSON.stringify({
    provider: { api: model.api, baseUrl: model.baseUrl },
    packageVersions,
    customSessionHeaders,
    requestCount: requests.length,
    requests,
    nativeCalls,
    nativeResults,
    executions,
    finalText,
    replayText,
    executionCountAfterReplay: executions.length,
  }) + "\n",
);
