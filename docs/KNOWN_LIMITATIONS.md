# 已知限制

Stage 4 在静态文件切分、安全上传/导出、任务来源隔离和 DB 任务视图上加入可选 OpenAI Responses 解析。默认仍为精确 FIXTURE；live 必须由操作者显式启用并配置价格、环境密钥、提供方和模型。库存和下游数字由内核重算。

- Worker 由 `worker-once` / Python 调用驱动，没有常驻调度服务；outbox 默认消费者只写本地去重回执，浏览器实时推送尚未接入。R01–R09、R16 有确定性故障测试。
- 上传为 `POST /sources` 的 multipart `task_id` + `file`，一次一个文件，每任务最多三个；任务须已存在。服务端生成存储路径，通过 `task_source` 关联限制 spans 查询，原始文件放在服务端临时目录，生产持久存储/清理策略尚未实现。拒绝返回 HTTP 400、parser_status=rejected 和中文原因，不保存部分 spans。
- UTF-8 TXT/MD 按非空行、静态 XLSX 按单元格切分；spans 摘要最多 500 字，完整原文独立保存。XLSM、VBA/宏类型、公式、外部链接和 XML 实体被拒绝；XLSX 有压缩包大小/压缩比及非空单元格上限。上传不会触发调度、批准或外传。
- R14 已接入 worker 的 `source_ids`：只读取当前任务关联的静态 spans，精确 fixture 聊天与匹配表格行数量不同时，保存 conflicts、questions 和聊天/单元格双来源，返回 NEEDS_CLARIFICATION，不创建计划、批准或提交。JSON 和 HTML 可展开来源并显示问题；抽取结果保存在当前事件的 InputPayload 中，原 text/source_ids 保留。
- 数量比较只支持第 1 行为明确列名的 XLSX：改单为 `订单号 / 原SKU / 目标SKU / 数量`，新建为 `客户 / SKU / 数量`；数量为非负整数。不匹配行、不支持的布局或无效数量先澄清。FIXTURE 仍精确匹配；可选 live 可解析自然语言和中文颜色/尺码别名，但真实模型质量尚未验证。live 上下文只加载 allowlist 内合成 SKU、客户和范围内的开放订单；不支持真实业务主数据。上传内容视为数据，通用表格布局仍不支持；未显式选择的上传不参与比较。R10 仍验证上传无副作用及越权文本在 fixture worker 中被拒绝。
- 最小黄金链由 `test_fixture_golden_chain_upload_chat_precheck_approve_commit_export` 覆盖：`POST /inputs` 发送 `先上传订单表，再核对聊天数量` → worker 等待上传 → `POST /sources` → 同任务 `POST /tasks/{id}/answers` 带完整 fixture 文本和 source_ids → worker 预检 → 批准/提交 → 带来源的任务视图和下载。它是受控 API/fixture 演示。
- DB 任务视图已有调度、差量、下游、来源、批准过期置灰和 CSV/XLSX 下载，任务卡和下载清单按任务账本标注 FIXTURE/LIVE。完整聊天入口、任务列表、澄清/批准按钮的浏览器提交交互和独立审计报告留待下一波；当前批准按钮只展示可批准状态，操作仍用已有业务 API。T-STUB 保留为演示桩。
- 对照的旧/新数量取计划基础版本和已提交版本；锁定量、可用量及下游状态来自当前业务事实，不是完整历史快照。计划因当前业务条件不可计算时不生成猜测差量。
- live HTTP 传输已实现，仅支持官方 OpenAI Responses API，默认关闭；预算熔断和缺单价/密钥保持 FIXTURE 延续 R22/R23。`scripts/live_smoke.py` 是操作者手动运行的临时合成库评估，真实 live 结果见 `docs/LIVE_EVAL.md`（2026-11-03，gpt-6-luna，两次合计 $0.003645）。单次 smoke 上限 1 美元，普通 live 硬上限 5 美元。CI 永远只用 fixture / MockTransport。
- live 预算和每任务调用次数在同一个 SQLite 写事务中预留，响应后在原用量行结算；进程崩溃、超时、连接失败或成功响应缺有效用量时保留悲观估算。提供方报告的实际用量即使超过估算也照实记录。HTTP 错误仍占调用次数，无用量时费用/token 为零，任务卡仍标注 LIVE。429/5xx/超时/连接错误不会自动重试，须重新提交新任务。测试从收集阶段起禁用 dotenv，生产 `.env` 加载不变。
- LIVE 的 `replace_remaining` 若模型返回 `qty: null` 且没有明确的 `only_uncut` 选择，返回 NEEDS_CLARIFICATION，询问总件数或是否只改未开裁；不会把“全部”自行解释为未执行量。精确 FIXTURE `TEXT_ALL_NAVY` 提供 300 件，仍走已裁剪锁定的 B19 澄清。离线顺序回放确认：原 STALE_ATTEMPT 是缺数量导致内核先持久化 FAILED/BUSINESS_CONFLICT、清理阶段再次校验 RUNNING fence 后掩盖原错误；不是同链等待任务、超时或 usage 独立事务导致。清理对已结束任务只读返回，实际写入仍校验完整 fence。
- ORM relationship、生产认证、CSRF、多用户。Alembic 初始迁移已经接入。
- `docs/TEST_REPORT.md`、`HANDOFF.md`、`LEARNING_GUIDE.md` 留到阶段 5。现在的测试清单是 `docs/TEST_CHECKLIST.md`

