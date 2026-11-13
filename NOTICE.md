# 声明

本仓库中的 DataFarm 源代码以 MIT License（`LICENSE`）发布。源自第三方项目的组件保留其上游许可证，列于下方。本仓库中的任何内容都不授予对它所不包含的模型、模拟器、服务或数据的权利。

## 本仓库中的第三方代码

| 路径 | 上游 | 许可证 | 备注 |
|---|---|---|---|
| `vendor/trainer/` | [openpi](https://github.com/Physical-Intelligence/openpi)（Physical Intelligence） | Apache-2.0（`vendor/trainer/LICENSE`） | 由 DataFarm 作者修改（Apache-2.0 第 4(b) 节）：dataset adapters、joint/EEF action modes、admission-bound training entry points；参见 `vendor/trainer/README.md`。仅包含本项目训练所用的文件，每个被修改的文件都带有声明。`LICENSE_GEMMA.txt` 适用于未包含在内的 PaliGemma 权重。 |
| `vendor/videox_fun/` | [VideoX-Fun](https://github.com/aigc-apps/VideoX-Fun) | Apache-2.0 | 针对 upstream 的补丁，用于 control pipeline 和 edge（HED）control 准备；参见该目录中的 README。 |
| `src/datafarm/render/h3/h3_lora.py` | Sol-H3 `h3_runtime/lora.py` | 尚未确认 | 未修改的副本（sha256 `0e8222b76e086f8f100092e325a08e86bfc8bd33fb404c8b8b0cdd4d3d56362c`）。在其条款得到确认之前，不受本仓库 MIT License 的覆盖。 |


## 未包含，需单独获取

| 组件 | 用途 | 条款 |
|---|---|---|
| MiniMax-H3 权重 | 默认渲染器（`docs/reference/rendering.md`） | 从其发布者处获取。MiniMax-H3 模型许可证限制生成输出的某些用途（包括训练其他 AI 模型）以及某些地区。在使用 H3 渲染训练数据之前请审阅该许可证；本仓库不授予对该模型或其输出的任何权利。 |
| Seedance API | 第二渲染器 | 按其 provider 条款提供的托管服务。 |
| [USDCraft](https://github.com/NVlabs/USDCraft) | 可选的物体资产生成（`docs/reference/usdcraft.md`） | Apache-2.0；需单独安装。 |
| RoboTwin 2.0、SAPIEN、Isaac Sim | 模拟 | 其各自的许可证。 |
| GPU scheduler | 作业放置（`docs/deployment.md`） | 由 deployment 提供。 |
| PiPER 机器人描述与 SDK | 机器人模型、关节限位 | 来自机器人供应商。 |
| pi0.5 / PaliGemma checkpoints | 策略训练 | openpi 和 Gemma 条款。 |
| 真实标定 episode、渲染数据集、checkpoints | 数据 | 不随代码分发。 |

本代码产生的运行记录、receipt 和证书通过哈希绑定内容；它们不授予对其所描述内容的再分发权利。
