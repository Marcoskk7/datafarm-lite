"""厂商无关的无头 agent session 机制：会话记录、错误分类、成本估算与 wall-capped 运行器。

一轮（round）是同一段对话的多个 session 序列：第一个 session 用 brief 启动 agent，
后续 session 用简短的状态 prompt 恢复它（``--resume <session id>``）。
每个 session 都以 wall-clock 时间设上限（先 SIGTERM，再对整个进程组
SIGKILL），其 stdout 事件流在到达时即追加到 transcript 文件，其
最终 ``result`` 事件提供 ``total_cost_usd``、``usage``、``num_turns`` 和
session id —— 因此成本来自 provider 自己的记账，而不是
手工从 transcript 重建。

具体后端按厂商分文件：``claude_code``（Claude Code）、``codex.exec_backend``（Codex）、
``fake``（测试与 dry run 使用的脚本化替身）；``backends.make_agent_backend`` 按配置选择后端。
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import queue
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

API_ERROR_PATTERNS = ("API Error", "api_error", "overloaded", "Overloaded", " 529", "rate_limit",
                      "rate limit", "ECONNRESET", "ETIMEDOUT", "ECONNREFUSED", "EAI_AGAIN",
                      "ENOTFOUND", "fetch failed", "Connection error", "socket hang up",
                      "502 Bad Gateway", "503 Service", "504 Gateway", "Internal server error",
                      "Request timed out", "stream disconnected", "network error")
AUTH_ERROR_PATTERNS = ("Invalid API key", "authentication_error", "Please run /login", "401 ",
                       "OAuth token has expired", "Not logged in", "invalid x-api-key",
                       "Credit balance is too low", "permission_error")


# --------------------------------------------------------------------------- 定价


def estimate_cost(model: str | None, usage: Mapping[str, int | None], prices: Mapping[str, Any]
                  ) -> float | None:
    """按价格表估算 USD 成本（每百万 token 价格），若无法定价则返回 ``None``。"""

    if not model:
        return None
    table = prices.get(model)
    if table is None:
        for name, value in prices.items():
            if model.startswith(name):
                table = value
                break
    if table is None:
        return None

    def n(key: str) -> int:
        return int(usage.get(key) or 0)

    total = (n("input_tokens") * table["input"] + n("output_tokens") * table["output"]
             + n("cache_read_tokens") * table["cache_read"]
             + n("cache_write_5m_tokens") * table["cache_write_5m"]
             + n("cache_write_1h_tokens") * table.get("cache_write_1h", table["cache_write_5m"]))
    return round(total / 1_000_000.0, 6)


# --------------------------------------------------------------------------- 解析


@dataclasses.dataclass
class ParsedSession:
    session_id: str | None = None
    model: str | None = None
    saw_result: bool = False
    is_error: bool = False
    subtype: str | None = None
    result_text: str | None = None
    cost_usd: float | None = None
    cost_source: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    turns: int | None = None
    tool_uses: int = 0
    events: int = 0
    bad_lines: int = 0
    error_texts: list[str] = dataclasses.field(default_factory=list)
    verified_transport_interruption: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        data["result_text"] = None if self.result_text is None else self.result_text[:2000]
        data["error_texts"] = [t[:500] for t in self.error_texts[-10:]]
        return data


def classify_errors(parsed: ParsedSession, stderr_tail: str, exit_code: int | None
                    ) -> tuple[bool, bool, str | None]:
    """根据零工具调用错误或权威的计量回执分类服务中断。

    经计量的传输中断可能发生在有用的工具调用之后；可在不重放工具调用的前提下
    恢复同一段对话，保留所有成本与试验记录。
    """

    texts = "\n".join(parsed.error_texts + [stderr_tail[-4000:]])
    failed = parsed.is_error or not parsed.saw_result or (exit_code not in (0, None))
    auth = failed and any(p in texts for p in AUTH_ERROR_PATTERNS)
    api = failed and not auth and (parsed.verified_transport_interruption or
          (parsed.tool_uses == 0 and any(p in texts for p in API_ERROR_PATTERNS)))
    if auth:
        return False, True, "auth_error"
    if api:
        return True, False, "api_outage"
    if failed:
        if parsed.subtype and "budget" in parsed.subtype:
            return False, False, "max_budget"
        return False, False, parsed.subtype or ("no_result" if not parsed.saw_result else "error")
    return False, False, None


# --------------------------------------------------------------------------- 后端


@dataclasses.dataclass
class SessionSpec:
    index: int
    prompt: str
    cwd: str
    resume_id: str | None = None
    new_session_id: str | None = None
    max_budget_usd: float | None = None


class AgentBackend:
    name = "agent"
    call_backend = "agent"
    uses_stdin_prompt = False

    def __init__(self, cfg: Mapping[str, Any], prices: Mapping[str, Any] | None = None) -> None:
        self.cfg = dict(cfg)
        self.prices = dict(prices or {})
        self.model = str(self.cfg.get("model"))

    def command(self, spec: SessionSpec) -> list[str]:
        raise NotImplementedError

    def parser(self) -> Any:
        raise NotImplementedError

    def env(self) -> dict[str, str]:
        return {}

    @contextlib.contextmanager
    def session(self, spec: SessionSpec, directory: Path):
        yield

    def settle(self, parsed: ParsedSession) -> ParsedSession:
        return parsed


# --------------------------------------------------------------------------- 运行器


@dataclasses.dataclass
class RunInfo:
    exit_code: int | None
    wall_s: float
    stop_reason: str  # completed | wall_cap | <stop 回调返回的原因>
    killed: bool
    stderr_tail: str


class SessionRunner:
    """运行一个 agent 进程：把 stdout 流入 transcript 与 parser，运行期间按 tick 驱动
    broker，执行 wall cap，按需终止进程组。"""

    def __init__(self, *, tick: Callable[[], None] = lambda: None,
                 should_stop: Callable[[], str | None] = lambda: None,
                 tick_interval_s: float = 1.0, term_grace_s: float = 15.0) -> None:
        self.tick = tick
        self.should_stop = should_stop
        self.tick_interval_s = tick_interval_s
        self.term_grace_s = term_grace_s

    def run(self, argv: Sequence[str], *, env: Mapping[str, str], cwd: str, stdin_text: str | None,
            transcript: Path, stderr_path: Path, wall_s: float, parser: Any) -> RunInfo:
        start = time.monotonic()
        transcript.parent.mkdir(parents=True, exist_ok=True)
        with open(transcript, "ab") as tx, open(stderr_path, "ab") as err:
            proc = subprocess.Popen(list(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=err, cwd=cwd, env=dict(env), start_new_session=True,
                                    preexec_fn=_die_with_parent)
            lines: "queue.Queue[bytes | None]" = queue.Queue()

            def pump() -> None:
                assert proc.stdout is not None
                for raw in iter(proc.stdout.readline, b""):
                    tx.write(raw)
                    tx.flush()
                    lines.put(raw)
                lines.put(None)

            def feed_stdin() -> None:
                assert proc.stdin is not None
                try:
                    if stdin_text:
                        proc.stdin.write(stdin_text.encode("utf-8"))
                    proc.stdin.close()
                except (BrokenPipeError, OSError):
                    pass

            reader = threading.Thread(target=pump, daemon=True)
            reader.start()
            threading.Thread(target=feed_stdin, daemon=True).start()
            stop_reason: str | None = None
            term_at: float | None = None
            killed = False
            eof = False
            try:
                while True:
                    try:
                        item = lines.get(timeout=self.tick_interval_s)
                        while True:
                            if item is None:
                                eof = True
                            else:
                                parser.feed(item.decode("utf-8", "replace"))
                            item = lines.get_nowait()
                    except queue.Empty:
                        pass
                    if eof:
                        try:
                            proc.wait(timeout=self.term_grace_s)
                        except subprocess.TimeoutExpired:
                            # stdout 已关闭但进程仍在滞留
                            stop_reason = stop_reason or "stdout_closed"
                            killed = True
                            _signal_group(proc, signal.SIGKILL)
                            proc.wait()
                        break
                    self.tick()
                    now = time.monotonic()
                    if stop_reason is None:
                        reason = self.should_stop()
                        if reason is None and now - start > wall_s:
                            reason = "wall_cap"
                        if reason is not None:
                            stop_reason = reason
                            term_at = now
                            _signal_group(proc, signal.SIGTERM)
                    elif term_at is not None and not killed and now - term_at > self.term_grace_s:
                        killed = True
                        _signal_group(proc, signal.SIGKILL)
            finally:
                _signal_group(proc, signal.SIGKILL)  # 清理进程组中残留的进程
                reader.join(timeout=5)
                if proc.poll() is None:
                    proc.wait()
        try:
            tail = stderr_path.read_bytes()[-8000:].decode("utf-8", "replace")
        except OSError:
            tail = ""
        return RunInfo(proc.returncode, time.monotonic() - start, stop_reason or "completed",
                       killed, tail)


def _die_with_parent() -> None:
    """在 exec 前于子进程中运行：若 harness 死亡，agent 会收到 SIGKILL
    （Linux ``PR_SET_PDEATHSIG``；其他平台为无操作）。bwrap 会自行添加 --die-with-parent。"""

    try:
        import ctypes

        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGKILL)
    except (OSError, AttributeError):
        pass


def _signal_group(proc: subprocess.Popen[bytes], sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass

