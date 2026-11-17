# scene-author OS sandbox under proot: real run on lab30run (2026-11-17)

Code under test: branch `feat/A-request-authoring` at `612823e` (`feat(scene-author): run每轮 agent 会话在 OS 沙箱里并如实记录隔离摘要`)
plus the uncommitted sandbox adapter package (see below). Nothing else uncommitted was taken.

## Machine and environment

- `lab30run` (RTX 4090, Isaac Lab 3.0 / Isaac Sim 6.2), GPU idle (`0 MiB`, `0 %`) and no other Isaac/python jobs
  running when the run started. Isaac Lab host expires 2026-11-18 23:05 SGT.
- `bwrap`/`unshare` are unusable in this container (no `CAP_SYS_ADMIN`), so the sandbox selects **proot 5.1.0**
  at `/home/isaac/tools/proot/bin/proot` (wrapper with `LD_LIBRARY_PATH`). proot has no read-only bind:
  `ro_soft: true`, `readonly_enforced: false` is the expected, honest summary; masks are still strict.
- Prepare/check interpreter `/home/isaac/IsaacLab30/.venv/bin/python` (pxr + coacd), `source /home/isaac/isaaclab30_env.sh`.
- SimReady cache `/opt/data/private/isaaclab/simready-cache` (bytes untouched), robot package
  `/opt/data/private/isaaclab/a-scene/runtime/robot_assets_isaaclab3_validated_20261113`, endpoint `https://hf-mirror.com`.
- Agent: Claude Code 2.1.325 (`/home/isaac/node/bin/claude`), model `claude-opus-5-5`, reached through the local
  adapter on `http://127.0.0.1:4001` with the adapter's placeholder token `dummy` (not a credential). Both reach the
  agent only through the config's `env_passthrough`.

## Sync recipe (and the uncommitted dependency)

`git archive` of the commit does **not** contain the sandbox adapter package: `src/datafarm/adapters/sandbox/` is the
user's uncommitted refactor (deleted `isolation.py`, split into `bwrap/jobwrapper/model/proot/registry/unshare`). The
snapshot is therefore:

```bash
git archive 612823e | ssh lab30 'mkdir -p /opt/data/private/isaaclab/request-authoring/c && tar -x -C .../c'
tar -czf - --exclude __pycache__ src/datafarm/adapters/sandbox | ssh lab30 'tar -xz -C .../c'   # working tree only
ssh lab30 'rm .../c/src/datafarm/adapters/sandbox/isolation.py'   # removed locally, still in the archive
```

Nothing else uncommitted (expert-harness work in the same tree) was copied. Venv: uv CPython 3.12, `pip install -e '.[dev,workflow]'`.

## Tests on the box

`PYTHONPATH=src:tests .venv/bin/python -m pytest -q tests/test_scene_authoring_sandbox.py tests/test_scene_authoring.py`
with `DATAFARM_PROOT_BIN=/home/isaac/tools/proot/bin/proot` so the real-proot test actually runs:

| Step | Result |
| --- | --- |
| first run, unfixed snapshot | 1 failed, 15 passed — `test_real_proot_round_...` asserted the repo path `docs/ir/scene-layout.md` was readable inside the guest; the sandbox correctly hides the repo and exposes the snapshot at `WORK/sandbox/repo` (test bug, not harness bug) |
| after fixing that assertion | 16 passed |
| after the interpreter-root fix below (+1 test) | 17 passed |

## Bugs found and fixed

1. **Real-proot test asserted the wrong path** (`tests/test_scene_authoring_sandbox.py`): it probed
   `<repo>/docs/ir/scene-layout.md` and expected `readable`, while the design is "repo absent, snapshot readable".
   It now probes `WORK/sandbox/repo/docs/ir/scene-layout.md` (readable) and the repo copy (hidden). This test had
   never run before: it is skipped without proot and only executes on Linux.
