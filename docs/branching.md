# 分支与 MR 规范

一个功能一个分支，从最新的 `origin/main` 切出，命名 `<type>/<line>-<slug>`。

| type | 用途 | 例子 |
|---|---|---|
| `feat` | 新行为 | `feat/A-scene-assembly` |
| `fix` | 修 bug | `fix/B-judge-holdout` |
| `docs` | 只改文档 | `docs/branch-naming` |
| `refactor` | 行为不变的结构调整 | `refactor/kernel-store` |

- `<line>` 是 `A`（资产、场景、渲染）或 `B`（控制、判定器、episode）；跨线的公共内核省略这一段，例如 `feat/kernel-store`。
- `<slug>` 用小写 kebab-case，2–4 个词，描述功能本身；不放人名、日期、工单号，也不用序号代替语义。
- 分支从当前 `origin/main` 切出；请求评审前先 rebase 或合并 `main`。
- 一个分支只带一个功能和它的证据。后续功能另开分支，不在这条分支上继续长。
- 分支内提交用 `type(scope): summary`；只改证据的提交用 `test(scope):`。
- MR 标题重复分支的功能名，正文写清：改了什么、怎么验证的（命令、结果）、哪些没验证、评审要注意的地方。
- 分支与 MR 一旦合并即删除；`main` 上不留长期开发分支。

## 示例

```text
feat/A-scene-assembly      SimReady 检索 + 场景组装 + 一条命令验收
feat/A-render-episode      episode 状态流 → Isaac RTX 出视频
fix/B-tcp-ik               TCP IK 求解的边界条件
docs/branch-naming         本规范
```
