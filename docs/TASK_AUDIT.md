# 任务审计摘要 v1

`GET /tasks/{task_id}/audit` 返回独立 JSON 下载；真实任务的 HTML 页面有同名入口。
这不改变冻结的 `TaskView`、CSV/XLSX 下载或业务 API，也不需要迁移。
此入口只供现有本机演示使用，**不是生产认证、多用户授权、真实 ERP 验收或模型质量证明**。

## 只读与时间语义

- 每次请求使用新的 `Session(autoflush=False)`。现有 `make_engine` 禁用 pysqlite 隐式事务，
  begin 监听器显式发送 deferred `BEGIN`；表检查和全部查询使用同一连接、同一读快照。
  结束时 rollback/close，不提交、不设置连接池 `query_only`，不使用 `BEGIN IMMEDIATE`。
  WAL 下并发写者可在导出读取期间提交，当前导出继续看到旧快照；下一次请求才看到新状态。
- `task`、计划/批准状态、来源元数据和 `current_verification` 是这次读取观察到的记录。
  `receipt` 仅提取已保存提交回执的订单号、版本和审计号，独立于会变化的核验状态。
  **pending 表示业务已经提交，不是待提交，也不是重新提交或修复的依据。**
- GET 不重新核验、不读取原始文件、不计算业务状态快照，不执行 worker、审批、提交、修复、
  预算对账或迁移。计划状态不等于此时可批准，批准状态也不等于此时可提交。
- 现有核验检查的是执行核验时的当前业务事实。报告只展示已记录标记，不证明当前业务一致，
  也不证明某次历史提交时的全部事实。没有完整历史快照、核验时间历史或可靠 plan→input 外键。
- 现有 commit 审计的 `before_hash` 和 `after_hash` 都是计划 hash，并非业务前后状态 hash。
  因此报告不导出这两个字段；审计的 `plan_ref` 指向本报告 `plans[].plan_hash`。
  报告不是签名或防篡改凭证，不对记录完整性作此类保证。
- 不从当前进程设置推断历史 FIXTURE/LIVE。`semantics.runtime_mode` 固定为
  `not_attested_by_this_report`；报告不证明模型模式或真实业务来源。
- 无生成时间、随机请求号或动态到期计算；数据库未变时 JSON 字节稳定。
  请求追踪号仍在 HTTP header，失败使用原有错误 envelope。

## 固定白名单形状

`schemas/task_audit.py` 定义独立版本 `export_version: "1.0"`，禁止额外字段。
没有结果的任务保留所有顶层字段，用空数组表达没有记录，不伪造提交回执。

| 字段 | 内容 |
| --- | --- |
| `semantics` | 单次读快照、历史/核验/模式限制、输入排序及来源覆盖范围的固定说明 |
| `limits` | `rows_per_collection: 1000`、`output_bytes: 4194304` |
| `task` | task_id、order_id（可空）、status、intent_revision、cancel_requested |
| `inputs` | event_id、input_sequence（可空）、kind、received_at、intent_revision（可空） |
| `plans` | 报告内 report_ref、kind、status、base_order_revision、intent_revision、chain_intent_revision、plan_hash、source_ids、approvals |
| `plans[].approvals` | status、intent_revision、expires_at、consumed_operation_id（可空） |
| `committed_operations` | operation_id、plan_ref、receipt、current_verification |
| `receipt` | order_id、revision、audit_id；只取持久化 JSON 的明确路径 |
| `current_verification` | pending、retry_attempts、next_attempt_at（可空）、manual_required |
| `business_audits` | audit_id、operation_id、plan_ref、action（固定 commit）、actor_type、object_id、at、source_ids |
| `sources` | source_id、sha256、media_type、size、parser_version |

`actor_type` 仅为 human/subagent/system/unknown 类别，未知值归为 unknown，绝不输出 actor_id。
输入类别仅为 input.submit/clarify_answer/unknown。标识符使用领域的非空、64 字符长度，保留 Unicode，
由 JSON 编码和 URL percent-encoding 安全转义，不把标识符拼入下载文件名。
来源 MIME 只含现有 TXT/MD/XLSX 类型，parser_version 为当前 static-v1；不支持的记录格式失败关闭。

## 隔离、排序与覆盖

- 输入经 `InputPayload.task_id` 归属；计划经 `Plan.task_id`；操作必须由该任务计划关联且状态为
  COMMITTED。回执内 task/plan/order 和审计归属必须一致，否则 409，不回落到 Task.result_ref。
  不按共享 chain_id、thread_id 或 order_id 扩大范围。
