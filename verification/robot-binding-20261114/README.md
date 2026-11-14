# Robot API 自动发现与绑定验证

日期：2026-11-14（Asia/Shanghai）。机器可读结果见 `summary.json`，含实际运行文件摘要、代码摘要、版本与绑定摘要；详细本机结果保留在仓库 `runs/robot-binding-check/`，该目录按仓库规则不提交。

## 已执行

- 新增 12 项 CPU 测试全部通过；与现有 CaP 合跑 29 项，最终 27 项通过、2 项 Windows/POSIX 限制跳过。
- 中间一次合跑触发现有 CaP 30 ms 测试期限的时序波动，最终合跑通过；未修改原测试期限或模型/运行代码。
- 现有 AIRBOT 单臂、双臂、双 xArm 的 URDF 均成功发现与静态绑定。
- 实际 Lab 3.0 / GPU Newton + MJWarp：单臂识别 9 个运动关节、7 个有效位置驱动，拒绝 2 个 mimic 从关节目标；双臂分别为 18、14、4。
- 两个实际实例均读取实测状态、暂存按名称映射的当前位置目标、实际推进一个仿真步并返回有限状态；不评测跟踪轨迹或抓放。
- 单臂另外使用人工的 link 偏移配置执行 XYZW 变换组合分支；配置的来源明确是 synthetic-transform-fixture，不宣称是真实 TCP 标定。实际夹爪开度标定未提供，相关能力保持关闭。
- standing-rules lint：11 条规则，无违规。新增 Python 文件编译检查通过。

## 限制

完整 pytest 因当前普通 Python 未安装 pytest 而未执行；指令层 audit 仍因原有 `datafarm.common` 导入 POSIX fcntl 而在 Windows 失败。simulation 包采用延迟导出，使独立自动发现功能不受该上游导入限制；没有伪装 fcntl 或改变原 broker 的隔离行为。

未验证：xArm 实例的新 API GPU 集成、真实 TCP/夹爪标定、IK/运动规划、动态限速、任务成功率、接触、相机、完整 Episode 与 HDF5。这些不属于自动识别已完成的验收。仅有原始 USD、没有 URDF 来源描述的通用发现仍不支持。

本次没有调用模型、读取密钥、运行抓放后端选择探针、切换 SAPIEN、改变原机器人资产或驱动增益。
