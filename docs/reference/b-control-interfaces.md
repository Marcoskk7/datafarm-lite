# B 线接口设计：Isaac Lab 3.0 控制、判定与 Episode

版本：`b-control/0.1.0-draft`。日期：2026-11-14（Asia/Shanghai）。状态：可评审的设计稿；A/B 交接字段保持原有定义，补充当前实现说明。

实现进展：URDF 结构/运动链、模型 TCP 与夹爪几何映射、profile 覆盖、Isaac Lab 实例绑定、状态读取与位置目标暂存已实现，见 [自动绑定说明](robot-auto-binding.md)。场景碰撞判定与 TCP IK/规划执行仍待接入；其他模块的设计不因本文更新而自动视为通过验收。

本文落实用户确定的路线：以 `translate-view` 为基线，仿真框架固定 Isaac Lab 3.0，复用已有 Newton + MJWarp 配置；取消独立后端选择探针、10 次抓放前置门槛和 SAPIEN 回退路径。设计不新增付费调用、远程运行或训练任务。

## 1. 范围与责任

第一阶段支持单环境、一个 AIRBOT + G2 的任务执行；标识和字段保留双臂扩展能力。实际任务逻辑放入运行目录中的任务 adapter，不建立固定任务注册表。三相机由场景与 RenderSpec 声明，相机未实现时允许状态采集，但不能签收三视角数据。

| 模块 | 负责人 | 输入 | 输出与边界 |
|---|---|---|---|
| 资产与场景 | A | AssetRecord、SceneGraph | 来源绑定的资产、实体、关节、坐标系、碰撞与相机定义 |
| 场景绑定 | B，与 A 联调 | 场景引用、运行 profile | 实体和关节名称映射、运行能力声明；启动不匹配时明确失败 |
| Robot API | B | 受限动作调用 | 实测状态、动作结果、控制输入；模型不能访问底层仿真对象 |
| TaskContract / Judge | B，独立评审 | 任务要求、实测状态与事件 | 冻结判据、结果、负向对照；模型不能改判据 |
| CaP adapter | B | 公共任务说明、公开反馈 | 专家候选与原语调用；保留现有 broker 边界 |
| Episode recorder | B | 同一次运行的状态、动作、接触 | 完整轨迹、帧映射、终止结果和来源摘要 |
| 状态回放 / 渲染 | A | Episode、RenderSpec | 相机图像、材质/灯光变体；只读取记录的运动 |
| 动作重放 / 导出 | B | 初始状态、实际控制输入 | 重放误差报告、HDF5；与渲染结果共同做准入 |

机器人、仿真推进和 recorder 由一个宿主串行持有。Judge 在宿主或独立评估进程中执行；独立审核读取冻结证据。接口隔离本身不构成操作系统安全隔离，Windows 的现有 POSIX broker 限制需要在实现阶段处理。

## 2. 通用数据约定

### 2.1 标识、版本与来源

- 逻辑标识采用稳定字符串，例如 `robot.airbot`、`object.plate`、`joint.drawer.slide`、`camera.head`。B 不依赖 USD 层级或导入后的数组顺序。
- 每类新契约有显式 `schema_version`；不兼容版本拒绝加载，版本升级须有显式迁移。
- 持久化资产、场景、控制配置、Judge、专家源码和 Episode 使用现有 `ArtifactRef {kind, sha256, media_type}`。运行时通过部署 resolver 解析本机文件位置。
- USD 文件和其完整依赖集、关节映射、TCP 标定、求解器及驱动参数都属于运行绑定。仅记录入口 USD 的哈希不够。
- `attempt_id` 标识一次尝试，`episode_id` 标识一次实际轨迹；失败重试产生新 ID，并用 `retry_of` 关联。不能覆盖失败记录。
- `created_at` 使用 UTC RFC3339，例如 `2026-11-14T01:00:00Z`；它不参与仿真同步。用户报告显示 Asia/Shanghai 时间。

