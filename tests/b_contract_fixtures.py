"""Portable B software fixtures; A scene builders are consumed read-only.

These synthetic measurements do not claim real robot or simulator qualification.
"""

from copy import deepcopy
import json
from datafarm.expert.episode import EpisodeRecorder, UNITS
from datafarm.kernel.artifacts import ArtifactRef
from datafarm.kernel.io import sha256_bytes
from datafarm.kernel.store import ArtifactStore
from datafarm.adapters.inputs.request_yaml import parse_request, load_request
from datafarm.task.planning import load_scene_group, build_context


def REF(kind):
    return dict(kind=kind, sha256="a" * 64, media_type="application/json")


SETUP = dict(
    episode_id="episode.a",
    task_id="task.a",
    task_revision="rev.a",
    attempt_id="attempt.a",
    request_ref=REF("task-request"),
    original_request_ref=REF("task-request-source"),
    task_ref=REF("task-ir"),
    scene_ref=REF("ir.scene-graph"),
    runtime_ref=REF("sim.runtime-profile"),
    expert_ref={**REF("expert"), "media_type": "text/x-python"},
    asset_refs=[REF("ir.asset-record")],
    control_dt_s=0.1,
    node_ids=["robot", "object.a"],
    source_sha256={"host": "b" * 64},
)


def reading():
    return dict(
        availability="available",
        asset_id="asset.a",
        position_m=[0, 0, 0],
        quaternion_xyzw=[0, 0, 0, 1],
        linear_velocity_m_s=[0, 0, 0],
        angular_velocity_rad_s=[0, 0, 0],
        joints=[],
        links={},
    )


def state(step):
    return dict(
        control_step=step,
        time_s=step * 0.1,
        units=deepcopy(UNITS),
        frame_id="world",
        entities={n: reading() for n in SETUP["node_ids"]},
        robot={"q": [0.0], "dq": [0.0], "joint_names": ["joint"]},
        contacts=dict(
            control_step=step,
            sampling="cycle_end_snapshot",
            availability="unavailable",
            rows=None,
            coverage="unknown",
        ),
    )


def recorder():
    r = EpisodeRecorder(deepcopy(SETUP), max_steps=8, max_bytes=50000)
    r.initial(state(0))
    return r


def raw_request():
    return {
        "schema": "datafarm.task-request/1",
        "request_id": "request.a:b",
        "demand": "Move the requested object to the requested receptacle.",
        "goals": [
            {"id": "goal.place", "text": "Place the object inside the receptacle."}
        ],
        "constraints": [{"id": "safe.robot", "text": "Respect joint limits."}],
        "forbidden_shortcuts": [
            {"id": "no.teleport", "text": "Do not teleport the object."}
        ],
        "parameters": [
            {
                "id": "position.tolerance",
                "value": 0.01,
                "unit": "m",
                "purpose": "task_success",
                "origin": "provided",
                "basis": "Explicit requested tolerance",
                "requirement_ids": ["goal.place"],
            }
        ],
        "task_count": 1,
        "episode_count": 3,
    }


def parse(raw):
    data = json.dumps(raw).encode()
    ref = ArtifactRef(
        kind="task-request-source",
        sha256=sha256_bytes(data),
        media_type="application/yaml",
    )
    return parse_request(data, ref)


def planning_fixture(tmp_path):
    from test_scene_layout import records, SPEC, URDF, ref
    from datafarm.scene import layout
    from datafarm.simulation.robot_binding import discover_urdf

    store = ArtifactStore(tmp_path / "store")
    assets = records()
    descriptor = discover_urdf(URDF)
    nodes = layout.resolve_nodes(SPEC, assets)
    reach = layout.reachability(SPEC, nodes, assets, descriptor)
    refs = {
        k: store.put_json("ir.asset-record", v).to_dict() for k, v in assets.items()
    }
    graph = layout.build_scene_graph(
        spec=SPEC,
        nodes=nodes,
        records=assets,
        record_refs=refs,
        descriptor=descriptor,
        asset_bundle=ref("scene.asset-bundle", "e").to_dict(),
        ledger=ref("ir.physics-ledger", "f").to_dict(),
        runtime_profile=ref("sim.runtime-profile", "0").to_dict(),
        reach=reach,
        settle={"ran": True, "steps": 240, "max_drift_m": 0.0004, "stable": True},
        penetration={"ran": True, "max_depth_m": 0.0, "ok": True},
    )
    scene = store.put_json("ir.scene-graph", graph).to_dict()
    capability = {
        "schema": "datafarm.task-capabilities/1",
        "scene_ref": scene,
        "api": {
            "move": "move a target using measured feedback",
            "observe": "query measured scene state",
        },
        "program_contract": {
            "entry_point": "Expert",
            "signature": "next_plan(observation)",
            "return": "bounded action or None",
            "allowed_imports": ["math"],
            "action_semantics": "requested target is not an achieved state",
            "observation_mode": "privileged_state",
        },
        "observations": [
            {
                "id": "pose",
                "availability": "available",
                "unit": "m",
                "frame_id": "world",
            },
            {
                "id": "contact.force",
                "availability": "unavailable",
                "reason": "A contact query exposes distances, not forces",
                "unit": "N",
                "frame_id": "world",
            },
        ],
        "control_budgets": {"max_actions": 20, "max_steps": 200, "max_wall_s": 30},
    }
    request_path = tmp_path / "request.yaml"
    request_path.write_text(json.dumps(raw_request()))
    request, request_ref = load_request(request_path, store)
    members = load_scene_group(store, [scene], [capability])
    context = build_context(request, request_ref.to_dict(), "group.one", members)
    proposal = {
        "schema": "datafarm.task-plan-proposal/1",
        "tasks": [
            {
                "name": "Object placement",
                "scene_id": graph["scene_id"],
                "requirement_ids": [
                    "request.demand",
                    "goal.place",
                    "safe.robot",
                    "no.teleport",
                ],
                "roles": [
                    {"role": "object", "node_id": "object.plate", "frame_id": "world"},
                    {
                        "role": "target",
                        "node_id": "object.cabinet",
                        "frame_id": "world",
                    },
                ],
                "needed_apis": ["move", "observe"],
                "needed_observations": ["pose"],
                "episode_count": 3,
            }
        ],
        "unresolved": [],
    }
    return store, request_path, scene, capability, context, proposal
