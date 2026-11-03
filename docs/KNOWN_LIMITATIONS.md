# 已知限制

Stage 4 在静态文件切分、安全上传/导出、任务来源隔离和 DB 任务视图上加入可选 OpenAI Responses 解析。默认仍为精确 FIXTURE；live 必须由操作者显式启用并配置价格、环境密钥、提供方和模型。库存和下游数字由内核重算。

- Worker 由 `worker-once` / Python 调用驱动，没有常驻调度服务；`OOS_WORKER_MAX_ACTIVE` 必须大于等于 1，默认 2。`claim()` 保留 `min(2, limits.active)` 的硬上限，配置大于 2 时，`worker-once` 在 logger `oos.worker` 上记录 WARNING，并按 2 生效，仍限制为 2 条 RUNNING 任务。outbox 默认消费者只写本地去重回执，浏览器实时推送尚未接入。R01–R09、R16 有确定性故障测试。
- 与 `docs/PLAN_v1.0_dot.md` 的设计不同，批准后尚未排队新的 commit job，也不领取新的 lease / fencing_token。`POST /plans/{plan_id}/commit` 同步调用内核，在一个 BEGIN IMMEDIATE 事务内原子提交业务写入；worker 的 lease / fencing 只用于规划阶段的 JobAttempt。
- `Operation.committed_verification_pending=True` 表示业务已提交但尚未回读核验或核验失败，不表示业务事务失败。首次提交回读一次；`commit_plan` 幂等重放（包括 `recovery.durable_commit`）仅重试该操作仍 pending 的核验，`watchdog.scan` 在现有写事务结束后重试所有 COMMITTED 且 pending 的操作，包括任务已经 SUCCEEDED 的操作。通过后只清除此标记，失败或异常保留 pending；已通过的操作跳过，写事务内重新读取标记，保留并发重试已通过的结果，不用较慢的失败结果覆盖它。重试不重新提交、不修复数据、不新增业务修订/审计/事件；核验的是当前库存流水和下游一致性，不是历史快照。没有常驻调度器，watchdog 仅在调用时运行，例如 `python -m app.cli worker-once`；无人调用且没有提交重放时，pending 不会自行消失。
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
- 结构化日志（见 `docs/OBSERVABILITY.md`）中批准、提交、outbox 事件不带 `mode`，因为 Task 表未持久化 mode；需要时用 `task_id` 关联同任务的 dispatch 事件获取。lease、recovery、watchdog 尚未打点。
- ORM relationship、生产认证、CSRF、多用户。Alembic 初始迁移已经接入。
- Qi 已确认支持按变更后 MO 计划量生成补购草稿：同一 MO、同一面料的有效 PO 全部 `RECEIVED` 且总采购量不足时，已收货 PO 只重链、永不重写数量或内容修订，缺口生成 `before=None` 的 `CHANGE_DRAFT`（`PLACED`、到料 0、`PENDING_SUPPLIER`），沿用供应商和 MO，服装 SKU 跟随新订单行。补单 ID 为按 ID 排序的首张有效已收货 PO 的 `{received_po_id}-S{n}`，从 1 起取最小空闲后缀，取消单和未链接现存单也占用 ID。已有补单计入覆盖，重复规划/提交不重复补购；批准后 ID 被占用则以现有单据过期错误拒绝，不覆盖。多个 PO 按 `(MO, 面料)` 汇总，减量优先调整未到料补单；低于已到料总量、取消已到料采购或更换已到料面料仍拒绝。草稿不代表已向供应商实际下单。未取消且未开裁（`cut_qty == 0` / `NOT_STARTED`）的 MO 可扩大 `planned_qty`，吸收同 SKU 的全部剩余订单需求；部分或全部开裁的 MO 仍不得超过原 `planned_qty`。分配后若仍有剩余需求、没有可扩容的同 SKU MO，保留「P0 不支持为剩余数量追加生产单」，不新建 MO。常见的未开裁 MO 计划量等于订单量时，同 SKU 订单增量会扩容 MO，并在同面料有效 PO 全部已收货且采购不足时生成补购草稿。
- `docs/TEST_REPORT.md`、`HANDOFF.md`、`LEARNING_GUIDE.md` 留到阶段 5。现在的测试清单是 `docs/TEST_CHECKLIST.md`

