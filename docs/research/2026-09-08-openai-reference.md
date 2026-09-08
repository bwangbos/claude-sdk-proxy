# OpenAI subscription OAuth reference

This implementation was independently written for `claude-sdk-proxy`. It does
not impersonate Pi and does not read or reuse Pi or Codex credentials.

The local reference inspected on 2026-09-08 was
`@earendil-works/pi-coding-agent` version **0.85.1**, specifically its installed
`dist/bundle/chunks/openai-codex.js` and `package.json`. The package identifies
its repository as `https://github.com/earendil-works/pi`, directory
`packages/coding-agent`, and its license as MIT.

The following public OAuth configuration was observed and is fixed in this
implementation:

- Authorization URL: `https://auth.openai.com/oauth/authorize`
- Token URL: `https://auth.openai.com/oauth/token`
- Public client ID: `app_EMoamEEZ73f0CkXaXp7hrann`
- Redirect URI: `http://localhost:1455/auth/callback`
- Scope: `openid profile email offline_access`
- Account claim namespace: `https://api.openai.com/auth`, field
  `chatgpt_account_id`

The implementation uses its own PKCE verifier and state, identifies its OAuth
originator as `claude-sdk-proxy`, and stores newly obtained credentials only in
the proxy-owned configuration directory. The upstream MIT notice retained for
this reference is in `third_party/pi-coding-agent-MIT.txt`.
