# B-line finding: G2 gripper width overshoots its own speed check (lab30, 2026-11-18)

Repro: runs/chain/gripper_repro.py (accepted scene, no code under test changed). Master `left_g2_joint`
ramped at a constant reference rate; the measured width comes from IsaacRobotAPI.observe() (finger joints).

| commanded master rate (m/s) | max measured width rate (m/s) | steps over rate | max mimic error (m) |
|---|---|---|---|
| 0.01 | 0.061 | 21/960 | 0.00044 |
| 0.03 | 0.180 | 22/400 | 0.00125 |
| 0.10 | 0.639 | 24/204 | 0.00467 |

The fingers are passive (zero-gain) mimic followers of the master (robot package MIGRATION_CHANGES.md);
the native mimic constraint makes them jump ~6x the reference rate in the first steps and oscillate.
TCPMotionService.move_gripper fails with SPEED_LIMIT when |Δwidth| per step > speed_m_s*dt + 1e-6, so
every gripper command at any requested speed fails on the first steps. This blocks every grasp.