禁止用途：连接真实客户、ERP、支付、采购或发货；把 fixture 成功说成模型理解；在单价未知时打开 live；把 `T-STUB` 当成业务结果。

56 个 B/R 用例均已接入真实测试，当前没有占位跳过；这不代表完整 UI 或已验证真实 live 理解质量。

Qi 已确认：`set_total`、`replace_remaining`、`increment` 和新建订单行的数量 0 不表示取消该行，
仍以 HTTP 422 / BUSINESS_CONFLICT 拒绝，并提示取消请走显式 `cancel_remaining`，仅取消未执行部分。
负数返回 BUSINESS_CONFLICT，小数返回 SCHEMA_INVALID。

## 输入错误、预算崩溃恢复和进程并发

- 输入在创建任务、事件或调度步骤之前校验。`POST /inputs`、`POST /tasks/{id}/answers` 和 `POST /dispatch` 拒绝空白消息（`INPUT_EMPTY`）、NUL/无效 Unicode（`ENCODING_INVALID`）和超过 5 MiB 的文本（`UPLOAD_TOO_LARGE`）；仅提供来源的 inbox 请求仍可用。上传拒绝空文件/无非空内容的文件、非 UTF-8 或含 NUL 的 TXT/MD、损坏/截断/非工作簿 XLSX，分别使用 `INPUT_EMPTY`、`ENCODING_INVALID`、`UPLOAD_CORRUPT`。保留 `UPLOAD_REJECTED` 表示其他文件策略拒绝。上传失败保留 `parser_status=rejected`，使用统一错误 envelope（包括 request/correlation ID、schema_version、retryable 和 details），在数据库事务失败时删除对应原始文件。
- 单文件上限仍为 5 MiB；文件过大继续返回 HTTP 400（兼容已有上传契约），代码为 `UPLOAD_TOO_LARGE`。实际请求体上限为 5 MiB + 64 KiB（含 multipart/JSON 开销），无论是否有 Content-Length 都在读取流时限制，超出返回 HTTP 413 和相同代码；JSON 文本本身超限也为 413。不猜测或修复损坏编码。
- XLSX 的 ZIP/XML 损坏、缺少压缩包成员、截断以及 openpyxl 的格式 ValueError 仍返回 HTTP 400 / `UPLOAD_CORRUPT`。TypeError、IndexError、AttributeError、RuntimeError、NotImplementedError 等非格式解析异常返回 HTTP 400 / `UPLOAD_PARSE_FAILED`，以独立错误码区分文件损坏和服务端解析缺陷，并支持按错误码告警。响应的 `error.details.error_id` 与服务端 ERROR 日志一致；日志只记录编号、`.xlsx` 后缀、字节大小和 traceback，不记录文件名、内容、单元格值或可能含原文的异常消息/异常链。拒绝前不写来源行或文件。
- `model_usage` 的 `status` 为 `RESERVED` / `SETTLED` / `ORPHANED`，同时记录 `reserved_at` / `settled_at`。迁移 `budget_reservations` 将历史行标为 SETTLED，两个时间以原 `at` 回填；它无法识别旧版本遗留的未结算预留，历史费用保持原值。部署前运行 `.venv/bin/python -m app.cli init-db` 迁移，启动生命周期不会自动建库或迁移。
- 后续迁移 `budget_price_snapshot`（前序 `budget_reservations`）增加可空 Decimal 单价列 `price_in_per_mtok` / `price_out_per_mtok`，采用与费用相同的 Money 类型，精确存储到六位小数。历史 `price_version` 必须为 `in=X;out=Y`，两值均能解析为有限、非负 Decimal 时才一起回填（按 Money 规则量化到六位小数）；无法解析、非有限值或负数使两列保持 NULL，表示历史单价未知。原有历史行已为 SETTLED；迁移不改其费用、状态和时间。升级前新建的 RESERVED/ORPHANED 行若携带有效版本串也按相同规则回填。
- lifespan 用 Alembic 的 ScriptDirectory 读取 head、MigrationContext 读取当前版本。已有表但版本缺失/过旧/不匹配，或缺少业务表时拒绝启动，提示 `数据库 schema 未迁移到最新版本（当前 X，需要 Y），请先运行 python -m app.cli init-db`（缺表时补充表名）。数据库文件不存在时不建立连接；已有文件或内存库完全无表时允许健康检查启动但不扫描账本，也不创建表，兼容独立 health 服务。正常业务请求仍须先显式 init-db。
- `reserve()` 原子写入悲观估算及 RESERVED 状态。`reconcile_reservations(factory, now=..., ttl=timedelta(...))` 只将年龄严格大于 TTL 的 RESERVED 行改成 ORPHANED，**完整保留费用、token 估算和每任务调用次数占用**。它不判断请求是否已发送，不释放任何可能已计费的预算。重复扫描无副作用；年轻预留以及刚好达到 TTL 的预留不变。
- 已初始化数据库在 FastAPI lifespan 启动时执行扫描；手工执行 `.venv/bin/python -m app.cli reconcile-budget` 可再次扫描。`OOS_BUDGET_RESERVATION_TTL_S` 必须是正整数，有效值为配置值与 `2 * model_timeout_s + 60` 秒的较大者；默认 timeout=60 秒时 TTL=180 秒。没有常驻定时扫描器，长时间运行的服务可由操作者调用该命令。
- ORPHANED 允许迟到的 `settle()`：在原行记录实际 token/费用，状态改 SETTLED 并设置 settled_at，因此既不会插入第二次调用，也不会累加两笔费用。相同 token 数的重复结算保持第一次结果和时间；不同 token 数的重复结算抛出 ValueError，防止覆盖已经确认的用量。实际用量高于估算仍照实记账；未知用量沿用悲观估算。`reserve()` 将当时的输入/输出单价写入行，普通和 ORPHANED 迟到结算均使用行内快照，因此跨重启改价不影响原调用费用。仅当行内某项单价为 NULL（未知历史单价）时，该项才回落到当前账本单价；零单价不会触发回落。
- `tests/test_multiprocess.py` 使用独立 venv Python 子进程、文件 SQLite、同时起跑屏障。四进程争用同一批六个任务，通过原 worker lease/fence API 执行，再由测试中的 demo human 显式批准并提交；检查任务、操作、库存预占/流水、outbox 和消费者回执各一次，并重放 commit 验证幂等。独立合成 SKU 避免把正常的库存版本冲突误判成重复领取问题。另测四进程争用只容纳三笔的预算，以及旧 worker 暂停、父进程到期接管后，旧调度及内核写入都被 fence 拒绝。worker 本身仍停在 AWAITING_APPROVAL，不自动批准。
- `tests/test_multiprocess.py` 在高负载机器上偶发变慢，等待子进程 ready 的上限已从 15 秒放宽到 30 秒。
- 并发测试沿用生产连接的 WAL、foreign_keys=ON、busy_timeout=5000ms 和写事务 BEGIN IMMEDIATE；未观察到 database is locked，不新增自动重试，也不放宽版本/库存/批准/fence 检查。SQLite 仍只有一个写者，锁持有超过 busy timeout 会报错；短事务是当前运行约束。这些测试不模拟杀死处于提交系统调用中的进程或任意长时间写锁。

