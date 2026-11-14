# Robot API 自动识别与绑定

实现日期：2026-11-14。范围：URDF 运动链、模型 TCP/夹爪语义、关节名称映射、能力声明、Isaac Lab 3.0 状态读取和位置目标暂存。支持单个环境，固定使用既有 Lab 3.0 / Newton 配置；不执行被取消的后端选择探针。

## 使用顺序

1. 从来源绑定的 URDF 字节发现 link/关节拓扑、origin、axis、类型、硬限位、mimic 与声明的碰撞几何；无需加载 GPU、语言模型或 Isaac Lab。
2. 优先读取命名 TCP/end frame；对两指相向平移机构，结合碰撞几何和完整 mimic 仿射关系推导开度。显式 RobotProfile 可选择候选、补充控制组或用有来源的配置覆盖模型定义。
3. 对已初始化的 Isaac Lab articulation 自动读取实际 joint_names/body_names，按名称建立索引，核对源关节集合和位置限位。
   位置目标下发另按实际运行时 stiffness 检查有效位置驱动；零增益或未知驱动不能仅凭独立关节身份获得控制权限。
4. 以位置控制为默认模式，从 URDF 传播 mimic 从关节的硬限位，生成 binding 与 capabilities。读取运行时增益和力矩限位，不修改这些参数；无有效位置驱动时拒绝下发。任何新运行应重新生成 binding 并保存。

URDF 提供结构与模型定义，运行实例提供实际顺序。仅有裸 USD 时仍需补齐对应 URDF，未实现通用 USD 反向解析。导入器合并固定 frame 后，沿 URDF 固定关节链组合位姿到仍存在的 runtime body；不跨越可动关节替换坐标系。

描述版本为 `robot-descriptor/2`，绑定版本为 `robot-binding/2`，profile 版本为 `robot-profile/2`；旧 descriptor/profile 版本明确拒绝，需重新生成或审核后显式迁移，不能直接沿用旧绑定摘要。新增的模型选择字段见下表。原 profile 完整保留在 `profile`，实际采用的定义位于 `semantics`，各自记录 `selection_source`；`semantic_diagnostics` 记录未启用的原因。

### 模型语义的证据范围

- 只自动识别名称符合 `tcp`、`tcp_link`、`tool0`、`tool_frame` 或 `end_link`（可带前缀）的 frame。单一候选可直接使用，标记 `named_model_frame`；这不是实测接触中心的证明。多个候选不自动猜左右臂。
- 开度推导要求两根同父 link、相向且平行的移动指关节，共用独立主关节。由关节 origin/axis、所有 mimic 比例/offset、主从限位和两指碰撞包络推导 `q = joint_offset + joint_per_m * width`。支持 box 和 ASCII/binary STL 网格、mesh scale 与 collision origin；非线性、旋转夹爪、缺网格或不支持形状均保留诊断并关闭自动夹爪能力。
- 几何开度标记 `collision_envelope_gap` / `model_geometry_only`；不宣称真实指垫标定或接触。网格由显式 resolver 读取，保存其内容哈希；不依赖语言模型给出尺寸。默认仅允许 URDF 目录内的相对路径，跨目录需显式 `asset_root`，`package://` 由 Python resolver 的 `packages` 显式映射。
- `observe().gripper.width_m` 由实测的两个指关节位置与几何映射计算，允许与主关节目标有偏差；显式 profile 映射则从实测主关节计算。不把目标开度当实测值，不裁剪越界读数。
- 自碰撞几何来自 URDF，但实际自碰撞检测、场景碰撞与不可达诊断仍须检测器/IK/规划器。本次不宣称这些能力已经启用。

## 无仿真的命令行

在仓库根目录执行；输入路径由当前部署提供，不进入可移植绑定记录：

```powershell
python tools/discover_robot.py '<robot.urdf>' --output runs/my-task/robot-binding.json
python tools/discover_robot.py '<robot.urdf>' --profile '<profile.json>' --output runs/my-task/robot-binding.json
python tools/discover_robot.py '<robot.urdf>' --asset-root '<asset-package>' --output runs/my-task/robot-binding.json
```

静态结果中 `runtime_bound=false`，`verification=source_structure_and_model_semantics_only`；不代表 USD 依赖、物理响应或相机通过验收。实际持久化到流水线时使用现有 ArtifactStore/ArtifactRef 绑定 URDF 与整个 USD 依赖包；本工具不代替完整 Episode 准入。

## Python 接入

```python
from datafarm.simulation.robot_binding import discover_urdf
from datafarm.simulation.isaac_robot import IsaacRobotAPI
from datafarm.simulation.robot_semantics import file_mesh_resolver

# source_bytes 由部署 resolver 从来源引用取得。
descriptor = discover_urdf(source_bytes, mesh_resolver)  # resolver(uri) -> 精确网格字节
# robot 是由宿主初始化的 articulation；profile 是显式配置或 None。
api = IsaacRobotAPI(robot, descriptor, profile)
binding = api.binding
state = api.observe()                  # 不推进物理
caps = api.capabilities()
result = api.set_joint_targets({joint_name: target})  # status='staged'
# 宿主调用 robot.write_data_to_sim()、sim.step()、robot.update(dt)。
```

