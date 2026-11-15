# Robot API auto-discovery and binding

Implemented: 2026-11-14. Scope: URDF kinematic chain, model TCP/gripper semantics, joint-name mapping, capability declaration, Isaac Lab 3.0 state reading and position-target staging. A single environment is supported, always using the existing Lab 3.0 / Newton configuration; the cancelled backend-selection probe is not run.

## Order of use

1. Discover link/joint topology, origin, axis, type, hard limits, mimic and declared collision geometry from source-bound URDF bytes; no GPU, language model or Isaac Lab is loaded.
2. Prefer a named TCP/end frame; for two-finger opposing prismatic mechanisms, derive the opening from collision geometry and the full mimic affine relation. An explicit RobotProfile can select a candidate, add control groups, or override the model definition with a sourced configuration.
3. For an initialized Isaac Lab articulation, read the actual joint_names/body_names automatically, index them by name, and check the source joint set and position limits.
   Sending position targets additionally checks for an effective position drive using the actual runtime stiffness; a zero gain or unknown drive does not grant control merely because the joint is independent.
4. Use position control as the default mode, propagate hard limits of mimic follower joints from the URDF, and produce the binding and capabilities. Runtime gains and effort limits are read, never modified; without an effective position drive, commands are refused. Any new run must regenerate and save the binding.

The URDF provides structure and model definitions; the running instance provides the actual ordering. With a bare USD only, the matching URDF must still be supplied; generic reverse parsing of USD is not implemented. After the importer merges fixed frames, poses are composed along URDF fixed-joint chains onto a surviving runtime body; frames are never substituted across movable joints.

The descriptor version is `robot-descriptor/2`, the binding version `robot-binding/2` and the profile version `robot-profile/2`. Older descriptor/profile versions are rejected explicitly and must be regenerated or migrated explicitly after review; an old binding digest cannot be reused as is. The new model-selection fields are listed in the table below. The original profile is kept intact in `profile`, the definitions actually used are in `semantics`, each recording its `selection_source`; `semantic_diagnostics` records why a feature is not enabled.

### Evidence scope of model semantics

- Only frames named `tcp`, `tcp_link`, `tool0`, `tool_frame` or `end_link` (optionally prefixed) are recognized automatically. A single candidate is used directly and marked `named_model_frame`; this does not prove a measured contact centre. With several candidates, left and right arms are not guessed.
- Opening derivation requires two prismatic finger joints with the same parent link, opposing and parallel, sharing one independent master joint. From the joint origin/axis, all mimic ratios/offsets, master and follower limits and the collision envelopes of both fingers, it derives `q = joint_offset + joint_per_m * width`. Box and ASCII/binary STL meshes, mesh scale and collision origin are supported; non-linear or revolute grippers, missing meshes and unsupported shapes keep a diagnostic and disable the automatic gripper capability.
- Geometric opening is marked `collision_envelope_gap` / `model_geometry_only`; no real finger-pad calibration or contact is claimed. Meshes are read through an explicit resolver and their content hash is stored; no sizes come from a language model. By default only relative paths inside the URDF directory are allowed; crossing directories requires an explicit `asset_root`, and `package://` is mapped explicitly through the Python resolver's `packages`.
- `observe().gripper.width_m` is computed from the measured positions of both finger joints through the geometric mapping and may differ from the master-joint target; with an explicit profile mapping it is computed from the measured master joint. The target opening is never reported as a measurement, and out-of-range readings are not clipped.
- Self-collision geometry comes from the URDF, but actual self-collision checking, scene collision and unreachability diagnosis still need a checker/IK/planner. This work does not claim those are enabled.

## Command line without simulation

Run from the repository root. Input paths are provided by the current deployment and do not enter the portable binding record:

```powershell
python tools/discover_robot.py '<robot.urdf>' --output runs/my-task/robot-binding.json
python tools/discover_robot.py '<robot.urdf>' --profile '<profile.json>' --output runs/my-task/robot-binding.json
python tools/discover_robot.py '<robot.urdf>' --asset-root '<asset-package>' --output runs/my-task/robot-binding.json
```

The static result has `runtime_bound=false` and `verification=source_structure_and_model_semantics_only`; it does not mean USD dependencies, physical response or cameras pass acceptance. When persisted into the pipeline, the URDF and the whole USD dependency package are bound through the existing ArtifactStore/ArtifactRef; this tool does not replace full Episode admission.

## Python integration

```python
from datafarm.simulation.robot_binding import discover_urdf
from datafarm.simulation.isaac_robot import IsaacRobotAPI
from datafarm.simulation.robot_semantics import file_mesh_resolver

# source_bytes is obtained by the deployment resolver from the source reference.
descriptor = discover_urdf(source_bytes, mesh_resolver)  # resolver(uri) -> exact mesh bytes
# robot is an articulation initialized by the host; profile is an explicit configuration or None.
api = IsaacRobotAPI(robot, descriptor, profile)
binding = api.binding
state = api.observe()                  # does not advance physics
caps = api.capabilities()
result = api.set_joint_targets({joint_name: target})  # status='staged'
# The host calls robot.write_data_to_sim(), sim.step(), robot.update(dt).
```

