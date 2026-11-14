"""Codex ``codex exec --json`` 后端：JSONL 事件解析、headless 命令，以及经受信任网关计量的变体。"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any, Mapping

from datafarm.adapters.agent_harness.session import AgentBackend, ParsedSession, SessionSpec, estimate_cost
from datafarm.expert.util import ConfigError


class CodexJsonParser:
    """解析 ``codex exec --json`` 的 JSONL 事件（thread/turn/item）。"""

    TOOL_ITEMS = ("command_execution", "file_change", "mcp_tool_call", "web_search")

    def __init__(self, prices: Mapping[str, Any] | None = None, model: str | None = None) -> None:
        self.prices = dict(prices or {})
        self.p = ParsedSession(model=model)
        self._tokens = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
        self._saw_usage = False

    def feed(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            event = json.loads(line)
        except ValueError:
            self.p.bad_lines += 1
            return
        if not isinstance(event, dict):
            self.p.bad_lines += 1
            return
        self.p.events += 1
        kind = event.get("type")
        if kind == "thread.started" and event.get("thread_id"):
            self.p.session_id = event["thread_id"]
        elif kind == "turn.completed":
            self.p.saw_result = True
            self.p.turns = (self.p.turns or 0) + 1
            usage = event.get("usage") or {}
            for key in self._tokens:
                self._tokens[key] += int(usage.get(key) or 0)
            self._saw_usage = True
        elif kind == "turn.failed":
            self.p.saw_result = True
            self.p.is_error = True
            self.p.error_texts.append(str((event.get("error") or {}).get("message", event))[:500])
        elif kind == "error":
            self.p.is_error = True
            self.p.error_texts.append(str(event.get("message", event))[:500])
        elif kind == "item.completed":
            item = event.get("item") or {}
            if item.get("type") in self.TOOL_ITEMS:
                self.p.tool_uses += 1
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                self.p.result_text = item["text"]

    def finish(self) -> ParsedSession:
        if self._saw_usage:
            cached = self._tokens["cached_input_tokens"]
            self.p.input_tokens = max(0, self._tokens["input_tokens"] - cached)
            self.p.cache_read_tokens = cached
            self.p.output_tokens = self._tokens["output_tokens"]
            self.p.cache_write_tokens = 0
            estimate = estimate_cost(self.p.model, {
                "input_tokens": self.p.input_tokens, "output_tokens": self.p.output_tokens,
                "cache_read_tokens": cached}, self.prices)
            if estimate is not None:
                self.p.cost_usd = estimate
                self.p.cost_source = "price_table"
        return self.p


class CodexExecBackend(AgentBackend):
    """``codex exec --json``；prompt 通过 stdin（``-``）传入，因此既不出现在
    进程列表中，也不会被追加到空的 ``<stdin>`` 块。Codex 自带的 sandbox 被绕过，
    因为 harness sandbox 已经限制了它。"""

    name = "codex"
    call_backend = "codex-exec"
    uses_stdin_prompt = True

    def command(self, spec: SessionSpec) -> list[str]:
        cfg = self.cfg
        argv = [cfg.get("cli", "codex"), "exec", "--json", "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox", "-m", self.model]
        argv += list(cfg.get("extra_args", []))
        if spec.resume_id:
            return argv + ["resume", spec.resume_id, "-"]
        return argv + ["-C", spec.cwd, "-"]

    def parser(self) -> Any:
        return CodexJsonParser(self.prices, self.model)


class MeteredCodexBackend(CodexExecBackend):
    """Gateway 与凭据留在受信任的 harness 中，位于作者挂载之外。

    作者只看到 loopback 端点。成本来自上游用量，
    绝不来自作者的 stdout。未知用量会阻塞该轮，而不是释放债务。
    """
    name = "codex-metered"
    call_backend = "codex-metered"

    @contextlib.contextmanager
    def session(self, spec: SessionSpec, directory: Path):
        from datafarm.adapters.agent_harness.credentials import KeysFileCredentials
        from datafarm.adapters.agent_harness.codex.metered_gateway import Meter, MeteredGateway, load_pricing
        if spec.max_budget_usd is None or spec.max_budget_usd <= 0:
            raise ConfigError('metered backend requires a finite positive session budget')
        try:  # agent.pricing：JSON 对象或文件，指定计量 model 及其价格；无默认值
            pricing = load_pricing(self.cfg.get('pricing'))
        except (ValueError, OSError) as exc:
            raise ConfigError(f'codex-metered agent.pricing: {exc}') from None
        if pricing.model != self.model:
            raise ConfigError(f'codex-metered: agent.model {self.model!r} is not the priced model {pricing.model!r}')
        self.meter = Meter(directory / f'session-{spec.index:03d}.provider.jsonl', spec.max_budget_usd, pricing=pricing,
                           buffered=not bool(self.cfg.get("upstream_streaming", False)),
                           receipt_lookup=self.cfg.get("provider_receipt_lookup")=="openai_compatible",
                           read_timeout_s=self.cfg.get("upstream_read_timeout_s",900),
                           stream_max_s=self.cfg.get("upstream_stream_max_s"))
        creds = KeysFileCredentials(self.cfg['credentials_file'])()
        gateway = MeteredGateway(self.meter, creds.endpoint.rstrip('/')+'/v1/responses', creds.api_key)
        self.gateway = gateway
        try:
            yield
        finally:
            gateway.close()  # 即使 CLI 被杀死，也要完成请求计费。
            self.receipt = {'schema':'datafarm.session-cost/1', 'complete':not self.meter.unknown,
                            'includes_descendants':True, 'cost_usd':float(self.meter.spent),
                            'cost_basis':'api-equivalent', 'provider_calls':self.meter.calls-self.meter.not_sent_calls,
                            'connection_attempts_not_sent':self.meter.not_sent_calls,
                            'denied_local':self.meter.denied, 'evidence':str(self.meter.path)}
            path = directory / f'session-{spec.index:03d}.cost.json'
            path.write_text(json.dumps(self.receipt, indent=2)+'\n')

    def command(self, spec: SessionSpec) -> list[str]:
        # 不允许任何可能重定向计量 provider 的任意配置覆盖。
        if self.cfg.get('extra_args'):
            raise ConfigError('metered backend does not accept arbitrary CLI overrides')
        argv = super().command(spec)
        options = ['model_provider="xh_metered"',
                   'model_providers.xh_metered.name="Trusted harness meter"',
                   'model_providers.xh_metered.base_url='+json.dumps(self.gateway.url),
                   'model_providers.xh_metered.wire_api="responses"',
                   'model_providers.xh_metered.requires_openai_auth=false',
                   'model_providers.xh_metered.request_max_retries=0',
                   'model_providers.xh_metered.stream_max_retries=0',
                   'model_providers.xh_metered.stream_idle_timeout_ms=960000',
                   'model_auto_compact_token_limit=64000',
                   'project_doc_max_bytes=0',
                   'features.responses_websockets=false',
                   'features.responses_websockets_v2=false', 'service_tier="default"']
        effort = self.cfg.get('reasoning_effort')
        if effort is not None:
            if effort not in ('low', 'medium', 'high'):
                raise ConfigError('metered reasoning_effort must be low, medium or high')
            options.append('model_reasoning_effort=' + json.dumps(effort))
        insertion = [v for item in options for v in ('-c', item)]
        argv[2:2] = insertion
        return argv

    def settle(self, parsed: ParsedSession) -> ParsedSession:
        parsed.cost_usd = self.receipt['cost_usd'] if self.receipt['complete'] else None
        parsed.cost_source = 'api-equivalent' if self.receipt['complete'] else None
        rows = [json.loads(l) for l in self.meter.path.read_text().splitlines()]
        last = rows[-1] if rows else {}
        parsed.verified_transport_interruption = bool(self.receipt['complete'] and
            (last.get('event') in ('not-sent', 'preflight-failed') or
             (last.get('event') == 'settled' and last.get('output_received') is False)))
        usage = [r['usage'] for r in rows if r.get('event')=='settled']
        parsed.cache_read_tokens = sum(u.get('input_tokens_details',{}).get('cached_tokens',0) for u in usage)
        parsed.cache_write_tokens = sum(u.get('input_tokens_details',{}).get('cache_write_tokens',0) for u in usage)
        parsed.input_tokens = sum(u['input_tokens'] for u in usage)-parsed.cache_read_tokens-parsed.cache_write_tokens
        parsed.output_tokens = sum(u['output_tokens'] for u in usage)
        return parsed