2. **The guest interpreter lost the venv** (`src/datafarm/entry/scene_author_sandbox.py`): `self.python` was the
   interpreter's `realpath`, so the guest ran the uv-managed CPython directly. CPython then finds no `pyvenv.cfg`,
   `sys.prefix` is the base installation, and `/home/isaac/IsaacLab30/.venv/lib/python3.12/site-packages` (pxr, coacd)
   is invisible — `simready.py` happened to work (stdlib + the snapshot only), but any venv package failed. Worse,
   binding only the realpath prefix leaves the venv `bin/python` symlink chain dangling:
   `.venv/bin/python -> …/uv/python/cpython-3.12-linux-x86_64-gnu/bin/python3.12 -> …/cpython-3.12.16-…/…`, and proot
   reported `'.../.venv/bin/python' not found`. Fixed by running the configured path and binding every hop:
   venv root, each symlink target directory, and the realpath prefix (`_interpreter_roots`), with a unit test that
   builds a two-hop venv layout.

Probe before any agent spend: `AuthoringSandbox.prepare_round(1, …)` + an in-guest check of the allowed tools.
`work/sandbox/rounds/001/isolation-probe.json` (also reproduced by the run itself): 23 masked paths all `HIDDEN`,
workspace + HOME + `/tmp` `WRITABLE`, 5 read-only-intent paths `WRITABLE` (proot `ro_soft`), 0 leaks, 0 umounts.
In-guest checks: cache and robot package listable, URDF readable, `~/.claude`, `~/.ssh`, `examples`, `runs`,
`verification`, `.git` absent, `/proc/uptime` and network fine, `pxr + coacd` import, `simready.py --endpoint
https://hf-mirror.com search plate` returns the pinned revision, `claude --version` = 2.1.325.

## Run

`launch.sh` (setsid nohup, log in `run.log`), remote work dir `/opt/data/private/isaaclab/request-authoring/runs/1b-proot-20261117-233440`.
Agent config is `scene-author.json` here; `sandbox.mode = proot`, `fallback = []`, `proot` = the absolute path,
`sandbox.python` = the IsaacLab venv interpreter, `extra_ro = []`.

| Round | Agent | Wall | Cost (provider) | Host verdict |
| --- | --- | --- | --- | --- |
| 1 | ok, 11 turns, 10 tool uses, exit 0 | 661 s | $1.75 (399,543 in / 7,403 out tokens, no cache tokens) | **accepted**: prepare ok, conformance ok + complete (0 problems, 0 skipped), plan ok (footprint ok; 2/2 reach targets solved, worst position error 0.23 mm, rotation 7.1e-5 rad) |

Total for the loop: 1 round, $1.75, 11 minutes (`stop_reason: accepted`; `--max-rounds 3` unused).

Accepted layout: the same three SimReady assets as 1b (`table_metal_stainless_a03`, `fixture_cabinet_b01`,
`dishware_plastic_bigPlate_a01`), cabinet at yaw 90° at (0, −1.15). `scene-layout.json` and `plan.json` here.

Isolation summary recorded in `agent-calls.jsonl` (`extra.isolation`), identical to the round-1 probe:
`mode proot`, `base allowlist`, `ok true`, `leaks 0`, `umounted 0`, `checked_masks 23`, `readonly_binds 6`,
`writable_readonly 6`, `unwritable_workspace 0`, `ro_soft true`, `readonly_enforced false`, `share_net true`.

## What the sandbox actually blocked vs what stayed instruction-only

Blocked by the OS sandbox (ENOENT inside the guest, verified in `blocked-reads.txt` against the same spec, and by the
pre-agent probe in `isolation-probe.json`): `examples/`, `runs/`, `verification/`, `.git`, `AGENTS.md`, `prompts/`,
the repository `src/`/`tools/`/`configs/` (the guest gets a snapshot at `WORK/sandbox/repo` instead), the run's own
`sessions/`, `attempts/`, `prepared/`, `attempts.jsonl`, `agent-calls.jsonl`, the host report, previous rounds'
HOMEs, and `~/.claude`, `~/.claude.json`, `~/.ssh`, `~/.config`, `~/.npmrc`, `~/.bash_history`. The agent could not
have read `examples/scene/` even if it ignored the role instruction — the path does not exist in the guest.

Instruction-only (not enforced): read-only intent on the snapshot, the SimReady cache, the robot package and the
interpreter prefixes — proot has no read-only bind and the guest runs as the same uid, so all 6 were writable and are
reported as `writable_readonly: 6` / `readonly_enforced: false`. Writes are therefore possible to the cache directory
and the robot package path; the copies of the layout/product files are host-side reads after the session, and the host
refuses a symlinked `scene-layout.json`/`host/` (`refused_links: []` this run). Network is shared (`share_net: true`)
and the agent legitimately used it for `simready.py search/probe` against `hf-mirror.com`.

