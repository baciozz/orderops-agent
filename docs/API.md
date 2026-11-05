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
| GET | `/ui`、`/ui/demo` | 仅本机合成数据演示首页 |
| GET / POST | `/ui/demo/submit` | 改单表单；POST 复用 inbox，303 跳转任务页 |
| GET | `/ui/demo/tasks`、`/ui/demo/orders/{order_id}` | 最近任务列表、订单只读页 |
| POST | `/ui/demo/worker-once` | 本机 FIXTURE 扫描恢复、处理一条任务、投递 outbox；返回 mode / recovered_task_ids / result（含 task_id、status，空闲为 null）/ published |
| GET | `/tasks/{task_id}/download` | 已有变更清单的 CSV/XLSX 下载，`fmt` 默认为 `csv` |
| POST | `/sources` | multipart `task_id` + `file`；每次一个文件、每任务最多三个，静态切分 TXT/MD/XLSX |
| GET | `/sources/{source_id}/spans` | 必填查询参数 `task_id`，只读取属于该任务的来源 |
| GET | `/orders/{order_id}` | 当前订单版本、状态和行明细 |
| GET | `/stock` | 库存与可用量查询 |
| POST | `/plans` | 订单变更的确定性预检，生成计划或澄清问题 |
| POST | `/plans/{plan_id}/approve` | 校验计划 hash 和意图版本，以服务端演示身份创建限时批准 |
| POST | `/plans/{plan_id}/commit` | 校验批准、版本和库存，原子提交并回读核验；支持幂等重放 |
| GET | `/inspections/downstream` | 只读下游一致性巡检，返回漂移列表 |

`T-STUB` 保留为空结构演示，`approvable=false`；实际任务从数据库读取。页面已接通批准与提交。批准过期时视图仍设置 `approvable=false`、`disabled_reason=EXPIRED`，旧批准不能提交。如果计划、任务、意图和依赖检查仍有效，HTML 页面另提供「重新批准当前计划」按钮；用户必须重新核对后明确批准，再单独提交。已撤销、已消费、已取消或依赖变化的计划不会显示该恢复入口。

同一计划的有效批准重复调用 `/approve` 仍返回原批准；到期后明确再次调用会将旧批准记为 `EXPIRED` 并创建新的 `approval_id` 与有效期，不延长旧批准、不写业务订单。GET 视图不修改批准历史。续批 API 保留原有计划状态、hash 与提交的意图版本检查；库存、订单和下游依赖仍由最终 `/commit` 重新校验，页面检查不替代提交校验。现有 JSON 字段契约不变。

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

### 静态上传解析资源上限

每文件最多 10,000 个来源片段（TXT/MD 的非空行，或 XLSX 的非空单元格）。
XLSX 每张表的实际行位置不得超过 10,000，列位置不得超过 256；空白间隔也计入坐标限制。
虚报的 worksheet dimension 不作为数据范围，实际 XML 坐标在展开空行/空单元格前检查。
超过上限返回上传拒绝及中文说明，请拆分文件或将数据移至表格左上区域。
现有 5 MB、5 张工作表、总计 5,000 非空行和 100,000 非空单元格限制仍生效。
嵌套行、重复/逆序行列坐标或单元格行归属冲突返回 `UPLOAD_CORRUPT`，避免证据静默覆盖；合法隐式坐标仍受支持。

### 提交后的有限只读核验

业务已提交与核验通过是独立事实；核验 pending 或转人工不允许重新提交订单。
`result.verification` 保持 `pending` / `passed` 兼容语义，新增 `verification_manual_required`、
`verification_retry_attempts`、`verification_next_attempt_at`。任务状态仍可为 `SUCCEEDED`。
首次提交读回一次，自动重试由 watchdog 或幂等重放触发，不启动常驻调度器。
首次自动重试可立即运行；失败后指数退避，默认最多 5 次自动重试；通过后不再核验。

环境配置：`OOS_VERIFICATION_MAX_RETRIES=5`（0–100，0 表示下次到期扫描或重放时直接转人工，不执行自动重试）、
`OOS_VERIFICATION_BASE_DELAY_SECONDS=30`、`OOS_VERIFICATION_MAX_DELAY_SECONDS=3600`
（均 1–86400）、`OOS_VERIFICATION_LEASE_SECONDS=120`（1–3600）、
`OOS_VERIFICATION_BATCH_SIZE=100`（1–1000）。延迟为 base × 2^(已重试次数−1)，受 max 上限约束。
重试预算在读取前持久化；崩溃同样消耗预算，租约过期后恢复，耗尽转人工。
租约限制并发，但超时的只读核验可能仍在运行；成功结果保持有效，旧 token 不改写新计划。

操作者调查并处理后，可运行 `python -m app.cli verify-operation OPERATION_ID` 执行一次只读核验。
仅已转人工且无有效租约的 pending 操作可执行；通过后清 pending，失败仍转人工，不重置自动预算。已通过的 COMMITTED 操作直接返回成功，不发起新的读回。
命令退出码 0=已通过、1=仍未通过、2=不存在或尚不可执行。命令不会修复或重提业务数据，
也没有强制标记通过的功能。用任务视图或结构化日志定位需人工的操作；尚无外部推送通知。
升级前先运行 `python -m app.cli init-db`，历史操作和回执保持不变。
降级迁移只删除核验重试元数据列，保留已提交业务、原始回执及 pending 标记；再次升级会从零次自动重试开始，不能把降级/升级当成恢复或重试预算重置的常规操作。

### 本机浏览器请求边界

所有 HTTP 路由要求单个受信任 Host（`localhost`、`127.0.0.1` 或 `[::1]`，可带有效端口）；否则返回 `UNTRUSTED_HOST`（400）。GET/HEAD/OPTIONS 之外的方法还检查 Origin、Referer 和 fetch metadata，拒绝非同源或冲突证据，返回 `CROSS_ORIGIN_REQUEST`（403）。拒绝发生在读取请求体和路由分派之前，使用统一错误信封。无浏览器来源头的 CLI/API 请求保持兼容，不等于认证成功；不要对外开放。防嵌套响应头及完整例外见 [browser-security](browser-security.md)。
