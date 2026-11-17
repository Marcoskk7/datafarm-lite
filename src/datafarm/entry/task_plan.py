"""Composition root: YAML + A refs + host capabilities -> immutable B task table."""
from __future__ import annotations

import argparse
from collections.abc import Mapping
import importlib
import json
from pathlib import Path
from typing import Any

from datafarm.adapters.agent_harness.task_plan import TaskPlanProvider
from datafarm.adapters.inputs.request_yaml import load_request
from datafarm.cap.public_task import project_public_task, validate_public_task
from datafarm.kernel.artifacts import ArtifactRef
from datafarm.kernel.identifiers import storage_key, write_once_json
from datafarm.kernel.io import read_json, sha256_file, sha256_json, utc_now
from datafarm.kernel.store import ArtifactStore
from datafarm.task.planning import build_context, compile_table, load_scene_group, validate_proposal

SOURCE_MODULES = ("datafarm.entry.task_plan", "datafarm.adapters.inputs.request_yaml",
                  "datafarm.adapters.agent_harness.task_plan", "datafarm.adapters.agent_harness.codex.structured",
                  "datafarm.task.planning", "datafarm.task.request_profile", "datafarm.cap.public_task",
                  "datafarm.kernel.identifiers", "datafarm.kernel.schema", "datafarm.kernel.store", "datafarm.kernel.io",
                  "datafarm.ir.scene_graph", "datafarm.ir.asset_record", "datafarm.ir.common",
                  "datafarm.grounding.quantities")


def _source_pin() -> dict[str, str]:
    return {name: sha256_file(importlib.import_module(name).__file__) for name in SOURCE_MODULES}


def _bind_loaded_result(store: ArtifactStore, result: Mapping[str, Any]) -> dict[str, Any]:
    table = store.load(ArtifactRef.from_dict(result["table_ref"]))
    if sha256_json(table) != result["table_sha256"] or table["table_id"] != result["table_id"]:
        raise ValueError("cached task table differs from its bound result")
    for ref in result["public_task_refs"]:
        validate_public_task(store.load(ArtifactRef.from_dict(ref)))
    return dict(result)