`set_joint_targets` validates non-empty named targets, finite values and effective URDF position limits (including mimic propagation), rejects unknown, fixed and mimic follower joints, and then sends targets by the current runtime index. It only stages targets: no interpolation, velocity control, stop/reset or stepping, and it does not claim the target was reached. The host still has to implement the rate limits, budgets, recording and runtime velocity monitoring from the interface design.

When neither the model nor the profile gives an unambiguous definition, the corresponding `tcp_pose` / `set_gripper` is false. `move_tcp` is currently always unsupported, pending an IK/motion planner. The local Lab 3.0 source already contains DifferentialIKController (with methods such as dls/adaptive_dls) and PinkIKController, so no inverse-kinematics algorithm needs rewriting; the kinematic chain, Jacobian/TCP offset, effective limits and collision checks still need adapting. Contact is unavailable and cameras are not_discovered; missing observations are never filled with zeros.

## RobotProfile format

`schema_version` must be `robot-profile/2`. The table describes the fields; concrete values come from task configuration. This repository ships no unconfirmed AIRBOT TCP or gripper calibration.

| Field | Content |
|---|---|
| `source_sha256` | Optional; binds the exact URDF byte digest; must match when given |
| `groups` | Group name to named joint list; no duplicates, cross-group overlap or passive joints |
| `control_mode` | Optional, default `position`; other modes are currently rejected |
| `model_tcp` | Optional; selects the link name of one model TCP candidate, such as the left/right end_link of a dual arm |
| `model_gripper` | Optional; selects the master_joint name of one model gripper candidate |
| `tcp.link` | Explicit override; a source-model link, resolvable to a runtime body through a fixed chain |
| `tcp.position_m` / `tcp.quaternion_xyzw` | Fixed link → TCP transform; XYZW must be normalized |
| `tcp.calibration_ref` | ArtifactRef with kind, 64-character lowercase sha256 and media_type; the content itself must be verified by the deployment resolver |
| `gripper.master_joint` | Independently controllable master joint |
| `gripper.width_range_m` | Effective two-finger opening range [min,max] |
| `gripper.joint_per_m` / `joint_offset` | Explicit affine mapping q = offset + scale × width; negative scale is supported; non-linear mechanisms are not covered |
| `gripper.calibration_ref` | ArtifactRef for the evidence behind the mapping; checking the reference structure does not mean the real calibration content was verified |

A profile may provide only groups and leave the rest to the model; explicit `tcp` / `gripper` take precedence over automatic candidates and are recorded as `explicit_profile`. Overrides still need a calibration_ref; they do not rewrite the model file or relabel model derivations as measured. A dual arm can select the left side explicitly with `{"schema_version":"robot-profile/2","model_tcp":"left_end_link","model_gripper":"left_g2_joint"}`.

## A-line scene collision

The existing scene_ref / asset_bundle_ref, entities / frames and collision_groups are reused; see [existing collision handoff](b-control-interfaces.md#31-providing-collision-information-through-existing-interfaces). No A-line limit payload is added. The asset bundle provides collision shapes, the scene provides object poses and initial articulation states, and the runtime reads the current state; B's collision checking/planning uses these to judge pose-dependent scene limits. The current interface definition can carry this information, but actual asset completeness and scene obstacle avoidance still need load validation and a checker.

## Runtime integration check

Run in the existing standalone Isaac Lab 3.0 environment (the runtime root is the deployment directory that provides robot_profiles.py):

```powershell
& '<runtime-root>/run.ps1' python tools/check_isaac_robot_binding.py `
  --runtime-root '<runtime-root>' --asset airbot_single `
  --output runs/my-task/live-binding.json
```

`--asset` also accepts the deployment's existing airbot_dual and dualxarm; these select deployment assets for the check script and are not a robot registry of the API. The script binds the actual ordering, reads state, stages named targets at the current pose, advances one step, and checks that mimic follower joints are refused. `--profile` and `--asset-root` are optional. `--exercise-model-semantics` additionally tests three openings in free space: the test host ramps the position target at 50% of the master joint's URDF velocity limit, then holds and reads the measured finger joints; it also compares the TCP with forward kinematics computed independently from the original URDF. This test ramp is not a production planner; for a dual arm a side must be selected first. The script does not evaluate pick-and-place, TCP planning, contact or visual quality.

CPU check: with `PYTHONPATH=src;tests` (Windows environment variable value), run `python -m unittest test_robot_binding test_robot_model_semantics -v`. It covers joint ordering, fixed-chain pose composition, ambiguous selection, mesh hashes and path boundaries, mimic ratio/offset/limit propagation, measured finger-joint opening, invalid targets, version/source mismatch, missing drives and non-finite state.

To keep standalone structure reading from triggering the upstream POSIX-only fcntl import, the simulation package exports by name lazily; existing public export names are unchanged. This does not change the original broker's runtime limitation on Windows.