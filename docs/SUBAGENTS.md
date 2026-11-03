# 子 Agent 契约（Stage 0）

事务、库存、版本、批准和提交属于确定性内核。子 Agent 只读数据、出提案，不能写业务表，也不能调用 approve 或 commit。预检会重新读数据库，不采信提案里的库存或版本。

契约类型在 `schemas/subagent.py`。注册表在 `app/orchestration/registry.py`，启动时由 `build_default_registry()` 加载。重复的 `agent_id` + 主版本，或 schema 不在目录里，都会让启动失败。运行中不能自注册，也不能改权限。

## P0 的三个规格

| agent_id | 版本 | kind | 能力 | 工具 |
|---|---|---|---|---|
| `parser` | 0.1.0 | model | `parse.text` `parse.chat` `parse.md` `parse.xlsx` | `source.read` |
| `inventory-check` | 0.1.0 | deterministic | `check.inventory` | `order.get` `inventory.check` |
| `doc-check` | 0.1.0 | deterministic | `check.downstream` | `order.get` `downstream.get` |

默认超时 30 秒，最多 8 步，重试 2 次。`parser` 使用模型：每次最多 1000 token，每个任务最多 3 次调用。确定性 Agent 的 `uses_model` 为 false。

`approve_plan`、`commit` 以及 `plans.approve`、`plans.commit`、`order.commit` 永远不在白名单里，即使规格文件写上了也会被拒绝。真正执行只读工具并写审计是 `TODO(stage-1C)`。

## 路由

Router 按「事件类型 → 能力」选 Agent，不写死 Agent 名。

| 事件 | 能力 |
|---|---|
| 非结构化输入 | 按 `text` / `chat` / `md` / `xlsx` 选一条 `parse.*` |
| `new_order` | `check.inventory` |
| `change_order`、`cancel_remaining` | `check.inventory`；有下游单据再加 `check.downstream`，否则记一条跳过，原因是「无下游单据」 |
| `query` | 不派发，走只读服务（服务本身还没写） |
| `clarify_answer` | 方案没有指定能力。返回 unspecified，不猜测 |

找不到所需能力时，结果是 `ok=false`、`code=CAPABILITY_MISSING`，消息里写明不能静默跳过。

统一输出信封是 `AgentProposal`。`fencing_token` 或 `intent_revision` 不匹配时 `accept_proposal` 丢弃提案。payload 的 schema 校验、超时和晚到结果是阶段 1C。

外部 Agent 使用同一套契约，kind 为 `external`。Stage 0 拒绝注册。说明文字里可以写「兼容某种 WebAPI 格式的模拟服务，非官方」，但不能用该厂商的名字做产品名。