- 只导出这些提交操作的 `commit` 业务审计。`tool.denied`、`proposal.discarded`、调度日志和预算日志
  不在 v1 范围内，不应把此文件称作全部应用审计。
- 输入正整数 sequence 优先升序，同序列按 received_at/event_id 排序；缺失/null 的历史 sequence
  置 null，排在末尾，以 received_at/event_id 稳定排序。已提供但非正整数的序列返回 409。缺失/重复/不连续序列时
  `causal_sequence_complete=false`。只有非空输入集合中，序列恰好为 1 至记录数且无缺失/重复时才为 true。
  空输入集合（包括没有 inbox 记录的历史任务、直接规划/提交的任务）也为 false，表示不能建立完整因果序列，
  不表示记录损坏或业务未提交。这个标记不是历史日志完整性的证明；时间/UUID 排序不能替代因果顺序，
  不能推断具体计划源于哪次输入。
- 计划按持久 ID 排序后编号 plan-1、plan-2；编号只在本报告内引用，不表示计划生成时间。
  批准按所属计划、expires_at、持久 ID 排序；操作按 ID；审计按 at/ID；来源及来源引用按 ID。
- 来源仅来自该任务的 `TaskSource` 关联。计划和审计 source_refs 在 SQLite 中只投影 source_id，
  校验引用对象及 source_id 的 JSON 字符串类型后，去重并与任务所属来源匹配。不导出其他任务的来源或没有持久化任务所属来源的 fixture/chat 引用，
  也不把这些引用认作已验证文件。报告不读取文件或重新计算 SHA-256。
- 不导出 raw input/body、原文件/摘录/单元格内容、文件名、本机路径、客户/演员身份、
  thread/chain ID、客户端事件/上传/幂等键、凭据、租约 token、模型请求/响应或任意 JSON 扩展字段。
  活跃计划和批准 ID 不导出，不提供批准、提交或修复链接。来源 hash 仍是元数据，分享前应自行审查。

## 上限、空库及错误

- 输入、计划、全部批准、提交操作、业务审计和来源各最多 1,000 行。
  来源关联另有两个独立的**全局**上限：所有计划的 distinct `(plan, source_id)` 对最多 1,000，
  所有业务审计的 distinct `(audit, source_id)` 对也最多 1,000，不是每个计划或审计各限 1,000。
  例如 334 个计划各引用同一组 3 个来源，根 sources 只有 3 行，但计划来源关联达 1,002 对，仍返回 413。
  查询取上限 + 1 检测超限，不静默截断。
  最终 UTF-8 JSON 最多 4 MiB。任一超限为 **413 AUDIT_EXPORT_LIMIT**，不返回部分报告。
- 缺失任务、缺失数据库、没有 task 表的空库以及 `T-STUB` 返回 **404 TASK_NOT_FOUND**。
  缺失文件和零字节空文件在连接前检查，不创建数据库、WAL/shm 或表。
  已建立的 SQLite 数据库沿用现有引擎连接配置；只读保证指不修改表/业务账本，
  不保证 SQLite 既有 WAL、锁和连接元数据的物理文件字节完全不变。
- 不一致回执归属、损坏 JSON/时间或不支持的摘要字段格式为 **409 AUDIT_EXPORT_INVALID**。
  不回显坏值、正文或驱动错误，不尝试修复。未迁移/缺少业务表的部分 schema 不在此入口支持范围，
  应按现有启动检查由操作者显式迁移。
- 成功为 application/json 附件，固定 `task-audit-v1.json`；成功及上述应用错误均带
  `Cache-Control: no-store` 与 `X-Content-Type-Options: nosniff`。既有本机 Host 边界仍适用，
  它不是用户身份验证。导出只返回本机请求者，不会主动外发或访问网络。

## 可重复验证

```bash
python -m pytest -q tests/test_task_audit.py tests/test_task_view.py
make check
make eval-offline
```

测试仅创建临时合成数据库，覆盖 precommit、pending、succeeded、cancelled、空任务、空/缺失库，
完整 SQLite iterdump 前后相等、GET SQL 无 DML、重复字节一致、同链同订单多任务隔离、隐私 canary、
损坏记录、上限、Unicode/文件名/链接安全，以及 WAL 并发写入下的真实读快照。
未使用真实业务数据、服务进程、浏览器或 live 模型验证。
