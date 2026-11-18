"""Offline gateway regressions; upstream HTTPS is mocked, loopback is real."""

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest
from datafarm.adapters.agent_harness.codex import structured
from datafarm.adapters.agent_harness.codex.fyp_gateway import FypGateway, RequestBudget
from datafarm.entry.codex_gateway import bind_gateway


def fake_cli(tmp_path):
    script = tmp_path / "cli.py"
    script.write_text(
        "import json,sys,os\na=sys.argv;sys.stdin.read()\nout=a[a.index('--output-last-message')+1]\nopen(out,'w').write(json.dumps({'ok':True}))\nprint(json.dumps({'type':'thread.started','thread_id':'session-'+out}))\nprint(json.dumps({'type':'turn.completed','usage':{'output_tokens':1}}))\n"
    )
    wrapper = tmp_path / "codex"
    wrapper.write_text(
        '#!/bin/sh\nexec "' + sys.executable + '" "' + str(script) + '" "$@"\n'
    )
    wrapper.chmod(0o700)
    return str(wrapper)


def test_identity_roles_channels_and_cache(tmp_path):
    cfg = {"work_root": str(tmp_path / "calls"), "executable": fake_cli(tmp_path)}

    class Gateway:
        client_key = "ephemeral-local-test"
        calls = 0

        def __init__(self, port):
            self.base = "http://127.0.0.1:" + str(port) + "/v1"

    args = {
        "config": cfg,
        "system": "data only",
        "schema": {"type": "object"},
        "validator": lambda v, r: v,
        "with_folder": True,
    }
    a, fa = structured.structured_call(
        {"data": "test"}, role="actor", gateway=Gateway(1001), **args
    )
    b, fb = structured.structured_call(
        {"data": "test"}, role="verifier", gateway=Gateway(1001), **args
    )
    c, fc = structured.structured_call(
        {"data": "test"}, role="actor", gateway=Gateway(1002), **args
    )
    assert len({fa, fb, fc}) == 3 and a == b == c == {"ok": True}
    again, folder = structured.structured_call(
        {"data": "test"}, role="actor", gateway=Gateway(1001), **args
    )
    assert folder == fa
    receipt = json.loads((fa / "receipt.json").read_text())
    receipt["status"] = "failed"
    (fa / "receipt.json").write_text(json.dumps(receipt))
    with pytest.raises(ValueError):
        structured.structured_call(
            {"data": "test"}, role="actor", gateway=Gateway(1001), **args
        )


def test_cli_exit_without_gateway_completion_refused(tmp_path):
    cfg = {
        "work_root": str(tmp_path / "calls"),
        "executable": fake_cli(tmp_path),
        "model": "test-model",
    }
    g = FypGateway(
        base_url="https://test.invalid/v1",
        api_key="fyp_offline-placeholder",
        model="test-model",
        budget_root=tmp_path / "budget",
        timeout_s=1,
    )
    with pytest.raises(RuntimeError, match="transport evidence"):
        structured.structured_call(
            {"data": "test"},
            config=cfg,
            system="data",
            schema={"type": "object"},
            validator=lambda v, r: v,
            role="judge_author",
            gateway=g,
        )
    assert not g.budget._read()["reservations"]
    assert not list((tmp_path / "calls").glob("*/decision.json"))
    assert (
        json.loads(next((tmp_path / "calls").glob("*/receipt.json")).read_text())[
            "status"
        ]
        == "failed_or_interrupted"
    )


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), 0, -1, True])
def test_invalid_timeout(timeout, tmp_path):
    with pytest.raises(ValueError):
        FypGateway(
            base_url="https://test.invalid/v1",
            api_key="fyp_placeholder",
            model="test-model",
            budget_root=tmp_path,
            timeout_s=timeout,
        )


def test_allowance_changes_identity(tmp_path):
    kw = dict(
        base_url="https://test.invalid/v1",
        api_key="fyp_placeholder",
        model="test-model",
    )
    a = FypGateway(**kw, budget_root=tmp_path / "a", max_requests=2)
    b = FypGateway(**kw, budget_root=tmp_path / "b", max_requests=3)
    assert a.fingerprint != b.fingerprint
    with pytest.raises(ValueError):
        FypGateway(**kw, budget_root=tmp_path / "a", max_requests=3)