### 2.2 单位与位姿

| 数据 | 对外约定 |
|---|---|
| 长度 / 角度 / 时间 / 质量 | m / rad / s / kg |
| 力 / 力矩 / 冲量 | N / N·m / N·s，字段名明确量纲 |
| 坐标 | 右手系；场景以 Z 为上；单位转换由导入适配层负责 |
| 位姿 | `Pose {frame_id, position_m:[x,y,z], quaternion_xyzw:[x,y,z,w]}` |
| 变换语义 | Pose 表示目标实体局部坐标到 `frame_id` 的变换，即 `T_frame_entity` |
| 四元数 | 单位四元数，序列 XYZW；比较旋转时识别 q 与 -q 等价 |
| TCP | 指定 `tcp_frame_id`、所属 link 和固定 `T_link_tcp`；禁止临时取 link 原点替代 TCP |

外部旧 WXYZ 在边界转换。USD 原生四元数、MJCF 等格式通过各自 API 解释，不对文件做盲目分量交换。相机单独声明光学坐标约定及其到场景坐标系的变换。

### 2.3 时间与推进权

默认沿用 `robot_profiles.py`：控制频率 120 Hz，一个控制周期调用一次 Newton 仿真步，内部 4 个子步。清楚区分：

- `control_dt_s = 1/120`；`sim_step_dt_s = 1/120`；`solver_substeps = 4`。
- 子步有效间隔为 `1/480` 秒，但当前设计不承诺能读取每个内部子步的状态或接触。
- 状态 `s[k]` 在第 k 个控制周期结束后采样；`s[0]` 是复位、settle 完成后采集的初始状态，采集时钟从零开始。
- `a[k]` 在半开区间 `[k, k+1)` 作用，产生 `s[k+1]`。N 个动作必须对应 N+1 个状态。
- 所有流记录 `episode_id`、`control_step`；有需要时额外记录仿真调用计数。时间由整数步和 dt 推导，避免累计误差。
- `observe()`、Judge 读取、日志和相机导出不推进物理。只有宿主 `step()` 推进；高层原语通过宿主执行循环间接推进。

## 3. A → B：SceneBinding 交接

SceneGraph 是场景权威；SceneBinding 是 B 对已加载场景的名称映射和运行适配，不能私自改资产物理参数。

| 字段 | 类型 | 要求 |
|---|---|---|
| `scene_ref` / `asset_bundle_ref` | ArtifactRef | 场景与完整依赖集 |
| `world_frame_id` / `units` | string / object | 世界坐标系及单位声明 |
| `entities` | entity 列表 | ID、类型、资源引用、初始 Pose；铰接物体另含关节初值 |
| `robots` | robot 列表 | 基座、命名关节及类型、限位、可控集合、末端和夹爪标定 |
| `frames` | frame 列表 | ID、parent ID、固定变换；无循环或悬空 parent |
| `cameras` | camera 列表 | ID、外参/安装 frame、内参、分辨率、畸变、光学轴与采样计划 |
| `collision_groups` | object | 碰撞关系和允许/禁止接触的实体集合 |
| `physics_parameter_ledger` | ArtifactRef | 质量、惯量、摩擦等来源：measured/fitted/assumed/unknown |
| `runtime_profile_ref` | ArtifactRef | 控制器、求解器、dt、驱动与接触配置的不可变快照 |

部署侧可持有 `entity_id → prim path` 与 `joint_name → index` 映射；绝对机器路径不进入可移植 IR。加载时核对实体存在、关节名唯一、类型/限位、TCP、mimic 主从和碰撞配置。

`reset(seed)` 的随机化轴来自 TaskContract；实际抽样结果全部记录。seed 相同不等于保证 GPU 逐位确定性，跨环境版本也不能仅靠 seed 重建场景。

### 3.1 沿用已有接口提供碰撞信息

