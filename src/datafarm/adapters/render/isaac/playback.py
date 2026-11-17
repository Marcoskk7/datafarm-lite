"""在 Isaac Lab 3.0 场景里回放 `state_trace` 并用 RTX 相机出图。只能在 `launch_simulation` 上下文内调用。

回放只写状态、不步进物理：每帧写入机器人关节 q（补全 mimic）、`entities` 根位姿和 `articulations` 关节，
`sim.forward()` 刷新运动学后只做渲染更新，再从仿真读回同一批量与 `state_trace` 比较。

实测（lab30，Isaac Sim 6.2 / Lab 3.0）得出的约束：
- Isaac RTX 渲染器按 `/World/envs/env_i` 做场景分区，相机必须放在 `env_0` 下，否则只看得见 env 外的几何。
- `sim.forward()` 之后 Fabric 里的刚体与关节变换已更新，不需要 `sim.step()`；RTX 有时域累积，
  每帧要多调几次 `sim.render()` 才收敛，外观（材质/灯光）刚改完时需要更长的预热。
- 相机建好后第一次 `set_world_poses` 在首次渲染前写入会丢失（第一帧停在原点），所以 `after_reset` 先渲染一次。
- 默认近裁剪 1 cm 会让腕部相机看到自身支架/镜头罩，这里取 4 cm。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from datafarm.adapters.sim.isaac_scene import PRIM_ROOT, entity, prim_name
from datafarm.ir.common import check
from datafarm.render import plan

LIGHT_ROOT = "/World/RenderLights"
LOOKS_ROOT = "/World/RenderLooks"
NEAR_CLIP_M = 0.04
FAR_CLIP_M = 1.0e6


def _camera_path(view_id: str) -> str:
    return f"{PRIM_ROOT}/RenderCam{prim_name(view_id)}"


class Playback:
    """一个已加载的场景：相机、外观和逐帧状态写入。`scene_graph` 节点 id 与 Isaac 场景实体一一对应。"""

    def __init__(self, sim, scene, *, scene_graph: Mapping[str, Any], spec: Mapping[str, Any],
                 descriptor: Mapping[str, Any]):
        self.sim, self.scene, self.graph, self.spec = sim, scene, scene_graph, spec
        self.descriptor = descriptor
        self.robot_id = next(n["node_id"] for n in scene_graph["nodes"] if n["semantic_tag"] == "robot")
        self.cameras: dict[str, Any] = {}
        self._bound: list[str] = []

    # 相机必须在 sim.reset() 之前创建
    def add_cameras(self) -> None:
        import isaaclab.sim as sim_utils
        from isaaclab.sensors import Camera, CameraCfg
        from isaaclab_physx.renderers import IsaacRtxRendererCfg

        for view in self.spec["views"]:
            k = plan.view_intrinsics(self.spec, self.graph, view["view_id"])
            width, height = int(k["width"]), int(k["height"])
            matrix = [k["fx"], 0.0, k["cx"], 0.0, k["fy"], k["cy"], 0.0, 0.0, 1.0]
            self.cameras[view["view_id"]] = Camera(CameraCfg(
                prim_path=_camera_path(view["view_id"]), height=height, width=width, data_types=["rgb"],
                update_period=0, renderer_cfg=IsaacRtxRendererCfg(),
                spawn=sim_utils.PinholeCameraCfg.from_intrinsic_matrix(
                    matrix, width=width, height=height, clipping_range=(NEAR_CLIP_M, FAR_CLIP_M))))

    def after_reset(self) -> None:
        robot = entity(self.scene, self.robot_id)
        self.robot_joints = list(robot.joint_names)
        self.robot_bodies = list(robot.body_names)
        self.mounts: dict[str, tuple[int, np.ndarray]] = {}
        cameras = {c["camera_id"]: c for c in self.graph["cameras"]}
        for view in self.spec["views"]:
            if view["kind"] != "physical" or cameras[view["camera_id"]].get("link") is None:
                continue
            camera = cameras[view["camera_id"]]
            body, link_offset = plan.link_mount(self.descriptor, camera["link"], self.robot_bodies)
            self.mounts[view["view_id"]] = (self.robot_bodies.index(body), link_offset @ plan.optical_offset(camera))
        # 非机器人铰接体：关节名 -> (节点, 关节下标)，名字必须全场唯一
        self.joint_owner: dict[str, tuple[str, int]] = {}
        for node in self.graph["nodes"]:
            if node["semantic_tag"] == "robot":
                continue
            handle = entity(self.scene, node["node_id"])
            for index, name in enumerate(getattr(handle, "joint_names", None) or []):
                check(name not in self.joint_owner, f"joint name {name!r} is not unique across the scene")
                self.joint_owner[name] = (node["node_id"], index)
        self.sim.render()
        for camera in self.cameras.values():
            camera.update(0.0, force_recompute=True)

    # ---- 外观 ----
    def apply_appearance(self, material_set: Sequence[str], light_set: Sequence[str]) -> None:
        import isaaclab.sim as sim_utils
        import omni.usd
        from pxr import UsdShade

        stage = omni.usd.get_context().get_stage()
        for path in self._bound:
            UsdShade.MaterialBindingAPI(stage.GetPrimAtPath(path)).UnbindDirectBinding()
        self._bound = []
        for root in (LIGHT_ROOT, LOOKS_ROOT):
            if stage.GetPrimAtPath(root):
                stage.RemovePrim(root)

        materials = {m["material_id"]: m for m in self.spec["materials"]}
        for material_id in material_set:
            material = materials[material_id]
            params = material["params"]
            look = f"{LOOKS_ROOT}/{prim_name(material_id)}"
            sim_utils.spawn_preview_surface(look, sim_utils.PreviewSurfaceCfg(
                diffuse_color=tuple(params["base_color"]), roughness=params["roughness"],
                metallic=params["metallic"]))
            for node in self.graph["nodes"]:
                if node["semantic_tag"] == material["slot"]:
                    path = f"{PRIM_ROOT}/{prim_name(node['node_id'])}"
                    sim_utils.bind_visual_material(path, look, stage=stage, stronger_than_descendants=True)
                    self._bound.append(path)

        lights = {light["light_id"]: light for light in self.spec["lights"]}
        for light_id in light_set:
            light = lights[light_id]
            common = {"intensity": light["intensity"], "enable_color_temperature": True,
                      "color_temperature": float(light["color_temperature"])}
            path = f"{LIGHT_ROOT}/{prim_name(light_id)}"
            if light["type"] == "dome":
                sim_utils.spawn_light(path, sim_utils.DomeLightCfg(**common))
                continue
            cfg = (sim_utils.DistantLightCfg(**common) if light["type"] == "distant"
                   else sim_utils.SphereLightCfg(radius=light["radius_m"], **common))
            sim_utils.spawn_light(path, cfg, translation=tuple(light["pose"]["position_m"]),
                                  orientation=tuple(light["pose"]["quaternion_xyzw"]))

    # ---- 状态 ----
    def write_state(self, state: Mapping[str, Any], joint_names: Sequence[str]) -> None:
        import torch

        robot = entity(self.scene, self.robot_id)
        values = plan.full_joint_positions(self.descriptor["joints"], joint_names, state["q"],
                                           simulated=self.robot_joints)
        position = robot.data.joint_pos.torch.clone()
        position[0] = torch.tensor([values[n] for n in self.robot_joints], device=position.device)
        robot.write_joint_position_to_sim_index(position=position)
        robot.write_joint_velocity_to_sim_index(velocity=torch.zeros_like(position))

        for node_id, entry in (state.get("entities") or {}).items():
            check(entry["pose"]["frame_id"] == "world", f"state_trace entity {node_id} pose must be in world frame")
            handle = entity(self.scene, node_id)
            pose = torch.tensor([list(entry["pose"]["position_m"]) + list(entry["pose"]["quaternion_xyzw"])],
                                dtype=torch.float32, device=position.device)
            handle.write_root_pose_to_sim_index(root_pose=pose)
            handle.write_root_velocity_to_sim_index(root_velocity=torch.zeros((1, 6), device=position.device))

        by_node: dict[str, dict[int, float]] = {}
        for name, value in (state.get("articulations") or {}).items():
            check(name in self.joint_owner, f"state_trace articulation joint {name!r} is not in the scene")
            node_id, index = self.joint_owner[name]
            by_node.setdefault(node_id, {})[index] = float(value)
        for node_id, joints in by_node.items():
            handle = entity(self.scene, node_id)
            target = handle.data.joint_pos.torch.clone()
            for index, value in joints.items():
                target[0, index] = value
            handle.write_joint_position_to_sim_index(position=target)
            handle.write_joint_velocity_to_sim_index(velocity=torch.zeros_like(target))

        self.scene.write_data_to_sim()
        self.sim.forward()
        self.scene.update(0.0)
        self._expected = (values, state)

    def readback_error(self) -> tuple[float, float]:
        """返回 (最大实体位置误差 m, 最大关节误差)：仿真读回值对写入值，覆盖机器人、实体和铰接关节。"""
        values, state = self._expected
        robot = entity(self.scene, self.robot_id)
        got = robot.data.joint_pos.torch[0].tolist()
        joint_error = max(abs(got[i] - values[n]) for i, n in enumerate(self.robot_joints))
        position_error = 0.0
        for node_id, entry in (state.get("entities") or {}).items():
            root = entity(self.scene, node_id).data.root_pose_w.torch[0, :3].tolist()
            position_error = max(position_error, float(np.linalg.norm(np.subtract(root, entry["pose"]["position_m"]))))
        for name, value in (state.get("articulations") or {}).items():
            node_id, index = self.joint_owner[name]
            joint_error = max(joint_error, abs(float(entity(self.scene, node_id).data.joint_pos.torch[0, index]) - value))
        return position_error, joint_error

    # ---- 相机 ----
    def place_cameras(self) -> dict[str, np.ndarray]:
        """按当前状态摆所有相机，返回期望的世界系 OpenCV 光学位姿。"""
        import torch

        robot = entity(self.scene, self.robot_id)
        cameras = {c["camera_id"]: c for c in self.graph["cameras"]}
        targets = {}
        for view in self.spec["views"]:
            view_id = view["view_id"]
            if view["kind"] == "virtual":
                world = plan.pose_matrix(view["pose"])
            elif view_id in self.mounts:
                index, offset = self.mounts[view_id]
                body = robot.data.body_link_pose_w.torch[0, index].tolist()
                world = plan.pose_matrix({"position_m": body[:3], "quaternion_xyzw": body[3:]}) @ offset
            else:
                world = plan.static_camera_world(self.graph, cameras[view["camera_id"]])
            pose = plan.matrix_pose(world)
            device = self.sim.device
            self.cameras[view_id].set_world_poses(torch.tensor([pose["position_m"]], device=device),
                                                  torch.tensor([pose["quaternion_xyzw"]], device=device),
                                                  convention="ros")
            targets[view_id] = world
        return targets

    def render(self, passes: int) -> None:
        for _ in range(passes):
            self.sim.render()

    def capture(self, view_id: str) -> tuple[np.ndarray | None, np.ndarray]:
        """返回 (RGB uint8 HxWx3 或 None, 仿真读回的相机世界系 OpenCV 位姿)。"""
        camera = self.cameras[view_id]
        camera.update(0.0, force_recompute=True)
        position = camera.data.pos_w.torch[0].tolist()
        quaternion = camera.data.quat_w_ros.torch[0].tolist()
        actual = plan.pose_matrix({"position_m": position, "quaternion_xyzw": quaternion})
        rgb = camera.data.output.get("rgb")
        if rgb is None:
            return None, actual
        image = rgb.torch[0].cpu().numpy()[..., :3]
        if image.size == 0 or not np.isfinite(image).all():
            return None, actual
        return np.ascontiguousarray(image.astype(np.uint8)), actual