@pytest.mark.parametrize("tokens", [0, -1, True, 17000])
def test_invalid_reservation(tokens, tmp_path):
    budget = RequestBudget(
        tmp_path,
        fingerprint="test",
        max_requests=2,
        max_output_tokens_total=50,
        deadline_s=60,
    )
    with pytest.raises(ValueError):
        budget.reserve("one", tokens, "sha")


def test_environment_named_key_strip(monkeypatch):
    monkeypatch.setenv("CUSTOM_PRIVATE_KEY", "fyp_sensitive-placeholder")
    monkeypatch.setenv("FYP_ANYTHING", "sensitive-placeholder")
    transport = SimpleNamespace(
        owner=SimpleNamespace(_key="fyp_sensitive-placeholder"),
        client_key="temp",
        base="http://127.0.0.1:1/v1",
    )
    env = structured._gateway_env(transport)
    assert "CUSTOM_PRIVATE_KEY" not in env and "FYP_ANYTHING" not in env
    assert env["OPENAI_API_KEY"] == "temp"


def test_configuration_has_no_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("FYP_TEST_URL", "https://test.invalid/v1")
    monkeypatch.setenv("FYP_TEST_KEY", "fyp_placeholder")
    cfg, g = bind_gateway(
        {
            "work_root": str(tmp_path),
            "model": "test-model",
            "gateway": {"base_url_env": "FYP_TEST_URL", "api_key_env": "FYP_TEST_KEY"},
        }
    )
    assert "gateway" not in cfg and "fyp_placeholder" not in json.dumps(
        g.budget._read()
    )
    with pytest.raises(ValueError):
        bind_gateway(
            {"work_root": str(tmp_path), "gateway": {"api_key": "fyp_placeholder"}}
        )


@pytest.mark.parametrize("case", ["tail", "duplicate", "excess", "save_error"])
def test_stream_and_finalization_negatives(tmp_path, monkeypatch, case):
    import io, urllib.request
    from http.client import IncompleteRead
    from email.message import Message
    from datafarm.adapters.agent_harness.codex import fyp_gateway as module

    event = {
        "type": "response.completed",
        "response": {
            "model": "test-model",
            "status": "completed",
            "usage": {"output_tokens": 31 if case == "excess" else 1},
        },
    }
    data = b"data: " + json.dumps(event).encode() + b"\n\n"
    if case == "tail":
        data += b"data: broken"
    if case == "duplicate":
        data += data

    class Response:
        status = 200

        def __init__(self):
            self.data = io.BytesIO(data)
            self.headers = Message()
            self.headers["Content-Type"] = "text/event-stream"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def read1(self, n):
            return self.data.read(n)

    class Opener:
        def open(self, *a, **kw):
            return Response()

    client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    monkeypatch.setattr(module, "build_opener", lambda *a: Opener())
    g = FypGateway(
        base_url="https://test.invalid/v1",
        api_key="fyp_offline",
        model="test-model",
        budget_root=tmp_path / "budget",
        timeout_s=0.2,
    )
    if case == "save_error":
        monkeypatch.setattr(
            g.budget,
            "finish",
            lambda *a: (_ for _ in ()).throw(OSError("software storage failure")),
        )
    with g.scope(30, case) as t:
        req = urllib.request.Request(
            t.base + "/responses",
            data=json.dumps(
                {"model": "test-model", "input": "test", "stream": True}
            ).encode(),
            headers={"Authorization": "Bearer " + t.client_key},
        )
        try:
            with client.open(req, timeout=3) as response:
                response.read()
        except (OSError, IncompleteRead):
            pass
    assert not t._request_lock.locked()
    assert t.records[0]["status"] == (
        "receipt_error" if case == "save_error" else "failed"
    )
    assert g.budget._read()["reservations"][0]["status"] == (
        "pending" if case == "save_error" else "failed"
    )