| 信息 | 权威来源 | B 的处理 |
|---|---|---|
| link、可动 joint、运动链、origin/axis、mimic | URDF | 解析并按名字绑定运行时，不以固定关节编号或手写连杆长度替代 |
| 机器人位置/速度/effort 硬限位 | URDF；运行时须核对 | 场景限制不能扩大机器人允许范围；当前目标入口实施位置限位，速度/effort 的轨迹级约束仍由执行器实现 |
| 机器人自身碰撞几何 | URDF `<collision>` 和依赖网格 | 交给碰撞检测器；URDF 不包含完整的自碰撞禁用矩阵，额外例外需有来源引用，不默认忽略相邻或其他 link 对 |
| 障碍物与物体铰接限位 | A 的现有 scene_ref / asset_bundle_ref、entities / frames | 资产提供碰撞形状与缩放，场景提供初始位姿/铰接状态，执行时从同一仿真读取最新状态 |
| 碰撞关系与允许接触 | A 的现有 collision_groups，任务的现有 constraints | B 根据实体身份和任务阶段检测；不能把所有接触都当作失败 |
| 机器人几何不可达 / 当前场景不可达 | B 的 IK / 规划与碰撞检测计算 | 前者依据 URDF 运动链/限位；后者再结合 A 的场景状态/约束；单次局部 IK 不收敛不能证明全局不可达 |

这些已有字段能够承载 A 线物品导致的碰撞约束，无需新增限位 payload、交接字段或接口版本。机器人运动链、关节硬限位、模型 TCP/夹爪定义由 B 从来源绑定的 URDF/网格解析，A 无需重复手抄。

接口足够不等于实际资产已完整：B 加载时必须确认引用可解析、碰撞几何存在且已启用、单位/缩放一致、物品位姿与当前铰接状态正确。只有 visual mesh 而无 collision shape 时，按已有资产交付约定修正资产内容；不把 collision_groups 或包围盒当作完整几何。场景物品造成的限制依赖整机姿态和物品状态，由 B 的碰撞检测/规划动态判断，不要求 A 预先转换成独立关节区间。当前该检测/规划尚未接入，不宣称完成场景避障验收。

## 4. Robot API

### 4.1 宿主接口：不向专家程序暴露

以下为类型签名设计，不是已实现的 Isaac Lab 包 API。

```python
class RobotRuntime:
    def capabilities(self) -> Capabilities: ...
    def reset(self, reset_request: ResetRequest) -> ResetResult: ...
    def observe(self) -> Observation: ...
    def step(self, command: ControlCommand) -> StepResult: ...
    def stop(self, reason: str) -> StopResult: ...
    def close(self) -> None: ...
```

| 方法 | 语义 |
|---|---|
| `capabilities` | 声明控制模式、可控关节、TCP、夹爪范围、接触量与采样语义、相机可用性；缺失能力不能用假数据补齐 |
| `reset` | 宿主绑定 seed、实际初值、settle 配置；成功后返回 s[0]；失败属于初始化错误并保留尝试记录 |
| `observe` | 返回当前步的只读实测快照，不推进仿真，不修改动作或判据 |
| `step` | 验证一个周期的目标，执行限速/控制器，记录实际施加值，推进一个控制周期，返回后状态与接触 |
| `stop` | 阻止继续接受原语；若后端可用，进入声明的受控停止逻辑；不能承诺无条件瞬停 |
| `close` | 关闭拥有的实例和写入器；不删除任何轨迹 |

`ControlCommand` 初版仅支持 `joint_position_target`；命名关节和目标值长度相等。臂与夹爪主关节属于可控集合，mimic 从关节只观测、不独立下发目标。记录 `requested`、`applied` 与限幅原因；目标限幅不能被误报成精确到达。

