# 1b request→layout authoring: real run on lab30run (2026-11-17)

Code under test: branch `feat/A-request-authoring` at `f8efb788f6c4a87590e1d1f16908cdeedcc98493`. It was exported with `git archive`
to `lab30run:/opt/data/private/isaaclab/request-authoring/b`. No code changes were made for this run.

## Machine and environment

- `lab30run` (n001, RTX 4090, Isaac Lab 3.0 / Isaac Sim 6.2). The GPU was idle and no other Isaac jobs were running when the run started.
- Repo venv: `.venv` created with uv-managed CPython 3.12 (`~/.local/bin/python3.12`), then `pip install -e '.[dev,workflow]'`.
  PyPI was reachable directly, so no mirror was needed.
- Prepare and check used `/home/isaac/IsaacLab30/.venv/bin/python` (pxr + coacd present), after `source /home/isaac/isaaclab30_env.sh`.
- Agent: Claude Code 2.1.325 (`/home/isaac/node/bin/claude`), model `claude-opus-5-5`.
  On this host Claude Code is not logged in: a bare `claude -p` returns "Not logged in". It works through the
  local Anthropic→OpenAI adapter on `127.0.0.1:4001` (`~/fyp-bridge/anthropic_openai_adapter.py`, which forwards to the
  FYP gateway). `launch.sh` therefore exports `ANTHROPIC_BASE_URL=http://127.0.0.1:4001` and the adapter's
  placeholder token `dummy`; this is not a credential. Both variables reach the agent via `env_passthrough`.
- Agent config `scene-author.json`: a copy of `configs/scene-author.template.json` with `cli` set to an absolute path and
  `price_table` pointing to a local copy of `prices.json`.

## Commands

```bash
# targeted tests (in the synced checkout)
CUDA_VISIBLE_DEVICES= PYTHONPATH=src:tests .venv/bin/python -m pytest -q \
  tests/test_scene_authoring.py tests/test_scene_conformance.py tests/test_scene_layout.py   # 30 passed
# authoring loop: launch.sh, started with setsid nohup, log in run.log
# GPU check: check.sh, the exact next_step from scene-authoring-report.json, log in check.log
```

Remote work dir: `/opt/data/private/isaaclab/request-authoring/runs/1b-20261117-023625`.

## Results

| Round | Agent | Wall | Cost (provider) | Host verdict |
| --- | --- | --- | --- | --- |
| 1 | ok, 38 turns, 37 tool uses | 1007 s | $8.18 (1,758,855 in / 57,211 out tokens) | prepare failed: `anchor_body` `"/RootNode/Geometry/body_01_obj_00"` rejected by the kernel's portable-value check (`SchemaError: ... absolute filesystem path`). Conformance and plan were not run. |
| 2 | ok, 7 turns, 6 tool uses | 69 s | $1.03 (245,471 in / 2,584 out tokens) | **accepted**: conformance ok and complete, plan ok (footprint ok, 4/4 reach targets, max error 0.31 mm) |

Total for the loop: 2 rounds, $9.21, about 18 minutes.

GPU `check` (exit 0, `prepared/854fc489b3884bc0/check.json`):

| Metric | This run | Hand-written baseline (context only) |
| --- | --- | --- |
| Settle drift | 1.028 mm (stable) | 1.04 mm |
| Max penetration | 54 µm (tolerance 3 mm) | 47 µm |
| Probe | `plate_in_top_drawer` ok, plate rests at z = 0.7594 m on `object.cabinet` | z = 0.759 |
| Reachability | 4/4 (kinematic) | 10/10 |

The two layouts use the same three SimReady assets (`table_metal_stainless_a03`, `fixture_cabinet_b01`,
`dishware_plastic_bigPlate_a01`) but differ in placement. In this run the cabinet is at (0, −1.15) with yaw 90°
and the robot is at (0, −0.2). The baseline has the cabinet at (1.3, 0.215) with yaw −90°, the robot at (0.84, −0.1),
and six drawer targets instead of two.

## Observations

- Round 1 failed because `docs/ir/scene-layout.md` asks for `anchor_body` "in `prim_ref` form" but never says that
  this means no leading `/`. `scene/layout.py` does not validate the field either, so the format error only surfaces as
  a traceback deep inside host prepare. The loop handled it as designed: the prepare error went back to the agent as a
  diagnostic, and the agent fixed it in round 2. This did not block the run, so no code was changed. It cost one round ($1.03).
- The transcripts show no access to `examples/scene/`. The agent did go beyond the tools that the role instructions list:
  it read the SimReady cache USD files and the robot package URDF directly, ran its own pxr bbox/FK scripts with the
  Isaac venv python (USD reads only, no simulator), and ran `check_conformance` itself before stopping. The role
  instructions say this, but nothing enforces it (`isolation: none`).
- Session transcripts (`sessions/`) and `workspace/` are not copied here. They remain on the remote work dir.

## Not verified

- Grasp feasibility and collision-aware motion: reachability is kinematic IK only.
- Whether the two drawer reach targets are enough to cover the release region. The baseline uses six.
- Repeatability: this was a single run. Agent quality and cost will vary between runs.
- Any downstream stage (expert, episodes, render).