`set_joint_targets` 验证非空命名目标、有限数、URDF 有效位置限位（含 mimic 传播），拒绝未知关节、固定关节与 mimic 从关节，然后按当前 runtime index 下发。它只暂存目标，不执行插值、速度控制、停止/复位或步进，也不宣称已经到达目标。宿主仍需实现接口设计中的限速、预算、记录与运行时速度监测。

模型或 profile 均无法提供无歧义定义时，对应 `tcp_pose` / `set_gripper` 为 false。`move_tcp` 当前始终不支持，等待 IK/运动规划器接入。现有本机 Lab 3.0 源码已包含 DifferentialIKController（含 dls/adaptive_dls 等方法）与 PinkIKController，无需自行重写逆解算法；仍须适配运动链、Jacobian/TCP 偏置、有效限位及碰撞检查。接触为 unavailable，相机为 not_discovered，不把不存在的观测填成零。

## RobotProfile 格式

`schema_version` 必须是 `robot-profile/2`。以下是字段说明，具体数值由任务配置产出；本仓库不内置未经确认的 AIRBOT TCP 或夹爪标定。

| 字段 | 内容 |
|---|---|
| `source_sha256` | 可选，绑定精确 URDF 字节摘要；提供时必须匹配 |
| `groups` | 组名到命名关节列表；不能重复、跨组重叠或包含被动关节 |
| `control_mode` | 可选，默认 `position`；当前拒绝其他模式 |
| `model_tcp` | 可选，选择模型中一个 TCP 候选的 link 名，如双臂的左/右 end_link |
| `model_gripper` | 可选，选择模型中一个夹爪候选的 master_joint 名 |
| `tcp.link` | 显式覆盖；源模型 link，可经固定链解析到 runtime body |
| `tcp.position_m` / `tcp.quaternion_xyzw` | link → TCP 固定变换；XYZW 必须归一化 |
| `tcp.calibration_ref` | ArtifactRef，含 kind、64 位小写 sha256、media_type；实际内容须由部署 resolver 验证 |
| `gripper.master_joint` | 独立可控主关节 |
| `gripper.width_range_m` | 有效两指开度范围 [min,max] |
| `gripper.joint_per_m` / `joint_offset` | 显式仿射映射 q = offset + scale × width；支持负 scale；不覆盖非线性机构 |
| `gripper.calibration_ref` | 映射依据的 ArtifactRef；校验引用结构不代表已经检验真实标定内容 |

可只提供分组，其余定义从模型读取；显式 `tcp` / `gripper` 优先于自动候选，并记录为 `explicit_profile`。覆盖定义仍须提供 calibration_ref，不改写模型文件或把模型推导改标为 measured。双臂可用 `{"schema_version":"robot-profile/2","model_tcp":"left_end_link","model_gripper":"left_g2_joint"}` 明确选择左侧。

## A 线场景碰撞

沿用现有 scene_ref / asset_bundle_ref、entities / frames 和 collision_groups，见 [已有碰撞信息交接](b-control-interfaces.md#31-沿用已有接口提供碰撞信息)。不新增 A 线限位 payload。资产包提供碰撞形状，场景提供物品位姿/铰接初态，运行时读取当前状态；B 的碰撞检测/规划据此判断姿态相关的场景限制。当前接口定义能承载这些信息，但实际资产完整性和场景避障仍需加载验证与检测器接入。

## 运行集成检查

在已有独立 Isaac Lab 3.0 环境中运行（runtime root 为提供 robot_profiles.py 的部署目录）：

```powershell
& '<runtime-root>/run.ps1' python tools/check_isaac_robot_binding.py `
  --runtime-root '<runtime-root>' --asset airbot_single `
  --output runs/my-task/live-binding.json
```

`--asset` 也支持部署已有的 airbot_dual、dualxarm；这些是检查脚本的部署资产选择，不是 API 的机器人注册表。脚本绑定实际顺序、读取状态、暂存当前位姿的命名目标并推进一步，检查 mimic 从关节拒绝下发。可选 `--profile`、`--asset-root`。`--exercise-model-semantics` 额外在自由空间测试三档开度：测试宿主按主关节 URDF 速度上限的 50% 逐步增加位置目标，再保持并读取实测指关节；同时将 TCP 与从原始 URDF 独立计算的正运动学比较。这个测试 ramp 不是生产级规划器；双臂需先选择一侧。该脚本不评测抓放、TCP 规划、接触或视觉质量。

CPU 检查：`PYTHONPATH=src;tests`（Windows 环境变量值）下运行 `python -m unittest test_robot_binding test_robot_model_semantics -v`。覆盖关节顺序、固定链位姿组合、歧义选择、网格哈希和路径边界、mimic 比例/offset/限位传播、实测指关节开度、非法目标、版本/来源不符、缺失驱动与非有限状态。

为避免独立结构读取触发上游 POSIX-only 的 fcntl 导入，simulation 包改为按名称延迟导出；已有公开导出名称保持不变。这不改变原 broker 在 Windows 上的运行限制。
