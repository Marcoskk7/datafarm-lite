"""相对于当前观测的 Piper EEF chunk（米 / 环绕的 Euler 弧度）。

夹爪保持绝对值。一个 chunk 中的每个目标都使用同一个观测
锚点，绝不使用相邻目标或累加和。这消除了常量
坐标偏移；但它不标定依赖位姿的运动学误差。
"""
from dataclasses import dataclass, replace
import numpy as np

MODE = "delta_eef_current_rpy_v1"
SUFFIX = "_delta_eef_current_rpy_v1"
POSE = (0, 1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12)
ANGLES = (3, 4, 5, 10, 11, 12)


def wrap(x):
    return (x + np.pi) % (2 * np.pi) - np.pi


def convert(actions, state, *, inverse=False):
    actions = np.asarray(actions)
    state = np.asarray(state)
    if state.shape[-1] != 14 or actions.shape[-1] < 14:
        raise ValueError("delta EEF requires a measured 14D state and >=14D actions")
    if actions.ndim != state.ndim + 1 or actions.shape[:-2] != state.shape[:-1]:
        raise ValueError("one current observation anchor is required per action chunk")
    if not np.isfinite(state).all() or not np.isfinite(actions).all():
        raise ValueError("non-finite EEF state/action")
    out = np.array(actions, dtype=np.float32, copy=True)
    anchor = np.expand_dims(state, -2)
    out[..., POSE] += (1 if inverse else -1) * anchor[..., POSE]
    out[..., ANGLES] = wrap(out[..., ANGLES])
    return out


@dataclass(frozen=True)
class DeltaEef:
    inverse: bool = False

    def __call__(self, data):
        # 即使是不依赖状态的模型，重建时也需要新鲜的实测状态。
        state = np.asarray(data["state"])
        if self.inverse and state.shape[-1] == 32:
            state = state[..., :14]
        if state.shape[-1] != 14 or not np.isfinite(state).all():
            raise ValueError("delta EEF requires finite measured 14D state")
        if "actions" not in data:
            return data
        return {**data, "actions": convert(data["actions"], state, inverse=self.inverse)}


def install():
    """注册新的 config，同时不改动只读的 trainer 检出目录。"""
    from openpi.training import config as cfg
    from openpi import transforms

    @dataclass(frozen=True)
    class DeltaEefDataConfig(cfg.LeRobotPiperEefXyz3dDataConfig):
        def create(self, assets_dirs, model_config):
            data = super().create(assets_dirs, model_config)
            group = data.data_transforms
            return replace(data, data_transforms=transforms.Group(
                inputs=[*group.inputs, DeltaEef()],
                outputs=[DeltaEef(inverse=True), *group.outputs],
            ))

    for name, base in list(cfg._CONFIGS_DICT.items()):
        if name.endswith(SUFFIX) or not isinstance(base.data, cfg.LeRobotPiperEefXyz3dDataConfig):
            continue
        fields = {field.name: getattr(base.data, field.name)
                  for field in __import__('dataclasses').fields(base.data)}
        cfg._CONFIGS_DICT[name + SUFFIX] = replace(
            base, name=name + SUFFIX, data=DeltaEefDataConfig(**fields),
            policy_metadata={**(base.policy_metadata or {}), "action_representation": MODE,
                             "server_output": "absolute_eef", "anchor": "request_state"})