禁止用途：连接真实客户、ERP、支付、采购或发货；把 fixture 成功说成模型理解；在单价未知时打开 live；把 `T-STUB` 当成业务结果。

56 个 B/R 用例均已接入真实测试，当前没有占位跳过；这不代表完整 UI 或已验证真实 live 理解质量。

## 输入错误、预算崩溃恢复和进程并发

- 输入在创建任务、事件或调度步骤之前校验。`POST /inputs`、`POST /tasks/{id}/answers` 和 `POST /dispatch` 拒绝空白消息（`INPUT_EMPTY`）、NUL/无效 Unicode（`ENCODING_INVALID`）和超过 5 MiB 的文本（`UPLOAD_TOO_LARGE`）；仅提供来源的 inbox 请求仍可用。上传拒绝空文件/无非空内容的文件、非 UTF-8 或含 NUL 的 TXT/MD、损坏/截断/非工作簿 XLSX，分别使用 `INPUT_EMPTY`、`ENCODING_INVALID`、`UPLOAD_CORRUPT`。保留 `UPLOAD_REJECTED` 表示其他文件策略拒绝。上传失败保留 `parser_status=rejected`，使用统一错误 envelope（包括 request/correlation ID、schema_version、retryable 和 details），在数据库事务失败时删除对应原始文件。
- 单文件上限仍为 5 MiB；文件过大继续返回 HTTP 400（兼容已有上传契约），代码为 `UPLOAD_TOO_LARGE`。实际请求体上限为 5 MiB + 64 KiB（含 multipart/JSON 开销），无论是否有 Content-Length 都在读取流时限制，超出返回 HTTP 413 和相同代码；JSON 文本本身超限也为 413。不猜测或修复损坏编码。
- `model_usage` 的 `status` 为 `RESERVED` / `SETTLED` / `ORPHANED`，同时记录 `reserved_at` / `settled_at`。迁移 `budget_reservations` 将历史行标为 SETTLED，两个时间以原 `at` 回填；它无法识别旧版本遗留的未结算预留，历史费用保持原值。部署前运行 `.venv/bin/python -m app.cli init-db` 迁移，启动生命周期不会自动建库或迁移。
- `reserve()` 原子写入悲观估算及 RESERVED 状态。`reconcile_reservations(factory, now=..., ttl=timedelta(...))` 只将年龄严格大于 TTL 的 RESERVED 行改成 ORPHANED，**完整保留费用、token 估算和每任务调用次数占用**。它不判断请求是否已发送，不释放任何可能已计费的预算。重复扫描无副作用；年轻预留以及刚好达到 TTL 的预留不变。
- 已初始化数据库在 FastAPI lifespan 启动时执行扫描；手工执行 `.venv/bin/python -m app.cli reconcile-budget` 可再次扫描。`OOS_BUDGET_RESERVATION_TTL_S` 必须是正整数，有效值为配置值与 `2 * model_timeout_s + 60` 秒的较大者；默认 timeout=60 秒时 TTL=180 秒。没有常驻定时扫描器，长时间运行的服务可由操作者调用该命令。
- ORPHANED 允许迟到的 `settle()`：在原行记录实际 token/费用，状态改 SETTLED 并设置 settled_at，因此既不会插入第二次调用，也不会累加两笔费用。相同 token 数的重复结算保持第一次结果和时间；不同 token 数的重复结算抛出 ValueError，防止覆盖已经确认的用量。实际用量高于估算仍照实记账；未知用量沿用悲观估算。结算继续使用调用方账本的配置单价，操作者跨重启迟到结算时应使用原调用单价。
- `tests/test_multiprocess.py` 使用独立 venv Python 子进程、文件 SQLite、同时起跑屏障。四进程争用同一批六个任务，通过原 worker lease/fence API 执行，再由测试中的 demo human 显式批准并提交；检查任务、操作、库存预占/流水、outbox 和消费者回执各一次，并重放 commit 验证幂等。独立合成 SKU 避免把正常的库存版本冲突误判成重复领取问题。另测四进程争用只容纳三笔的预算，以及旧 worker 暂停、父进程到期接管后，旧调度及内核写入都被 fence 拒绝。worker 本身仍停在 AWAITING_APPROVAL，不自动批准。
- 并发测试沿用生产连接的 WAL、foreign_keys=ON、busy_timeout=5000ms 和写事务 BEGIN IMMEDIATE；未观察到 database is locked，不新增自动重试，也不放宽版本/库存/批准/fence 检查。SQLite 仍只有一个写者，锁持有超过 busy timeout 会报错；短事务是当前运行约束。这些测试不模拟杀死处于提交系统调用中的进程或任意长时间写锁。