默认控制模式固定为 `position`，由 B 下发关节位置目标；底层位置驱动的 stiffness/damping/effort 由运行 profile 提供并保存。默认位置模式不授权 B 猜测增益；没有有效位置驱动时关闭对应动作能力。当前明确拒绝 velocity/effort 模式。

`Observation` 必须包含步号、命名关节 q/dq、TCP Pose、任务物体 Pose/速度、铰接状态、夹爪开度、有效性标记。力矩字段区分驱动目标、仿真实际值和估计值；拿不到实际值时标记 unavailable。

### 4.2 专家接口：受限、同步、有限步数

```python
class RobotPrimitives:
    def observe(self) -> PublicObservation: ...
    def move_tcp(self, robot_id, target_pose,
                 motion_limits, completion) -> PrimitiveResult: ...
    def set_gripper(self, robot_id, width_m,
                    speed_m_s, completion) -> PrimitiveResult: ...
    def wait(self, control_steps) -> PrimitiveResult: ...
```

- 原语同步返回：运动完成、失败或达到步数上限后才返回；“请求被接受”不等于动作成功。不在初版混用异步 handle。
- `completion` 明确 `max_control_steps`、位置/姿态或开度误差、连续满足的 `settle_steps`。阈值由控制配置限定，策略只能在允许范围内选值。
- `move_tcp` 的目标 frame 必须已注册；宿主把目标在动作开始时变换到世界系并冻结，动态追踪目标需将来增加显式模式。
- `motion_limits` 包含 TCP 平移/角速度与命名关节限速。轨迹规划、IK、插值和速度监测由适配层执行。
- `set_gripper` 输入为两指开度（米）；映射优先由模型 mimic 和碰撞几何推导，记录 `width_semantics=collision_envelope_gap`、网格来源和 `verification=model_geometry_only`。它不自动等同于实物夹持面标定；有来源的显式 profile 可覆盖。不能仅把主关节行程当成开度。
- `wait` 保持已声明的控制模式和最后目标，照常记录动力学；不是跳过物理时间。
- 所有原语有调用次数和总步数预算。复位、隐藏种子、Judge、原始文件、仿真对象、任意代码导入不暴露给专家。
- 抓住物体可能导致夹爪未达到期望开度：普通开度原语报告 `blocked`，是否构成有效抓取由任务 Judge 依据证据判定；不使用通用“闭爪即抓住”规则。

### 4.3 返回值与失败

`PrimitiveResult` 包含 `call_id`、`status`、`start_step`、`end_step`、`requested`、`resolved_target`、`before`、`after`、`error`。状态枚举：`completed | rejected | failed | timeout | aborted | blocked`。拒绝前未推进时 start=end；中途失败保留已经推进的全部状态。

| 错误码 | 类别 | 处理 |
|---|---|---|
| `INVALID_ARGUMENT` / `UNKNOWN_FRAME` | 专家调用错误 | 返回公开诊断，不推进 |
| `IK_UNREACHABLE` | 控制失败 | 当前动作失败，返回当前状态；由策略在预算内改变目标 |
| `JOINT_LIMIT` / `SPEED_LIMIT` / `FORBIDDEN_CONTACT` | 执行约束违规 | 终止当前动作，按冻结策略决定是否结束 episode |
| `MOTION_TIMEOUT` | 控制失败 | 保留部分轨迹，不自动重放动作 |
| `GRIPPER_BLOCKED` | 运动受阻 | 返回开度与可用接触证据，由 Judge 判断任务关系 |
| `NONFINITE_STATE` / `SIMULATION_ERROR` / `ASSET_MISMATCH` | 运行错误 | 终止并标为 error，不送成普通任务失败 |
| `CAPABILITY_UNAVAILABLE` | 配置或能力不足 | 明确拒绝；禁止把未知接触写成零 |

宿主恢复通过 `phase=recovery` 另行记录。运行时兜底恢复与策略主动退回分开；不能用兜底恢复冒充策略成功。

## 5. TaskContract 与 Judge

