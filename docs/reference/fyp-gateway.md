# Server-direct FYP Responses gateway

The FYP transport is an explicit deployment adapter, not a new CaP business mode.
`entry/codex_gateway.py` binds `adapters/agent_harness/codex/fyp_gateway.py` to the
existing Codex role call. CaP and Judge business roles do not read credentials.
No gateway endpoint, account, host path or token is bundled with the source.

## Explicit runtime configuration

Set deployment environment variables on the machine executing the gateway:

```sh
export FYP_BASE_URL="https://<authorized-gateway>/v1"
export FYP_API_KEY="<private-deployment-token>"
# Userspace Tailscale only; omit in a normal TUN deployment.
export FYP_PROXY_URL="http://127.0.0.1:<server-proxy-port>"
```

Never put the token in argv, provider JSON, logs, task artifacts or version control.
The following provider JSON contains names, not secret values:

```json
{
  "work_root": "runs/my-task/model-calls",
  "executable": "codex",
  "model": "<verified-GPT-Responses-model>",
  "timeout_s": 300,
  "gateway": {
    "base_url_env": "FYP_BASE_URL",
    "api_key_env": "FYP_API_KEY",
    "proxy_url_env": "FYP_PROXY_URL",
    "max_requests": 20,
    "max_output_tokens_total": 120000,
    "deadline_s": 2400,
    "timeout_s": 180
  }
}
```

Remove `proxy_url_env` when no proxy is needed. Ambient HTTP(S) proxies are ignored.
CaP uses `datafarm.entry.cap_provider:build_provider` as before. Judge uses the same
JSON with `python -m datafarm.entry.judge_cli build --provider-config ...`.
`fyp_env_file` and embedded credentials are not accepted. The runtime deliberately
does not parse or copy desktop FYP scripts or automatically log into Tailscale.

## Server networking

A TUN-capable server can run a normal Tailscale deployment. In a restricted container,
Tailscale supports `tailscaled --tun=userspace-networking` with an explicit
`--outbound-http-proxy-listen=127.0.0.1:<port>`. Authorize the server itself using the
invited account. This is a server-local HTTPS CONNECT path through its own tailnet,
not a workstation relay. Keep the daemon's state/socket/logs outside source, with
private permissions; supervise it using the deployment's process manager. Do not
silently change system routes, DNS, SSH or existing workloads.

The gateway verifies upstream TLS certificates and forbids redirects. A successful
`/models` query proves connectivity only: it does not prove model inference,
Responses compatibility, tool use or task completion. Chat Completions-only models
cannot silently substitute for a Codex Responses model.

## Safety, budgets and identity

- Each structured role gets a fresh Codex session and a separate loopback listener.
  The CLI receives only a temporary loopback key. Personal upstream keys are removed
  from its environment. Shell, MCP, web, skills and additional agents remain disabled.
- Requests are inline and stateless (`store=false`). Tool definitions, call IDs and
  outputs are preserved in tool-capable transport scopes; unknown semantic fields
  are refused rather than silently dropped. Structured roles preserve definitions but force `tool_choice=none` and reject tool-call inputs/outputs.
  Only declared Codex cache/retention and client-metadata extensions are removed and recorded.
- Listeners bind only to loopback and require the temporary key. Request/stream sizes,
  output allowances, timeout and run deadline are bounded. Parallel scopes are refused.
- `gateway-budget/budget.json` is file-locked and records a reservation before each
  upstream request. Failed requests consume allowances. Duplicate requests are refused;
  unknown/incomplete requests remain pending and block further spend. There is no
  automatic timeout, rate-limit or server-error retry.
- The cumulative output limit counts requested allowances, not guessed actual usage.
  Each task/provider work root has its own budget; an operator coordinating several
  work roots must enforce an additional shared overall ceiling. Dollar cost remains
  unknown unless separately supplied by an authoritative cost policy, never zero.
- Endpoint/proxy hashes, model, source/config and protocol are bound to transport and
  call identity. Keys and endpoint strings are not written into those fingerprints.
  Changing transport or source requires new call and Judge build versions. Old frozen
  records are neither upgraded nor rewritten; new source dependencies enter new freezes.

Legacy programmatically injected gateways remain accepted, but their endpoint/class
now participates in identity. A working directory or prompt is not same-user read
isolation; tool-capable deployments must use a verified OS sandbox and protect private
files separately. Never disable the sandbox as root to report a false pass.

## Acceptance evidence

Report implementation, offline tests, real model runs and robot physics separately.
A real CLI check needs a tool/file action plus independent host checks. Judge needs
separate real author/reviewer receipts, deterministic positive/negative checks covering
all criteria, a new anchored freeze and explicit failure/error/abort results. CaP needs
actual SearchRunner/broker execution, measured evidence, independent skill validation
and frozen-program holdout. Software/synthetic fixtures do not certify Isaac physics.
New validation scripts, credentials and outputs stay in ignored run directories.

Transport completion requires a terminated, bounded SSE stream, an explicit
matching upstream model and a valid completion event. Duplicate completions,
excess reported output use, malformed trailing data and CLI exit without complete
upstream records fail. Missing token usage remains unknown. Reads use the remaining
wall allowance; a receipt-storage failure retains the pre-send pending reservation
and releases the request lock, rather than deadlocking cleanup or permitting retries.
