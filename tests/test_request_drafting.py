"""1a: one sentence → request.yaml. Defaults fill only what the sentence left out; every LLM attempt is ledgered."""
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import openai
import pytest
import yaml

from datafarm.adapters.agent_harness.call_log import read_calls
from datafarm.adapters.llm.client import OpenAIStructuredLLM
from datafarm.adapters.llm.fake import FakeStructuredLLM
from datafarm.entry import request_cli
from datafarm.request.defaults import fill_defaults
from datafarm.request.drafting import DraftError, draft_json_schema, draft_request
from datafarm.request.ports import StructuredLLMError
from datafarm.request.schema import validate_request

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = yaml.safe_load((ROOT / "examples/request/plate-drawer-airbot.yaml").read_text(encoding="utf-8"))
INSTRUCTIONS = (ROOT / "prompts/request/draft.md").read_text(encoding="utf-8")
SENTENCE = "Put the plate with a raised rim into the open drawer in the kitchen, using SimReady or PartNet assets."
SECRET = "test-key-do-not-log-0123456789"


def mvp_draft():
    """What the extraction prompt asks for: task and scene only."""
    return {"task": EXAMPLE["task"], "scene": json.loads(json.dumps(EXAMPLE["scene"]))}


def test_mvp_sentence_draft_fills_to_the_example_request():
    llm = FakeStructuredLLM([mvp_draft()])
    result = draft_request(SENTENCE, llm, instructions=INSTRUCTIONS)
    assert result.request.request_id == "put-the-plate-into-the-open-drawer"
    assert result.request.model_copy(update={"request_id": EXAMPLE["request_id"]}) == validate_request(EXAMPLE)
    assert result.provenance == {
        "task": "sentence", "scene": "sentence", "schema": "default", "robot": "default", "episodes": "default",
        "seeds.dev": "default", "seeds.holdout": "default", "render.engine": "default",
        "render.materials": "default", "render.lights": "default", "dataset.format": "default",
        "request_id": "default", "cameras": "default", "render.views": "default"}
    sent = llm.calls[0]
    assert sent["messages"] == [{"role": "system", "content": INSTRUCTIONS}, {"role": "user", "content": SENTENCE}]
    assert sent["schema"] == draft_json_schema()


def test_draft_schema_requires_only_what_has_no_default():
    schema = draft_json_schema()
    assert schema["required"] == ["task", "scene"]
    defs = schema["$defs"]
    assert "required" not in defs["SeedPartitions"] or defs["SeedPartitions"]["required"] == []
    assert defs["RenderRequest"]["required"] == []
    assert defs["SceneObject"]["required"] == ["object_id", "category", "role"]


def test_sentence_values_win_over_defaults():
    draft = mvp_draft() | {"episodes": 50, "cameras": ["head"], "render": {"lights": 3},
                           "seeds": {"holdout": [500, 509]}}
    result = draft_request(SENTENCE, FakeStructuredLLM([draft]), instructions=INSTRUCTIONS)
    request = result.request
    assert (request.episodes, request.cameras, request.render.lights) == (50, ["head"], 3)
    assert request.render.views == ["head", "third_person"]  # derived from the sentence's cameras
    assert (request.seeds.dev, request.seeds.holdout) == ([0, 99], [500, 509])
    assert request.render.materials == 2
    assert {k: result.provenance[k] for k in ("episodes", "cameras", "render.lights", "render.materials",
                                              "seeds.holdout", "seeds.dev", "render.views")} == {
        "episodes": "sentence", "cameras": "sentence", "render.lights": "sentence", "render.materials": "default",
        "seeds.holdout": "sentence", "seeds.dev": "default", "render.views": "default"}


def test_fill_defaults_does_not_mutate_the_draft():
    draft = mvp_draft()
    before = json.dumps(draft, sort_keys=True)
    fill_defaults(draft)
    assert json.dumps(draft, sort_keys=True) == before


def test_invalid_llm_output_raises_with_reason():
    draft = mvp_draft()
    draft["scene"]["objects"][2]["role"] = "obstacle"
    with pytest.raises(DraftError) as info:
        draft_request(SENTENCE, FakeStructuredLLM([draft]), instructions=INSTRUCTIONS)
    assert "request.scene.objects[2].role" in str(info.value)


def test_sentence_seed_overlapping_default_partition_is_rejected_not_repaired():
    draft = mvp_draft() | {"seeds": {"holdout": [50, 60]}}
    with pytest.raises(DraftError, match="overlaps"):
        draft_request(SENTENCE, FakeStructuredLLM([draft]), instructions=INSTRUCTIONS)


def test_draft_makes_exactly_one_call():
    llm = FakeStructuredLLM([{"task": "x"}, mvp_draft()])
    with pytest.raises(DraftError):
        draft_request(SENTENCE, llm, instructions=INSTRUCTIONS)
    assert len(llm.calls) == 1


# --- OpenAI adapter with a stubbed SDK client (no network) ---