Allowed tools that worked under proot (checked before spending money, `preflight.txt`): `claude --version` = 2.1.325,
`simready.py --endpoint https://hf-mirror.com search plate` (pinned revision `21b045a8…`) and `probe` on an asset,
the cache and robot URDF readable by path, `/proc` readable, DNS/TLS to `hf-mirror.com` (200). The agent itself ran
`simready.py search` four times and `probe` on two assets through the venv interpreter (`agent-tool-calls.txt`).

## GPU check (physics + reachability, same host)

The exact `next_step` from `scene-authoring-report.json`, run with the IsaacLab venv interpreter after
`source /home/isaac/isaaclab30_env.sh`, `setsid nohup` (`check.sh`, log in `check.log`, work `prepared/78c0d8dceaa3310a`):

```
/home/isaac/IsaacLab30/.venv/bin/python tools/scene_assemble.py check \
  --spec …/runs/1b-proot-20261117-233440/scene-layout.json \
  --work …/runs/1b-proot-20261117-233440/prepared/78c0d8dceaa3310a --headless
```

| Metric | Measured (this layout) | Tolerance | Hand-written baseline (context only) |
| --- | --- | --- | --- |
| settle drift | **1.057 mm** (run 1: 1.040 mm), max end speed 0.001139 m/s | 5 mm / 0.01 m/s | 1.04 mm |
| drawer joint drift | **0.0409 rad** (0.35 → 0.309, the drawer slides in) | 0.005 rad | — |
| max penetration | **61.922 mm** (`object.cabinet` part_01 vs table `sm_table_metal_stainless_a03_obj_00`) | 3 mm | 47 µm |
| probe `plate_in_drawer` | rest z **0.759335 m**, touching `object.cabinet`, end speed 0.000584 m/s → ok | 0.55–0.85 m | z 0.759 |
| reachability | **2/2** targets, worst 0.23 mm / 7.1e-5 rad | — | 10/10 |
| footprint | ok | — | — |
| exit code | **1** (not accepted: `settle.stable=false`, `penetration.ok=false`) | | |

Run 1 of the same command (evidence in `check-run1.json`; its log was overwritten by run 2) measured drift 1.040 mm,
penetration 0.061922 m and `probes_ok=false`: the probe's end speed was 0.013623 m/s, just over the 0.01 m/s
tolerance, while rest z and contact were the same. Run 2 settled to 0.000584 m/s and passed. So the probe assertion is
flaky run to run; the penetration failure is reproducible and identical in both runs.

The 61.9 mm penetration is the agent's own placement error: it put the cabinet 62 mm into the table, which the
kinematic host gate (plan/footprint, which only checks footprint overlap and IK) cannot see. The joint drift is the
drawer sliding under its authored damping after the initial state — also present in the check's model, not the
sandbox. Neither was tuned; the layout is reported as-is. See `check.json` / `check.log`.

## Remaining risks

- proot is a ptrace path translator, not a hard security boundary; it is the only option here. Masks are verified
  after an `umount` attempt, but a determined guest could still subvert ptrace. `ro_soft` means "read, don't trust
  read-only": cache bytes and the robot package are writable in principle (no such write happened; the host does not
  hash them before/after).
- The agent found the snapshot without being told (`ls` in the workspace showed nothing, then it listed
  `../sandbox/repo`). That path is not in the round prompt; a less lucky agent could burn a round locating it.
- `setting_sources: user` with a private HOME means Claude Code starts without host user settings; it worked
  (auth via `ANTHROPIC_BASE_URL` + placeholder token), but per-user settings/plugins are not available.
- The GPU check now fails on this layout (61.9 mm cabinet–table penetration, drawer joint drift), i.e. the kinematic
  host gate (conformance + plan) accepted a layout that is not physically sound. That is a gate-coverage gap, not a
  sandbox issue; the `check` applies no penetration/joint-drift limits to the agent's own placement before acceptance.
- The snapshot is not reproducible from the commit alone: it needs the uncommitted `src/datafarm/adapters/sandbox/`
  package, recorded above.

