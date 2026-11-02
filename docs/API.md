# 接口契约（Stage 0）

所有已实现的响应都带 `request_id`、`correlation_id`、`schema_version`。错误体是：

```json
{
  "request_id": "...",
  "correlation_id": "...",
  "schema_version": "1.0",
  "error": {"code": "TASK_NOT_FOUND", "message": "中文说明", "retryable": false, "details": {}}
}
```

形状错误走 400 `VALIDATION_ERROR`。业务错误码由 `AppError` 决定，例如 404 `TASK_NOT_FOUND`、429 `BUDGET_EXCEEDED`、503 `PRICES_NOT_CONFIGURED`。堆栈和密钥不会放进响应。

## 已挂载

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 进程活着、阶段 0、FIXTURE 或 LIVE、预算上限 |
| GET | `/` | 指向健康检查和任务桩 |
| GET | `/tasks/{task_id}/view` | 修订第 5 节的任务视图。只有 `T-STUB` 有内容 |
| GET | `/ui/tasks/{task_id}` | 同一结构的朴素 HTML，没有视觉设计 |

`T-STUB` 的 `approvable` 为 false，调度轨迹和结果都是空的。过期批准使 `disabled_reason=EXPIRED` 的行为是 R24，阶段 3 才做。

## 已冻结、尚未挂载

模型在 `schemas/api.py`。名称对应方案第 8 节，实现阶段见下表。

| 方案中的接口 | 模型 | 计划阶段 |
|---|---|---|
| POST `/sources` | `SourceUploadResponse` | 3 |
| POST `/inputs` | `InputSubmitRequest` / `InputSubmitResponse` | 2 |
| GET `/tasks/{id}` | `TaskGetResponse` | 2 |
| POST `/tasks/{id}/cancel` | `TaskCancelRequest` / `TaskCancelResponse` | 2 |
| GET `/orders/{id}` | `OrderGetResponse` | 1A |
| GET `/stock` | `StockQuery` / `StockResponse` | 1A |
| POST `/plans` | `PlanChangeRequest` / `PlanPreviewResponse` | 1A |
| GET `/plans/{id}` | 与预检响应同一契约，阶段 1A 再挂 | 1A |
| POST `/plans/{id}/approve` | `ApproveRequest` / `ApproveResponse` | 1A |
| POST `/plans/{id}/commit` | `CommitRequest` / `CommitSuccess` | 1A |
| GET `/operations/{id}` | `OperationGetResponse` | 1A |
| GET `/sources/{id}/spans` | 来源模型 `SourceRef` | 3 |
| POST `/purchase-proposals` | `PurchaseProposeRequest` / `PurchaseProposeResponse` | 1A |

示例 JSON 在 `schemas/examples/`。`PlanChangeRequest` 拒绝多余字段，包括客户端自带的 `approved`。批准只能由以后的服务端会话创建。

幂等键、plan_hash 和版本冲突的规则沿用方案第 7 节，提交服务还没写。
