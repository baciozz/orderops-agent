# 接口契约（Stage 4）

已接通确定性业务内核、持久任务、静态来源上传、任务视图与导出，以及受预算限制的可选 OpenAI Responses 解析。默认 FIXTURE；live 须显式启用并配置价格、密钥、提供方和模型。

结构化业务响应通常带 `request_id`、`correlation_id`、`schema_version`；根路径、来源 spans 列表、HTML 和文件下载使用各自格式。错误体是：

```json
{
  "request_id": "...",
  "correlation_id": "...",
  "schema_version": "1.0",
  "error": {"code": "TASK_NOT_FOUND", "message": "中文说明", "retryable": false, "details": {}}
}
```

形状错误走 400 `VALIDATION_ERROR`。业务错误码由 `AppError` 决定，例如 404 `TASK_NOT_FOUND`。堆栈和密钥不会放进响应。上传拒绝返回 400、`parser_status=rejected` 和中文原因。

## 已挂载

以下路由均由 `app/main.py` 挂载。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 进程状态、FIXTURE/LIVE、预算上限及 live 阻断原因；兼容字段 `stage` 仍为 `"0"` |
| GET | `/` | 服务信息、健康检查和任务演示桩链接 |
| POST | `/dispatch` | 同步调度：文本 → Router → 子 Agent → 预检；默认 fixture，可按配置使用 live |
| POST | `/inputs` | 原子保存输入事件和任务，返回 202 入队回执 |
| GET | `/tasks/{task_id}` | 持久状态、问题、计划、来源、结果和错误码 |
| POST | `/tasks/{task_id}/answers` | 保存新回答事件，恢复等待中的同一任务，返回 202 |
| POST | `/tasks/{task_id}/cancel` | 取消任务；已提交时返回 ALREADY_COMMITTED |
| GET | `/tasks/{task_id}/view` | 数据库任务视图：调度、差量、下游、来源、批准区和结果 |
| GET | `/ui/tasks/{task_id}` | 同一任务视图的 HTML 页面 |
| GET | `/tasks/{task_id}/download` | 已有变更清单的 CSV/XLSX 下载，`fmt` 默认为 `csv` |
| POST | `/sources` | multipart `task_id` + `file`；每次一个文件、每任务最多三个，静态切分 TXT/MD/XLSX |
| GET | `/sources/{source_id}/spans` | 必填查询参数 `task_id`，只读取属于该任务的来源 |
| GET | `/orders/{order_id}` | 当前订单版本、状态和行明细 |
| GET | `/stock` | 库存与可用量查询 |
| POST | `/plans` | 订单变更的确定性预检，生成计划或澄清问题 |
| POST | `/plans/{plan_id}/approve` | 校验计划 hash 和意图版本，以服务端演示身份创建限时批准 |
| POST | `/plans/{plan_id}/commit` | 校验批准、版本和库存，原子提交并回读核验；支持幂等重放 |
| GET | `/inspections/downstream` | 只读下游一致性巡检，返回漂移列表 |

`T-STUB` 保留为空结构演示，`approvable=false`；实际任务从数据库读取。批准过期时视图设置 `disabled_reason=EXPIRED`。页面展示可批准状态，浏览器提交审批的交互尚未实现，操作仍走业务 API。

## 尚未挂载的契约

`schemas/api.py` 保留方案中的模型；以下接口当前没有路由。

| 方案中的接口 | 契约 |
|---|---|
| GET `/plans/{id}` | 预检响应契约 |
| GET `/operations/{id}` | `OperationGetResponse` |
| POST `/purchase-proposals` | `PurchaseProposeRequest` / `PurchaseProposeResponse` |

示例 JSON 在 `schemas/examples/`。`PlanChangeRequest` 拒绝多余字段，包括客户端自带的 `approved`。批准身份来自服务端本地演示用户，不接受模型或请求体自批。

## 持久执行与来源

- `POST /inputs` 使用 `InputSubmitRequest`，在一个事务保存 Event、Task、InputPayload 和 InboxDelivery，返回 **202** 的 `InputSubmitResponse`。`client_event_key` 是来源投递 ID / 客户端事件 ID，在 thread 内去重；相同文字的新 key 是新事件。选择订单时，接收事务就递增意图并使旧批准失效，重复投递不递增。
- `python -m app.cli worker-once` 执行恢复扫描、一条任务和 outbox 投递。202 不是业务完成回执；API 入队不自动运行 worker。
- 回答使用同一输入模型和新 key。默认 fixture 回答使用完整的已登记语句；可选 live 按预算调用模型。worker 在写入结果前检查租约与 fencing。
- `source_ids` 持久保存，仅允许读取当前任务关联的静态 spans。上传本身只保存证据，不触发调度或批准；回答可显式选择上传来源参与核对。
- 取消先完成则 commit 返回 `CANCELLED`；commit 先完成则取消返回 `ALREADY_COMMITTED`，不撤销业务历史。
- 幂等键、plan_hash 和版本冲突规则沿用方案第 7 节；提交响应丢失后必须重用原请求和原 key。
