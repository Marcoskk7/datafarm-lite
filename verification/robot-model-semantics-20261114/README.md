# Model-first Robot API verification — 2026-11-14

Implemented URDF joint origins/axes/topology and collision declarations, named
model TCP selection, fixed-frame resolution after importer merging, and opposing
linear-finger opening inference from collision geometry plus mimic metadata.
Position control is the default. Model definitions and explicit profile overrides
have distinct provenance. Descriptor, profile and binding use version 2; version
1 is rejected rather than silently reinterpreted.

A/B SceneBinding fields remain unchanged. Existing scene/asset references,
entities/frames and collision_groups can convey A-line object geometry and poses.
No new scene-limit payload is required or implemented. Actual asset completeness
and scene collision planning have not been validated by these tests.

## Evidence

- 26 CPU unit tests passed (12 existing, 14 new), without Isaac Lab or credentials.
- Standing-rule lint: 11 rules, zero violations. `git diff --check` passed.
- Static discovery used existing AIRBOT single, AIRBOT dual and dual-xArm URDFs.
  Single AIRBOT automatically selects left_end_link and its G2 gripper. Multiple
  candidates are retained without silently selecting a side.
- Local Isaac Lab 3.0 / Newton AIRBOT single integration passed: model-defined
  left_end_link resolves onto the surviving left_link6 body with the authored
  transform (translation 0, 0, 0.2466 m and the authored rotation).
- Free-space gripper targets at approximately 14.4, 36.0 and 57.6 mm passed using
  a position ramp at 50% of the URDF master velocity limit. Opening is computed
  from actual finger joint readings, not command targets. TCP is checked against
  independent forward kinematics parsed directly from the original URDF.
- Maximum final opening error: 1.30e-7 m; maximum TCP FK position difference:
  8.20e-8 m; maximum rotation difference: 3.01e-7 rad. These are internal model
  consistency results, **not physical calibration accuracy**.

`summary.json` binds the inputs, code and retained raw reports by SHA-256.
`single-live-final.json` contains all three final checks and source hashes.
`single-live-step-failure.json` retains an earlier failed abrupt-target test:
the second target caused finger state divergence (~0.122 m opening error).
The subsequent ramp is a test-host trajectory, not a production motion planner.
The original first abrupt attempt exited before writing JSON; its assertion was
visible in the execution log. It is not counted as a successful test.

The instruction-layer audit could not run in Windows because the existing
datafarm.common import requires POSIX fcntl. The attempt to run Linux checks in
the existing server checkout timed out during SSH connection, before transfer
or execution. No Linux or full-suite result is claimed for this change.

## Limits

TCP naming records a model frame, not an independently calibrated contact center.
Opening inference supports two opposing sibling prismatic fingers, boxes and
STL meshes. Unsupported or ambiguous mechanisms remain unavailable with reasons.
No grasp, contact sensor, obstacle avoidance, IK execution, scene generation or
task success is accepted by these checks. The installed Lab controllers already
include DifferentialIKController and PinkIKController; move_tcp remains disabled
until their runtime adaptation and collision-aware execution are implemented.

Reproduce CPU checks with PYTHONPATH set to src and tests:

```text
python -m unittest test_robot_binding test_robot_model_semantics -v
python tools/lint_standing_rules.py
```

Use the deployment's existing Lab environment for the live check:

```text
python tools/check_isaac_robot_binding.py --runtime-root <runtime> --asset airbot_single --exercise-model-semantics --output <new-evidence-file.json>
```
