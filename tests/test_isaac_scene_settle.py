"""settle() 的判定规则，用假的 scene/sim（不需要 Isaac）。"""
from __future__ import annotations

import numpy as np

from datafarm.adapters.sim import isaac_scene


class _Tensor:
    def __init__(self, rows):
        self.rows = np.asarray(rows, dtype=float)

    @property
    def torch(self):
        return self

    def __getitem__(self, index):
        return self.rows[index]

    def tolist(self):
        return self.rows.tolist()

    def clone(self):
        return _Tensor(self.rows.copy())


class _Data:
    def __init__(self, joints, pos=(0.0, 0.0, 0.0)):
        self.joint_pos = _Tensor([joints])
        self.root_pos_w = _Tensor([pos])
        self.root_lin_vel_w = _Tensor([(0.0, 0.0, 0.0)])


class _Handle:
    def __init__(self, names, joints, pos=(0.0, 0.0, 0.0)):
        self.joint_names = names
        self.data = _Data(joints, pos)

    def set_joint_position_target_index(self, target):
        self.target = target


class _Sim:
    def get_physics_dt(self):
        return 1.0 / 120.0


def _settle(monkeypatch, robot_after, drawer_after):
    robot = _Handle(["left_joint1", "left_joint3"], [0.0, 0.0])
    cabinet = _Handle(["slide"], [0.4], pos=(1.0, 0.0, 0.0))
    scene = {"robot__airbot": robot, "object__cabinet": cabinet}

    def step(sim, scene_, steps):
        robot.data.joint_pos = _Tensor([robot_after])
        cabinet.data.joint_pos = _Tensor([drawer_after])

    monkeypatch.setattr(isaac_scene, "contacts", lambda node_ids: [])
    monkeypatch.setattr(isaac_scene, "_step", step)
    nodes = [{"node_id": "object.cabinet", "asset_id": "cab", "joint_state": {"slide": 0.4}}]
    return isaac_scene.settle(
        _Sim(), scene, nodes=nodes, assets={"cab": {"body": "articulation"}}, robot_node_id="robot.airbot",
        seconds=2.0, drift_tolerance_m=0.005, speed_tolerance_mps=0.01, joint_tolerance_active=0.005,
        joint_tolerance_passive=0.005, penetration_tolerance_m=0.003, joint_limits={"object.cabinet": {"slide": (0.0, 0.5)}},
        active_joints={"object.cabinet": ["slide"]})[0]


def test_robot_gravity_sag_is_recorded_not_gated(monkeypatch):
    """lab30 实测：AIRBOT 位置驱动在静置 2 s 后 left_joint3 下垂 0.0127 rad；这是驱动增益，不是布局问题。"""
    report = _settle(monkeypatch, robot_after=[0.0, -0.012745], drawer_after=[0.4])
    assert report["stable"]
    assert report["robot_joint_drift_rad"] == 0.012745
    assert report["measured_joint_state"]["robot.airbot"]["left_joint3"] == -0.012745
    assert report["max_joint_drift"] == 0.0


def test_listed_active_fixture_joint_still_gated(monkeypatch):
    report = _settle(monkeypatch, robot_after=[0.0, 0.0], drawer_after=[0.41])
    assert not report["stable"]
    check = report["nodes"]["object.cabinet"]["joint_checks"][0]
    assert check["actuated"] and not check["ok"]