class StubCompletions:
    def __init__(self, outcome):
        self.outcome = outcome
        self.bodies = []

    def create(self, **body):
        self.bodies.append(body)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def completion(content, finish_reason="stop"):
    return SimpleNamespace(
        id="chatcmpl-stub", model="gpt-stub-2026",
        choices=[SimpleNamespace(finish_reason=finish_reason, message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=812, completion_tokens=143))


def adapter(monkeypatch, tmp_path, outcome):
    monkeypatch.setenv("DRAFT_TEST_KEY", SECRET)
    llm = OpenAIStructuredLLM({"model": "gpt-stub", "api_key_env": "DRAFT_TEST_KEY"}, call_log=tmp_path / "calls.jsonl")
    stub = StubCompletions(outcome)
    llm._client = SimpleNamespace(chat=SimpleNamespace(completions=stub))
    return llm, stub


def assert_ledger_has_no_secrets(path):
    text = path.read_text(encoding="utf-8")
    assert SECRET not in text and "raised rim" not in text and "Request drafting" not in text


def test_adapter_success_records_call_and_returns_data(monkeypatch, tmp_path):
    llm, stub = adapter(monkeypatch, tmp_path, completion(json.dumps(mvp_draft())))
    result = draft_request(SENTENCE, llm, instructions=INSTRUCTIONS)
    assert result.request.scene.objects[2].constraints.shape == "raised_rim"
    assert (result.llm.response_id, result.llm.input_tokens, result.llm.output_tokens) == ("chatcmpl-stub", 812, 143)
    body = stub.bodies[0]
    assert body["response_format"]["json_schema"]["strict"] is False
    assert body["response_format"]["json_schema"]["schema"] == draft_json_schema()
    [call] = read_calls(tmp_path / "calls.jsonl")
    assert (call.status, call.component, call.backend, call.model, call.response_id) == (
        "ok", "request-draft", "openai-chat", "gpt-stub-2026", "chatcmpl-stub")
    assert (call.input_tokens, call.output_tokens) == (812, 143)
    assert call.prompt_sha256 and call.request_sha256 and call.response_sha256
    assert_ledger_has_no_secrets(tmp_path / "calls.jsonl")


def test_adapter_accepts_json_wrapped_in_a_markdown_fence(monkeypatch, tmp_path):
    """JSON mode 只保证“是 JSON”；兼容服务仍会把对象包进 ```json 代码块，那不是内容错误。"""
    fenced = "```json\n" + json.dumps(mvp_draft(), ensure_ascii=False) + "\n```"
    llm, _ = adapter(monkeypatch, tmp_path, completion(fenced))
    result = draft_request(SENTENCE, llm, instructions=INSTRUCTIONS)
    assert result.request.scene.objects[2].constraints.shape == "raised_rim"
    [call] = read_calls(tmp_path / "calls.jsonl")
    assert call.status == "ok"
    assert_ledger_has_no_secrets(tmp_path / "calls.jsonl")


@pytest.mark.parametrize("outcome,status,error_type", [
    (openai.APITimeoutError(request=httpx.Request("POST", "https://api.example/v1/chat/completions")),
     "timeout", "APITimeoutError"),
    (openai.RateLimitError("slow down", body=None, response=httpx.Response(
        429, request=httpx.Request("POST", "https://api.example/v1/chat/completions"))), "error", "http_429"),
    (completion("not json {"), "invalid_response", "not_json"),
    (completion('{"task": "put the pla', finish_reason="length"), "invalid_response", "not_json"),
])
def test_adapter_failure_is_recorded_before_raising(monkeypatch, tmp_path, outcome, status, error_type):
    llm, _ = adapter(monkeypatch, tmp_path, outcome)
    with pytest.raises(StructuredLLMError) as info:
        draft_request(SENTENCE, llm, instructions=INSTRUCTIONS)
    assert info.value.status == status and SECRET not in str(info.value)
    [call] = read_calls(tmp_path / "calls.jsonl")
    assert (call.status, call.error_type) == (status, error_type)
    assert_ledger_has_no_secrets(tmp_path / "calls.jsonl")


def test_adapter_refuses_without_key_and_never_echoes_it(monkeypatch, tmp_path):
    monkeypatch.delenv("DRAFT_TEST_KEY", raising=False)
    with pytest.raises(StructuredLLMError, match="DRAFT_TEST_KEY is not set"):
        OpenAIStructuredLLM({"model": "m", "api_key_env": "DRAFT_TEST_KEY"}, call_log=tmp_path / "c.jsonl")
    with pytest.raises(ValueError, match="unknown LLM config keys"):
        OpenAIStructuredLLM({"model": "m", "api_key": SECRET}, call_log=tmp_path / "c.jsonl")


def test_cli_writes_request_and_provenance_sidecar(monkeypatch, tmp_path):
    def fake_llm(config, *, call_log):
        assert config["api_key_env"] == "OPENAI_API_KEY"
        return FakeStructuredLLM([mvp_draft()])

    monkeypatch.setattr(request_cli, "OpenAIStructuredLLM", fake_llm)
    out = tmp_path / "request.yaml"
    assert request_cli.main([SENTENCE, "--config", str(ROOT / "configs/llm.template.json"), "--out", str(out)]) == 0
    written = validate_request(yaml.safe_load(out.read_text(encoding="utf-8")))
    assert written.model_copy(update={"request_id": EXAMPLE["request_id"]}) == validate_request(EXAMPLE)
    side = json.loads((tmp_path / "request.draft.json").read_text(encoding="utf-8"))
    assert side["sentence"] == SENTENCE and side["provenance"]["cameras"] == "default"
    from datafarm.kernel.io import sha256_file
    assert side["request_sha256"] == sha256_file(out)
    assert side["prompt_file_sha256"] == sha256_file(ROOT / "prompts/request/draft.md")
    assert request_cli.main([SENTENCE, "--config", str(ROOT / "configs/llm.template.json"), "--out", str(out)]) == 2