def test_actual_cli_gateway_receipt_and_no_respend(tmp_path, monkeypatch):
    import io, urllib.request
    from email.message import Message
    from datafarm.adapters.agent_harness.codex import fyp_gateway as module

    source = tmp_path / "requesting_cli.py"
    source.write_text(
        "import json,sys,os,urllib.request\na=sys.argv;sys.stdin.read()\nurl=os.environ['OPENAI_BASE_URL']+'/responses'\nr=urllib.request.Request(url,data=json.dumps({'model':'test-model','input':'test','stream':True}).encode(),headers={'Authorization':'Bearer '+os.environ['OPENAI_API_KEY']})\nwith urllib.request.build_opener(urllib.request.ProxyHandler({})).open(r) as response: response.read()\nopen(a[a.index('--output-last-message')+1],'w').write(json.dumps({'ok':True}))\nprint(json.dumps({'type':'thread.started','thread_id':'software.session'}))\nprint(json.dumps({'type':'turn.completed','usage':{'output_tokens':1}}))\n"
    )
    wrapper = tmp_path / "codex"
    wrapper.write_text(
        '#!/bin/sh\nexec "' + sys.executable + '" "' + str(source) + '" "$@"\n'
    )
    wrapper.chmod(0o700)

    class Response:
        status = 200

        def __init__(self):
            self.headers = Message()
            self.headers["Content-Type"] = "text/event-stream"
            self.data = io.BytesIO(
                b'data: {"type":"response.completed","response":{"status":"completed","model":"test-model","usage":{"output_tokens":1}}}\n\n'
            )

        def read1(self, n):
            return self.data.read(n)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    class Opener:
        def open(self, *a, **kw):
            return Response()

    monkeypatch.setattr(module, "build_opener", lambda *a: Opener())
    g = FypGateway(
        base_url="https://test.invalid/v1",
        api_key="fyp_software",
        model="test-model",
        budget_root=tmp_path / "budget",
        timeout_s=2,
    )
    cfg = {
        "work_root": str(tmp_path / "calls"),
        "executable": str(wrapper),
        "model": "test-model",
    }
    args = dict(
        config=cfg,
        system="data",
        schema={"type": "object"},
        validator=lambda v, r: v,
        role="judge_author",
        output_tokens=30,
        gateway=g,
        with_folder=True,
    )
    result, folder = structured.structured_call({"data": "software"}, **args)
    receipt = json.loads((folder / "receipt.json").read_text())
    assert result == {"ok": True} and receipt["status"] == "complete"
    assert receipt["transport"]["records"][0]["status"] == "complete"
    assert g.budget._read()["reservations"][0]["status"] == "complete"
    assert structured.structured_call({"data": "software"}, **args)[1] == folder
    assert len(g.budget._read()["reservations"]) == 1


@pytest.mark.parametrize(
    "cause", ["pending", "duplicate", "count", "tokens", "deadline"]
)
def test_budget_failures_prevent_further_upstream_spend(tmp_path, monkeypatch, cause):
    from datafarm.adapters.agent_harness.codex import fyp_gateway as module

    now = 1000.0
    monkeypatch.setattr(module.time, "time", lambda: now)
    budget = RequestBudget(
        tmp_path,
        fingerprint="offline",
        max_requests=1 if cause == "count" else 3,
        max_output_tokens_total=20 if cause == "tokens" else 60,
        deadline_s=10,
    )
    rid, _ = budget.reserve("first", 20, "request-first")
    if cause != "pending":
        budget.finish(rid, {"status": "complete"})
    if cause == "deadline":
        now += 11
    identity, digest = (
        ("first", "request-first")
        if cause == "duplicate"
        else ("second", "request-second")
    )
    with pytest.raises(RuntimeError):
        budget.reserve(identity, 1, digest)
    assert len(budget._read()["reservations"]) == 1


@pytest.mark.parametrize("field", ["previous_response_id", "conversation", "unknown"])
def test_structured_gateway_rejects_history_or_unknown_semantics(field):
    from datafarm.adapters.agent_harness.codex.fyp_gateway import normalize_request

    raw = {
        "model": "test-model",
        "input": "offline",
        "stream": True,
        field: "private-history",
    }
    with pytest.raises(ValueError):
        normalize_request(raw, model="test-model", output_tokens=20, allow_tools=False)


def test_gateway_forces_no_server_storage_without_changing_input():
    from datafarm.adapters.agent_harness.codex.fyp_gateway import normalize_request

    raw = {"model": "test-model", "input": "offline", "stream": True, "store": True}
    normalized, removed = normalize_request(
        raw, model="test-model", output_tokens=20, allow_tools=False
    )
    assert removed == ["store"]
    assert normalized["store"] is False
    assert normalized["input"] == raw["input"]
    assert raw["store"] is True