### 5.1 契约字段

| 字段 | 含义 |
|---|---|
| `task_id`、`task_version`、`scene_ref` | 绑定任务和场景 |
| `goal_predicates`、`stage_graph` | 最终关系及必要动作阶段；阶段顺序适用于任务确实要求的动作 |
| `constraints` | 允许接触、关节/速度约束、物体掉落与姿态边界 |
| `thresholds` | 值、单位、来源和冻结版本；未确定值不填伪造测量值 |
| `required_capabilities` | 必需状态/接触证据；缺失时拒绝启动任务评估 |
| `initialization`、`randomization` | 初始范围、settle、失败处理；实际抽样写入 Episode |
| `termination` | 总步数预算、成功保持窗口与提前失败条件 |
| `judge_ref`、`negative_controls_ref` | Judge 源码/依赖与独立评审材料 |
| `public_task` | 可提供给 CaP 的目标、API 和公开约束 |

开发、候选选择、技能复验、隐藏评估、生产采集使用互不重叠的种子分区。隐藏分区由宿主持有，专家不可读取；公开反馈不泄露隐藏初值、种子或内部判据实现。

### 5.2 判定接口与结果

```python
class Judge:
    def initialize(self, frozen_contract, initial_state) -> None: ...
    def update(self, transition) -> JudgeStatus: ...
    def finalize(self, termination_reason) -> JudgeResult: ...
```

Judge 只读状态与事件，无权控制机器人。`JudgeResult` 包含 `outcome=success|failure|error|aborted`、`success`、`terminated`、`truncated`、`reason_code`、`metrics`、`predicate_results`、证据步区间和 `judge_ref`。内部 error/aborted 的 success 为 null；与现有 CaP 布尔结果桥接时，error/aborted 必须走异常/运行错误通道，不能强制转 false。

正常成功或任务失败为 terminated；达到总步数预算为 truncated；基础设施错误或用户取消独立分类。超时任务计入任务评估分母，初始化/基础设施错误单独列出并保留；报告同时展示计划数、有效评估数、错误数和取消数，禁止删除不利尝试。

Judge 在专家搜索前冻结，任何判据、阈值或场景物理配置改变都创建新任务版本。隐藏评估只在程序冻结后运行；结果不能流回修复。独立审核另行重算关键指标。

### 5.3 餐盘任务的判据结构

以下是判据结构，不是已经测定的阈值或冻结任务。抓放入屉与推盘入屉使用不同 task_id。

| 阶段 | 判据 | 证据 |
|---|---|---|
| 提起 | 餐盘实际离开支撑面并达到任务规定高度 | 餐盘几何最低点、世界位姿、支撑关系 |
| 保持 | 持续窗口内不掉落，位移/旋转漂移在阈值内 | 连续状态；接触能力可用时结合两指证据 |
| 放置 | 整盘几何处于目标容器有效空间内 | 变换后的几何边界/体积测试，不能只检查中心点 |
| 释放与稳定 | 指爪释放，盘子由目标表面支撑且稳定 | 开度、相对位姿、接触与持续速度窗口 |
| 完整性 | 无瞬移、无运行违规；任务要求归位时实际归位 | 全轨迹、事件及最终 TCP |

至少准备“中心在抽屉内但盘沿越界”“仍被夹持”“短暂进入后滑出”“被直接写位姿放入”“只推盘却声称提起”等负向对照。物理观测不足时结果为 unknown/error，不能默认谓词通过。

## 6. Episode 与记录规范

### 6.1 与现有 IR 兼容

复用 `core/model.py` 的 `ArtifactDocument(kind=EPISODE)`，其 payload 保留现有必需键：`setup`、`actions`、`state_trace`、`contacts`、`events`、`frame_map`、`replay`。新增契约通过版本化的嵌套记录和引用承载；不改写现有 schema 的含义。以下字段结构是设计要求，完整校验器尚待实现。

