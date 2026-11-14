"""一个 authoring round 的磁盘布局，以及各部分允许谁读或写。

::

    <round>/
      round.json                public  round 标识、预算、摘要（不含 holdout 数据）
      brief.md                  public  已 lint 的 brief
      bin/xh                    public  broker 工具（只读）
      handoff-in/  references/  public  上一轮的 bundle、已验证的 reference experts
      workspace/                agent   agent 唯一可以写文件的地方
      agent-home/               agent   agent CLI 自己的 HOME / 配置 / 会话状态
      tmp/                      agent   在 sandbox 内绑定到 /tmp
      broker/requests/          agent   存放 xh 请求的收件箱（由 harness 校验）
      broker/responses/ results/ status.json   harness 写入，agent 可读
      rollouts/<attempt>/       harness 写入的 dev rollouts，agent 可读
      submissions/<id>/         harness 写入的 expert 快照，agent 可读
      ledger/attempts.jsonl     harness 拥有的 attempt 账本（只追加），agent 可读
      ledger/events.jsonl       harness 拥有的事件，agent 可读
      ledger/agent-calls.jsonl  datafarm.agent-call/1 记录，agent 可读
      harness/                  PRIVATE：状态、transcripts、防篡改清单、holdout seed、
                                holdout 运行与结果。每个 sandbox 中都被遮蔽。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path


@dataclasses.dataclass(frozen=True)
class RoundPaths:
    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root).resolve())

    # public（agent 只读）
    @property
    def round_json(self) -> Path:
        return self.root / "round.json"

    @property
    def brief(self) -> Path:
        return self.root / "brief.md"

    @property
    def bin_dir(self) -> Path:
        return self.root / "bin"

    @property
    def tool(self) -> Path:
        return self.bin_dir / "xh"

    @property
    def handoff_in(self) -> Path:
        return self.root / "handoff-in"

    @property
    def references(self) -> Path:
        return self.root / "references"

    @property
    def broker(self) -> Path:
        return self.root / "broker"

    @property
    def responses(self) -> Path:
        return self.broker / "responses"

    @property
    def results(self) -> Path:
        return self.broker / "results"

    @property
    def status(self) -> Path:
        return self.broker / "status.json"

    @property
    def rollouts(self) -> Path:
        return self.root / "rollouts"

    @property
    def submissions(self) -> Path:
        return self.root / "submissions"

    @property
    def ledger(self) -> Path:
        return self.root / "ledger"

    @property
    def attempts(self) -> Path:
        return self.ledger / "attempts.jsonl"

    @property
    def events(self) -> Path:
        return self.ledger / "events.jsonl"

    @property
    def agent_calls(self) -> Path:
        return self.ledger / "agent-calls.jsonl"

    # agent 可写
    @property
    def workspace(self) -> Path:
        return self.root / "workspace"

    @property
    def agent_home(self) -> Path:
        return self.root / "agent-home"

    @property
    def tmp(self) -> Path:
        return self.root / "tmp"

    @property
    def requests(self) -> Path:
        return self.broker / "requests"

    # private（每个 sandbox 中都被遮蔽）
    @property
    def harness(self) -> Path:
        return self.root / "harness"

    @property
    def state(self) -> Path:
        return self.harness / "state.json"

    @property
    def lock(self) -> Path:
        return self.harness / "run.lock"

    @property
    def sessions(self) -> Path:
        return self.harness / "sessions"

    @property
    def manifests(self) -> Path:
        return self.harness / "manifests"

    @property
    def holdout_seeds(self) -> Path:
        return self.harness / "holdout-seeds.json"

    @property
    def holdout_dir(self) -> Path:
        return self.harness / "holdout"

    @property
    def holdout_results(self) -> Path:
        return self.holdout_dir / "results.json"

    @property
    def processed(self) -> Path:
        return self.harness / "processed-requests"

    @property
    def private_events(self) -> Path:
        return self.harness / "events.jsonl"

    @property
    def round_result(self) -> Path:
        return self.harness / "round-result.json"

    def public_dirs(self) -> list[Path]:
        return [self.bin_dir, self.handoff_in, self.references, self.responses, self.results,
                self.rollouts, self.submissions, self.ledger]

    def agent_writable(self) -> list[Path]:
        return [self.workspace, self.agent_home, self.tmp, self.requests]

    def create(self) -> None:
        for path in (self.public_dirs() + self.agent_writable()
                     + [self.harness, self.sessions, self.manifests, self.holdout_dir, self.processed]):
            path.mkdir(parents=True, exist_ok=True)
        # 即使是无认证的本地 provider，Codex 也要求 CODEX_HOME 已存在。
        (self.agent_home / ".codex").mkdir(exist_ok=True)
        self.harness.chmod(0o700)
