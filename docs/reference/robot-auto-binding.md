# Robot API 自动识别与绑定

实现日期：2026-11-14。范围：结构发现、语义配置合并、关节名称映射、能力声明、Isaac Lab 3.0 状态读取和目标暂存。支持单个环境，固定使用既有 Lab 3.0 / Newton 配置；不执行被取消的后端选择探针。

## 使用顺序

1. 从来源绑定的 URDF 字节发现 link/关节拓扑、类型、限位、mimic 主从；无需加载 GPU、模型或 Isaac Lab。
2. 合并可选 RobotProfile：命名控制分组、TCP 固定变换及其标定引用、夹爪开度映射及其标定引用。
3. 对已初始化的 Isaac Lab articulation 自动读取实际 joint_names/body_names，按名称建立索引，核对源关节集合和位置限位。
   位置目标下发另按实际运行时 stiffness 检查有效位置驱动；零增益或未知驱动不能仅凭独立关节身份获得控制权限。
4. 生成带摘要的 binding 和 capabilities。读取当前运行时的增益及力矩限位，不修改这些参数。任何新运行应重新生成 binding 并保存。

URDF 提供类型与主从关系，运行实例提供实际顺序；这里没有把命名猜测当作事实。仅有裸 USD、没有对应 URDF 元数据时，目前需要先补齐来源描述，未实现通用 USD 反向解析。固定关节合并导致 TCP link 消失时拒绝绑定，不自动猜替代坐标系。

## 无仿真的命令行

在仓库根目录执行；输入路径由当前部署提供，不进入可移植绑定记录：

```powershell
python tools/discover_robot.py '<robot.urdf>' --output runs/my-task/robot-binding.json
python tools/discover_robot.py '<robot.urdf>' --profile '<profile.json>' --output runs/my-task/robot-binding.json
```

静态结果中 `runtime_bound=false`，`verification=source_structure_only`；不代表 USD 依赖、物理响应或相机通过验收。实际持久化到流水线时使用现有 ArtifactStore/ArtifactRef 绑定 URDF 与整个 USD 依赖包；本工具不代替完整 Episode 准入。

## Python 接入

```python
from datafarm.simulation.robot_binding import discover_urdf
from datafarm.simulation.isaac_robot import IsaacRobotAPI

# source_bytes 由部署 resolver 从来源引用取得。
descriptor = discover_urdf(source_bytes)
# robot 是由宿主初始化的 articulation；profile 是显式配置或 None。
api = IsaacRobotAPI(robot, descriptor, profile)
binding = api.binding
state = api.observe()                  # 不推进物理
caps = api.capabilities()
result = api.set_joint_targets({joint_name: target})  # status='staged'
# 宿主调用 robot.write_data_to_sim()、sim.step()、robot.update(dt)。
```

`set_joint_targets` 验证非空命名目标、有限数、原始位置限位，拒绝未知关节、固定关节与 mimic 从关节，然后按当前 runtime index 下发。它只暂存目标，不执行插值、速度控制、停止/复位或步进，也不宣称已经到达目标。宿主仍需实现接口设计中的限速、预算、记录与运行时速度监测。

不含 TCP 配置时 `tcp_pose=false`；不含夹爪标定时 `set_gripper=false`，调用返回 `BindingError`。`move_tcp` 当前始终不支持，等待显式 IK/运动规划器接入。接触为 unavailable，相机为 not_discovered，不把不存在的观测填成零。

## RobotProfile 格式

`schema_version` 必须是 `robot-profile/1`。以下是字段说明，具体数值由任务配置产出；本仓库不内置未经确认的 AIRBOT TCP 或夹爪标定。

| 字段 | 内容 |
|---|---|
| `source_sha256` | 可选，绑定精确 URDF 字节摘要；提供时必须匹配 |
| `groups` | 组名到命名关节列表；不能重复、跨组重叠或包含被动关节 |
| `tcp.link` | 实际 runtime body 中存在的 link |
| `tcp.position_m` / `tcp.quaternion_xyzw` | link → TCP 固定变换；XYZW 必须归一化 |
| `tcp.calibration_ref` | ArtifactRef，含 kind、64 位小写 sha256、media_type；实际内容须由部署 resolver 验证 |
| `gripper.master_joint` | 独立可控主关节 |
| `gripper.width_range_m` | 有效两指开度范围 [min,max] |
| `gripper.joint_per_m` / `joint_offset` | 显式仿射映射 q = offset + scale × width；支持负 scale；不覆盖非线性机构 |
| `gripper.calibration_ref` | 映射依据的 ArtifactRef；校验引用结构不代表已经检验真实标定内容 |

可只提供分组而不启用 TCP 或夹爪能力。所有配置均保留原样快照和来源，不通过最后一个 link、名称后缀或自由空间夹爪开合推断标定。

## 运行集成检查

在已有独立 Isaac Lab 3.0 环境中运行（runtime root 为提供 robot_profiles.py 的部署目录）：

```powershell
& '<runtime-root>/run.ps1' python tools/check_isaac_robot_binding.py `
  --runtime-root '<runtime-root>' --asset airbot_single `
  --output runs/my-task/live-binding.json
```

`--asset` 也支持部署已有的 airbot_dual、dualxarm；这些是检查脚本的部署资产选择，不是 API 的机器人注册表。脚本绑定实际关节顺序、读取状态、暂存当前位姿的命名目标并实际推进一步，检查 mimic 从关节拒绝下发。可选 `--profile` 用于用户已准备的标定配置。该脚本不评测抓放、TCP 规划、接触或视觉质量。

CPU 检查：`PYTHONPATH=src;tests`（Windows 环境变量值）下运行 `python -m unittest test_robot_binding -v`。覆盖关节顺序变化、单位、主从拒绝控制、非法/超限目标、循环拓扑与 mimic、过期 profile、配置篡改、缺失能力、运行限位不符、多环境拒绝和非有限状态。

为避免独立结构读取触发上游 POSIX-only 的 fcntl 导入，simulation 包改为按名称延迟导出；已有公开导出名称保持不变。这不改变原 broker 在 Windows 上的运行限制。