| payload 项 | 数据 |
|---|---|
| `setup` | 场景/机器人/profile/契约/Judge/专家引用、引擎版本、seed 分区、实际初始状态与随机化、dt、关节顺序、能力快照 |
| `actions` | 请求与实际施加的低层控制输入、控制模式、作用区间、原语 call_id |
| `state_trace` | N+1 个状态；包含机器人、所有任务实体及渲染所需动态实体 |
| `contacts` | 独立接触流与 availability；采样语义、实体双方、量纲及聚合窗口 |
| `events` | 原语开始/结束、阶段变化、错误、Judge、停止与恢复事件 |
| `frame_map` | 每帧对应的 control_step、camera_id、view_variant_id、内外参引用及图像引用；无相机时明确为空 |
| `replay` | 初始条件、动作重放配置、状态回放配置及误差报告引用；未执行时标记 unverified |

任务完成标志、内容绑定摘要和写入完整性由 recorder finalize 后生成。中断文件标记 partial，保留最后完整步；不得给未完成文件签发完整 Episode。

### 6.2 动作与状态

低层动作记录 `requested_target` 与 `applied_target`，以及控制器的插值/限幅结果；不能只存 `move_tcp` 参数。若以后使用力矩控制，需增加新的控制模式并记录该模式的实际输入。

状态字段包含命名关节 q/dq、夹爪主从状态、TCP、刚体位置/四元数/线角速度、抽屉等关节状态。位置单位随关节类型区分 revolute=rad、prismatic=m，不能把混合关节数组全标成 rad。

接触字段区分 `available`、`unavailable`、`not_requested`。没有接触记录只有在传感/查询有效时才表示“检测到零接触”。Newton 内部子步无法读出时，明确是周期末快照或后端提供的周期聚合；不宣称每子步接触完备。

### 6.3 相机与 A 线回放

相机分为固定相机、安装在 link 上的相机。后者记录安装变换，并由每步刚体状态推导世界外参；不将自由跟随视角命名为真实腕部相机。所有相机共享按整数 control_step 定义的采样计划。

一个物理 episode 可派生多材质/灯光变体，仍只计一条物理轨迹。RenderSpec 指定三个物理视角、材质与灯光变体，frame_map 用 camera_id 和 variant_id 区分。变体不得改变几何、碰撞、动作或接触。

渲染可以跳帧，但必须保留原状态采样索引。不得默认线性插值能复现接触事件；需要插值时显式记录方法与限制。无效帧保留 missing/error 状态，不能复制上一帧伪装同步。

## 7. 两种重放与验收

| 类型 | 输入与操作 | 用途 | 验收边界 |
|---|---|---|---|
| 状态回放 | 加载同一资产，写入记录的刚体/铰接状态；禁用或隔离物理推进 | A 线相机渲染 | 验证实体完整、时间同步、位姿与图像内容；不证明动力学复现 |
| 动作重放 | 恢复完整初始条件、固定版本和配置，重新施加 applied 控制输入 | B 线动力学一致性 | 对齐每个控制步，报告位置/旋转/关节/事件误差 |

将项目的“≤1 cm”落实为：对 TaskContract 声明的任务刚体（含餐盘、目标容器的运动部件），在全部记录步上计算相同实体坐标原点的位置误差，最大值 ≤0.01 m。报告也包含 TCP 最大位置误差；哪些实体参与签收在冻结契约中明确。旋转、关节和接触时序分别报告并另定阈值，1 cm 不能覆盖这些维度。

缺步、未比较实体、不同资产或只比较末帧不能判定通过。GPU 数值不保证逐位重现；超限时保留原始轨迹与重放轨迹、版本和差异，不能修改原动作/状态来过门。

## 8. HDF5 导出约定

每个 episode 一个逻辑组；可多文件存储，但 manifest 明确成员和摘要。

