# Action replay

`datafarm.entry.episode_replay.replay_episode` replays every recorded actuator input
and compares every measured state, including robot/object roots, velocities,
articulation q/dq and body/TCP poses. Tolerances are explicit and unit-specific;
quaternion sign is rotation-equivalent. Missing coverage or a step failure never
passes. The trusted host must independently load and qualify the same source
assets/runtime, own the physics clock and supply its exact source binding.

`IsaacReplayHost` restores measured roots/joints/velocities then applies actual
recorded targets, not requested TCP goals. Hidden solver state is not available;
contact, bitwise replay and rendering remain unverified. The separate immutable
report binds the Episode ref. It does not change the original Episode or confer
Judge acceptance. Reports from fixtures only qualify software, not live physics.