def plan_request(*, request_yaml: str | Path, scene_refs: list[Mapping[str, Any]],
                 capabilities: list[Mapping[str, Any]], scene_group_id: str, store: ArtifactStore,
                 out_dir: str | Path, provider_config: Mapping[str, Any], approve_model: bool = False,
                 max_model_calls: int = 1, output_tokens: int = 5000, call: Any = None) -> dict[str, Any]:
    """A bounded planning API. Configuration/approval are host arguments, never YAML fields.

    Completed calls are reused after validation. A pending/failed operation is not
    resubmitted. The budget is one invocation per pinned operation, advisory for
    monetary spend: unknown cost is not zero. This does not execute a scene.
    """
    if type(approve_model) is not bool:
        raise ValueError("model approval must be an explicit boolean")
    if type(output_tokens) is not int or not 1 <= output_tokens <= 16000:
        raise ValueError("invalid planning token budget")
    from datafarm.adapters.agent_harness.codex.structured import validate_config

    validate_config(provider_config)
    if type(max_model_calls) is not int or max_model_calls < 1:
        raise ValueError("planning call budget must be a positive integer")
    request, request_ref = load_request(request_yaml, store)
    members = load_scene_group(store, scene_refs, capabilities)
    context = build_context(request, request_ref.to_dict(), scene_group_id, members)
    pin = _source_pin()
    model_config = {k: v for k, v in provider_config.items() if k != "work_root"}
    # Host executable/config locators are represented by a digest, not copied to artifacts.
    identity = {"context_sha256": sha256_json(context), "source_sha256": sha256_json(pin),
                "provider_config_sha256": sha256_json(model_config), "output_tokens": output_tokens}
    operation_id = storage_key("task-plan", identity)
    folder = Path(out_dir) / operation_id
    cached = folder / "result.json"
    if cached.is_symlink() or (folder.exists() and folder.is_symlink()):
        raise ValueError("planner operation or result cannot be a symlink")
    if cached.exists():
        result = read_json(cached)
        if result.get("identity") != identity or result.get("operation_id") != operation_id:
            raise ValueError("changed task-plan result identity")
        intent = read_json(folder / "intent.json")
        if intent.get("identity") != identity or intent.get("source_pin") != pin:
            raise ValueError("changed planner source or intent")
        if any((folder / name).is_symlink() for name in ("intent.json", "proposal.json")):
            raise ValueError("planner evidence cannot be a symlink")
        saved = read_json(folder / "proposal.json")
        validate_proposal(saved, context)
        if result.get("proposal_sha256") != sha256_json(saved):
            raise ValueError("changed completed planner proposal")
        # Rebuild deterministic bindings without making any provider call. A cached
        # table/public-task replacement must not be legitimized by changing its hashes.
        table, expected_ref = compile_table(store, context, saved, created_at=intent["created_at"])
        expected_public = []
        for entry in table["tasks"]:
            if entry["status"] == "planned":
                member = next(m for m in members if m["scene_id"] == entry["scene_id"])
                doc = store.document(ArtifactRef.from_dict(entry["task_ref"]))
                public = project_public_task(doc.payload, entry, request, request_ref.to_dict(), member)
                expected_public.append(store.put_json("cap-public-task", public).to_dict())
        expected_result = {"schema": "datafarm.task-plan-result/1", "operation_id": operation_id,
            "identity": identity, "table_id": table["table_id"], "table_ref": expected_ref.to_dict(),
            "table_sha256": sha256_json(table), "proposal_sha256": sha256_json(saved),
            "public_task_refs": expected_public, "model_calls": 1, "cost_usd": None,
            "cost_status": "unknown", "semantic_review": "unverified"}
        if result != expected_result:
            raise ValueError("cached result does not match original task-plan bindings")
        return _bind_loaded_result(store, result)
    if not approve_model:
        raise ValueError("a model proposal requires explicit host approval; YAML grants none")
    config = dict(provider_config)
    config["work_root"] = str(folder / "provider")
    provider = TaskPlanProvider(config, output_tokens=output_tokens, **({"call": call} if call is not None else {}))
    recovered = None
    try:
        folder.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        # Recover a completed immutable proposal locally; never resend a pending or
        # failed model request. The existing structured-call port verifies its own
        # completed decision/receipt before the controller copies it.
        intent_path = folder / "intent.json"
        if (folder.is_symlink() or intent_path.is_symlink() or not intent_path.is_file()
                or (folder / "failure.json").exists()):
            raise RuntimeError("task plan pending/failed; do not resubmit: " + operation_id) from exc
        intent = read_json(intent_path)
        if intent.get("identity") != identity or intent.get("source_pin") != pin:
            raise ValueError("changed pending planner intent") from exc
        if (folder / "proposal.json").is_file() and not (folder / "proposal.json").is_symlink():
            recovered = validate_proposal(read_json(folder / "proposal.json"), context)
        elif call is None:
            from datafarm.adapters.agent_harness.task_plan import SYSTEM
            from datafarm.adapters.agent_harness.codex import structured
            from datafarm.kernel.io import sha256_text
            from datafarm.task.planning import proposal_schema

            caller_sha = sha256_text(Path(importlib.import_module("datafarm.adapters.agent_harness.task_plan").__file__)
                                     .read_text(encoding="utf-8"))
            port_identity = sha256_json({"request": context, "schema": proposal_schema(context), "system": SYSTEM,
                "output_tokens": output_tokens, "config": provider.config,
                "provider_source": {"structured": sha256_json(Path(structured.__file__).read_text(encoding="utf-8")),
                                    "caller": caller_sha}})
            receipt_dir = Path(config["work_root"]) / port_identity
            if (not (receipt_dir / "decision.json").is_file()
                    or read_json(receipt_dir / "receipt.json").get("status") != "complete"):
                raise RuntimeError("model request still pending/failed; do not resubmit: " + operation_id) from exc
            recovered = validate_proposal(provider.propose(context), context)
        else:
            raise RuntimeError("task plan pending/failed; do not resubmit: " + operation_id) from exc
        created_at = intent["created_at"]
    else:
        created_at = utc_now()
        write_once_json(folder / "intent.json", {"schema": "datafarm.task-plan-intent/1", "operation_id": operation_id,
            "identity": identity, "source_pin": pin, "created_at": created_at, "max_model_calls": max_model_calls,
            "output_tokens": output_tokens, "request_ref": request_ref.to_dict(), "status": "starting"})
    try:
        proposal = recovered if recovered is not None else validate_proposal(provider.propose(context), context)
        write_once_json(folder / "proposal.json", proposal)
        table, table_ref = compile_table(store, context, proposal, created_at=created_at)
        public_refs = []
        for entry in table["tasks"]:
            if entry["status"] == "planned":
                member = next(m for m in members if m["scene_id"] == entry["scene_id"])
                task_doc = store.document(ArtifactRef.from_dict(entry["task_ref"]))
                public = project_public_task(task_doc.payload, entry, request, request_ref.to_dict(), member)
                public_refs.append(store.put_json("cap-public-task", public).to_dict())
        result = {"schema": "datafarm.task-plan-result/1", "operation_id": operation_id, "identity": identity,
                  "table_id": table["table_id"], "table_ref": table_ref.to_dict(), "table_sha256": sha256_json(table),
                  "proposal_sha256": sha256_json(proposal), "public_task_refs": public_refs,
                  "model_calls": 1, "cost_usd": None, "cost_status": "unknown", "semantic_review": "unverified"}
        write_once_json(folder / "result.json", result)
        return result
    except BaseException as exc:
        write_once_json(folder / "failure.json", {"schema": "datafarm.task-plan-failure/1",
            "operation_id": operation_id, "error_type": type(exc).__name__, "model_calls": 1,
            "cost_usd": None, "cost_status": "unknown", "status": "failed_or_interrupted"})
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan request tasks from explicit A SceneGraph refs; no execution")
    parser.add_argument("--request", required=True)
    parser.add_argument("--scene-group", required=True, help="local JSON: scene_group_id, scene_refs, capabilities")
    parser.add_argument("--provider-config", required=True, help="deployment-local Codex settings")
    parser.add_argument("--store", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--approve-model", action="store_true")
    parser.add_argument("--max-model-calls", type=int, default=1)
    parser.add_argument("--output-tokens", type=int, default=5000)
    args = parser.parse_args(argv)
    from datafarm.task.request_profile import object_fields

    try:
        group = read_json(args.scene_group)
        object_fields(group, {"scene_group_id", "scene_refs", "capabilities"}, label="scene group")
        result = plan_request(request_yaml=args.request, scene_refs=group["scene_refs"],
            capabilities=group["capabilities"], scene_group_id=group["scene_group_id"],
            store=ArtifactStore(args.store), out_dir=args.output, provider_config=read_json(args.provider_config),
            approve_model=args.approve_model, max_model_calls=args.max_model_calls, output_tokens=args.output_tokens)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, RuntimeError, OSError) as exc:
        # Do not print provider credentials, source paths or model response text.
        print(json.dumps({"status": "error", "error_type": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