```text
/meta                           版本、来源、命名关节、dt、控制模式、Judge 结果
/state/control_step             [N+1] int64
/state/robots/<id>/q             [N+1,J] float64
/state/robots/<id>/dq            [N+1,J] float64
/state/entities/<id>/pose_xyzw   [N+1,7] float64，顺序 x,y,z,qx,qy,qz,qw
/action/interval_steps          [N,2] int64，逐行为 [k,k+1]
/action/robots/<id>/requested    [N,U] float64
/action/robots/<id>/applied      [N,U] float64
/action/call_id                 [N] UTF-8
/contacts/records               可变长 UTF-8 JSON，字段按本契约；保留时间与 availability
/events/records                 可变长 UTF-8 JSON
/frames/<camera>/<variant>/...  原图或 ArtifactRef，以及 control_step 映射
```

J 是观测关节数，U 是实际控制通道数；分别存 joint_names、joint_types 和 control_names，通常 J≠U。保存原始流与 dtype，不用重采样结果替代原始证据。失败与 recovery 阶段仍可存档，但训练导出通过明确 phase mask 选择，不修改动作时间语义。

## 9. 完整的小型 Episode 示例

下面是两步静止保持的**人工协议示例**，完整表达 N=2、状态数=3 的关系，不代表机器人实测或抓放成功；不是可直接提交给现有 IR 校验器的 ArtifactDocument。实际持久化须把各流存入 ArtifactStore 后组装引用与真实摘要。示例省略真实资产包，仅用于对齐字段与时间。

```json
{
  "schema_version": "b-control-example/0.1.0",
  "synthetic_example": true,
  "episode_id": "example.hold.001",
  "setup": {
    "framework": "isaaclab",
    "release": "3.0.0",
    "physics_backend": "newton_mjwarp",
    "control_dt_s": 0.008333333333333333,
    "solver_substeps": 4,
    "phase": "debug",
    "seed": 0,
    "joint_names": ["left_joint1", "left_g2_joint"],
    "joint_types": ["revolute", "prismatic"],
    "control_names": ["left_joint1", "left_g2_joint"],
    "initialization": "settle_completed",
    "contact_capability": "unavailable"
  },
  "actions": [
    {"call_id": "call.001", "interval_steps": [0, 1], "mode": "joint_position_target", "requested": [0.0, 0.02], "applied": [0.0, 0.02]},
    {"call_id": "call.001", "interval_steps": [1, 2], "mode": "joint_position_target", "requested": [0.0, 0.02], "applied": [0.0, 0.02]}
  ],
  "state_trace": [
    {"control_step": 0, "q": [0.0, 0.02], "dq": [0.0, 0.0], "plate_pose": {"frame_id": "world", "position_m": [0.5, 0.0, 0.75], "quaternion_xyzw": [0.0, 0.0, 0.0, 1.0]}},
    {"control_step": 1, "q": [0.0, 0.02], "dq": [0.0, 0.0], "plate_pose": {"frame_id": "world", "position_m": [0.5, 0.0, 0.75], "quaternion_xyzw": [0.0, 0.0, 0.0, 1.0]}},
    {"control_step": 2, "q": [0.0, 0.02], "dq": [0.0, 0.0], "plate_pose": {"frame_id": "world", "position_m": [0.5, 0.0, 0.75], "quaternion_xyzw": [0.0, 0.0, 0.0, 1.0]}}
  ],
  "contacts": {"availability": "unavailable", "records": []},
  "events": [
    {"control_step": 0, "event": "primitive_started", "call_id": "call.001", "api": "wait"},
    {"control_step": 2, "event": "primitive_finished", "call_id": "call.001", "status": "completed"}
  ],
  "frame_map": [],
  "replay": {"state_playback": "unverified", "action_replay": "unverified"},
  "result": {"outcome": "failure", "success": false, "terminated": true, "truncated": false, "reason_code": "GOAL_NOT_REACHED"}
}
```