- 模型传输层抽成 provider 接口：`openai`（Responses API，行为不变）和 `openai_compatible`（Chat Completions，面向 DeepSeek / vLLM / Ollama）。兼容 provider **只用 mock 测试，未做任何真实联调**。未实测：各家 json_object/json_schema 支持、usage 与错误体格式。远端必须 HTTPS 且需要运行时密钥；仅 localhost/127.0.0.1/::1 允许 HTTP 和免密钥。没有按提供方设置 base URL 白名单：操作者配置的任何 HTTPS 地址都会收到 key。localhost 例外按主机名字符串判定、不做 DNS 解析（例如 hosts 文件把别的名字指向 127.0.0.1 不享受例外，localhost 被改指向远端也不会被发现）。缺单价时拒绝启用 live（保持 FIXTURE），本地模型须显式填 0。默认 `json_object` 加本地 schema 校验；本地 schema 校验器只支持 parser 用到的子集，遇到不认识的关键字在调用前 fail-closed，返回 SCHEMA_INVALID，不预留或计费。兼容 provider 不发送 store，是否留存数据取决于提供方政策；也不发送 `reasoning` 字段。

## 后续计划

- 拆分 app/services/kernel.py（约 1500+ 行）：按计划/预检、审批、提交、下游重链、校验拆成子模块，行为不变。
- 解开 `app/orchestration/inbox.py` 与 `app/services/kernel.py` 之间仍存在的包间互相依赖（inbox 导入 `invalidate_order`，kernel 导入 orchestration.state）。

