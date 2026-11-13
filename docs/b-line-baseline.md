# B 线开发基线

更新日期：2026-11-13（Asia/Shanghai）。

用户要求：以同学在 GitHub 上传的新分支为基线继续开发。

## 来源与分支

- 仓库：https://github.com/Marcoskk7/datafarm-lite
- 基线分支：`translate-view`
- 基线提交：`73669e9d9f0a805ef0acdbb0a2e7511dbfa67fc7`
- 本地开发分支：`feat/b-control`
- 合入已有 CaP 功能提交：`f937fc8d10db0453236a644816a4de256ae97adc`
- 基线包含两次中文翻译提交，覆盖文档、提示词、注释和文档字符串；不能视为新增机器人任务验收。
- 合并保留中文文档，并补回 CaP 文档索引和 expert-harness 入口。原分支保留。

## 软件验证

在工作区临时目录下运行 `python -m unittest test_cap_search test_cap_xh test_cap_broker_integration -q`：17 项，15 项通过、2 项因 Windows 的 POSIX broker 限制跳过。

默认沙箱临时目录曾导致原子文件替换 WinError 650，改用工作区 `tmp/b-control-tests` 后解决；首次复跑有一项 30 ms broker 测试超时，再次运行全部可运行项通过，保留为测试时序不稳定风险，未修改产品代码或测试期限。

`python tools/lint_standing_rules.py`：11 条规则，无违规。

完整 pytest 尚未执行：当前普通 Python 缺少 pytest。指令层审计因上游 `datafarm.common` 在 Windows 导入 `fcntl` 失败，尚未通过。这里的 CPU 检查不证明机器人抓放、LLM 成功率或物理重放通过。

## 已确定的仿真后端

用户在本次对话明确要求：“这部分探针取消，直接确定使用Issac lab3.0作为仿真后端”。此决定替代之前通过抓放探针选择或切换后端的建议。

- 仿真框架固定为 Isaac Lab 3.0，复用工作区 `IsaacLab-3.0` 的独立环境及已验证机器人资产。
- 物理解算沿用现有已验证配置 Newton + MJWarp（`newton_mjwarp`）；Isaac Lab 是仿真框架，Newton/MJWarp 是其物理后端配置。
- 取消独立的后端选择／抓放探针阶段和“10 次至少 8 次成功后再决定后端”的前置门槛，取消 SAPIEN 回退开发路径。
- 后续开发直接进入 Robot API、任务契约与 Episode 接口；任务成功率、记录准确性及重放验收仍在相应开发阶段实测。
- 当前资产验收范围为无界面机器人动力学基线；相机、任务抓放和完整场景集成尚未验收。

## B 线接续路径

1. 与 A 线固定 SceneGraph 交接字段，以及 Judge/Episode 的单位、坐标系、四元数、时间和关节语义。
2. 在固定的 Isaac Lab 3.0 环境中，复用 `IsaacLab-3.0/validated_usd/airbot_single/robot.usda` 和 `robot_profiles.py`，直接实现 Robot API 的复位、观测、末端控制、夹爪、步进与接触读取，并对接 A 线场景。
3. 接入已有 `src/datafarm/cap/`，冻结 Judge，分离开发和隐藏评估种子。
4. 隐藏评估达到 80% 后，先采一条 Episode 与 A 线联调，完成位置重放误差不超过 1 cm 的检查和 HDF5 导出，再扩到 20 条。

工作区 `DataFarm-MVP` 与 `aspire-agent` 是参考实现，不是本分支的新基线。旧推盘入屉与非接触任务的通过记录不能代替完整抓放验收。