### 动作失败返回示例

```json
{
  "call_id": "call.002",
  "status": "rejected",
  "start_step": 2,
  "end_step": 2,
  "requested": {"api": "move_tcp", "robot_id": "robot.airbot", "target_pose": {"frame_id": "unknown.frame", "position_m": [0.5, 0.0, 0.8], "quaternion_xyzw": [0.0, 0.0, 0.0, 1.0]}},
  "resolved_target": null,
  "before": {"control_step": 2},
  "after": {"control_step": 2},
  "error": {"code": "UNKNOWN_FRAME", "category": "expert_call", "message": "Target frame is not registered", "retryable": true}
}
```

`retryable=true` 只表示专家可在预算内修正参数，不表示运行时自动重复动作。该次拒绝仍写入事件流，但不新增状态或控制输入。

## 10. 与当前代码的连接及实现顺序

| 当前代码 | 复用方式 | 尚待实现 |
|---|---|---|
| `core/model.py`、`core/schema.py` | ArtifactRef、Episode 外壳、现有 payload 必需项 | 新嵌套契约校验、引用装配和 schema 版本策略 |
| `cap/search.py` | 搜索、开发/选择/技能复验分区、预算 | 本任务的公共说明、原语绑定和状态反馈 |
| `cap/xh.py` | 现有 rollout/inspect/submit broker 客户端 | Isaac Lab adapter、公开 primitive trace、Windows 执行边界 |
| 工作区 `IsaacLab-3.0/robot_profiles.py` | `make_simulation_cfg`、机器人配置和已验收增益 | 场景绑定、TCP 控制、接触能力、记录器 |
| 工作区 `DataFarm-MVP` | 动作区间与实测记录、独立审核设计参考 | 不直接当作 Lab 3.0 adapter 或真实 LLM/抓放验收 |

公开 primitive trace 保持 `event="primitive"`、唯一整数 `event_id`、`api`、`before`、`after`、`result/error`，符合现有 XHEngine 的读取约定。新增 call_id 与低层动作流建立关联。IR 的逻辑 episode_id 与 broker 的 A-xxxx attempt_id 显式关联，不混用。

建议按以下可评审交付顺序实施，不恢复已取消的探针阶段：

1. A/B 确认 SceneBinding、坐标/时钟、TCP、可用接触量和三相机身份；确定具体任务及阈值依据。
2. 实现数据结构、校验器和 `RobotRuntime`，复用 profile；验证关节名称映射、一次 step 的时间和记录语义。
3. 实现受限原语、任务 Judge 与负向对照；冻结任务契约，接现有 CaP broker。
4. 采一条完整 Episode，与 A 线完成状态回放和三视角同步联调。
5. 完成隐藏种子 ≥80% 的任务评估、动作重放 ≤1 cm 检查及 HDF5 导出，再采满 20 条。

软件验收应覆盖：混合关节单位、WXYZ/XYZW 边界、主从关节拒绝控制、N/N+1 对齐、原语中途失败、未知接触、Judge 负向对照、隐藏结果不可回流、partial 文件不可准入。物理任务和相机画面另做实际验收；本文未执行这些运行。

## 11. 评审时需要落实的项目参数

接口规则已给出；下面几项应从 A 线资产和任务要求确认后写入版本化配置，而非在代码中猜测：

- 首个任务采用单臂还是双臂；抓放入屉是否包含拉开/关闭抽屉、最终归位。
- AIRBOT 的 TCP 固定变换、夹爪开度标定、资产命名和场景初始布局。
- Newton 当前运行配置实际能提供的接触数据、时间窗口与精度。
- 成功高度、稳定窗口、允许接触、位置/姿态/速度阈值的依据。
- 三个相机的实体身份、光学标定和采样频率；Lab 3.0 无界面物理与 A 线渲染器的文件交接。

这些待定项不改变固定使用 Isaac Lab 3.0 的决定。
