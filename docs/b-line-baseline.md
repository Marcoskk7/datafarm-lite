# B-line development baseline

Updated: 2026-11-13 (Asia/Shanghai).

User requirement: continue development from the new branch a teammate pushed to GitHub.

## Source and branches

- Repository: https://github.com/Marcoskk7/datafarm-lite
- Baseline branch: `translate-view`
- Baseline commit: `73669e9d9f0a805ef0acdbb0a2e7511dbfa67fc7`
- Local development branch: `feat/b-control`
- Merged existing CaP feature commit: `f937fc8d10db0453236a644816a4de256ae97adc`
- The baseline contains two Chinese translation commits covering documentation, prompts, comments and docstrings; they are not acceptance of any new robot task.
- The merge kept the Chinese documentation and restored the CaP documentation index and the expert-harness entry point. The original branch is kept.

## Software verification

`python -m unittest test_cap_search test_cap_xh test_cap_broker_integration -q`, run from a temporary directory inside the workspace: 17 tests, 15 passed and 2 skipped because of the POSIX broker limitation on Windows.

The default sandbox temporary directory once caused WinError 650 during atomic file replacement; switching to the workspace directory `tmp/b-control-tests` fixed it. On the first rerun one 30 ms broker test timed out; running again, every runnable test passed. This is kept as a test-timing flakiness risk; no product code or test deadline was changed.

`python tools/lint_standing_rules.py`: 11 rules, no violations.

The full pytest suite was not run: the plain Python used at the time lacked pytest. The instruction-layer audit did not pass because the upstream `datafarm.common` failed to import `fcntl` on Windows. These CPU checks do not show that robot pick-and-place, LLM success rate or physical replay pass.

## Simulation backend decision

The user explicitly asked in this conversation to "cancel this probe and directly use Isaac Lab 3.0 as the simulation backend". This replaces the earlier proposal to choose or switch backends through a pick-and-place probe.

- The simulation framework is fixed to Isaac Lab 3.0, reusing the standalone environment in the workspace `IsaacLab-3.0` and its validated robot assets.
- Physics keeps the existing validated configuration Newton + MJWarp (`newton_mjwarp`); Isaac Lab is the simulation framework and Newton/MJWarp is its physics backend configuration.
- The separate backend-selection / pick-and-place probe stage, the "at least 8 successes out of 10 before choosing a backend" gate and the SAPIEN fallback development path are cancelled.
- Development goes straight to the Robot API, task contract and Episode interfaces; task success rate, recording accuracy and replay acceptance are measured in the corresponding development stages.
- The current asset acceptance covers a headless robot-dynamics baseline only; cameras, task pick-and-place and full scene integration are not yet accepted.

## B-line continuation path

The interface review is based on the [B-line interface design](reference/b-control-interfaces.md) (2026-11-14, `b-control/0.1.0-draft`). It contains the A/B handoff fields, coordinate and time conventions, Robot API, Judge, Episode, replay/HDF5 and a hand-written protocol example; the implementation status of each module is judged by its actual test evidence. Scene collision keeps the existing asset/entity/collision-group fields.

1. Fix the SceneGraph handoff fields with the A line, and the units, frames, quaternions, time and joint semantics for Judge/Episode.
2. In the fixed Isaac Lab 3.0 environment, reuse `IsaacLab-3.0/validated_usd/airbot_single/robot.usda` and `robot_profiles.py` to implement the Robot API directly: reset, observation, end-effector control, gripper, stepping and contact reading, and connect it to the A-line scene.
3. Connect the existing `src/datafarm/cap/`, freeze the Judge, and separate development and hidden-evaluation seeds.
4. Once hidden evaluation reaches 80%, first collect one Episode for integration with the A line, complete the replay check (position error at most 1 cm) and HDF5 export, then scale to 20.

The workspace `DataFarm-MVP` and `aspire-agent` are reference implementations, not a new baseline for this branch. Earlier pass records for push-plate-into-drawer and non-contact tasks do not replace full pick-and-place acceptance.

## Status update 2026-11-18 (branch `fix/b-line-review`)

- Expert-caused execution failures are now classified `expert_error` (counted) via `EXECUTION_<STATUS>:<REASON>`.
- Scene worker policy relaxed and shared with a host pre-check; scene brief template added.
- Contact snapshots carry `complete`/`truncated` coverage from `nacon`/`naconmax` (still no force/normal, no substeps).
- `move_gripper` reports `blocked` / `GRIPPER_BLOCKED`.
- HDF5 export implemented (`adapters/export/hdf5_episode.py`, `entry/episode_export.py`).
- Open: import-layer debt between `judge`, `expert` and `cap` (see architecture.md); real Isaac Lab 3.0 runs not yet executed for these changes.
