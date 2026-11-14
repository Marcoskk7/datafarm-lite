"""脚本化替身后端，用于测试与 dry run。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from datafarm.adapters.agent_harness.claude_code import ClaudeStreamParser
from datafarm.adapters.agent_harness.session import AgentBackend, SessionSpec


class FakeAgentBackend(AgentBackend):
    """用 JSON 计划运行 ``fakes/fake_agent.py``（Claude stream-json 输出）。"""

    name = "fake"
    call_backend = "fake"
    uses_stdin_prompt = True

    def command(self, spec: SessionSpec) -> list[str]:
        cfg = self.cfg
        script = cfg.get("script") or str(Path(__file__).resolve().parents[1] / "fakes" / "fake_agent.py")
        argv = [cfg.get("python", sys.executable), script, "--plan", cfg["plan"],
                "--session-index", str(spec.index), "--model", self.model]
        if spec.resume_id:
            argv += ["--resume", spec.resume_id]
        elif spec.new_session_id:
            argv += ["--session-id", spec.new_session_id]
        return argv

    def parser(self) -> Any:
        return ClaudeStreamParser(self.prices)
