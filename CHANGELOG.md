# Changelog

按 Keep a Changelog 风格记录面向使用者的变更，PR 标题保留原文。

## [Unreleased]

- 新增操作者显式启动的有界 FIXTURE `worker-loop`：可配置轮间等待和轮数上限、优雅停止、未处理异常非零退出；与原 worker-once 复用单轮函数，不自动批准/提交或随 API 启动。详见 `docs/WORKER_LOOP.md`。

- 新增零依赖、线程安全的 12 个进程内 counter 和 `GET /metrics` JSON 快照；测试自动清零，不跨进程聚合。
- lease 获取/到期/fencing、durable commit recovery、watchdog 扫描和逐操作 pending 核验重试接入结构化事件；任务终态在事务提交后记录，不记录客户原文。
- 保留原 50 条离线评测，追加 watchdog / worker-once 清除 pending 与幂等 commit replay 不双写用例：**53/53**，兜底 **23/23**、管道 **30/30**。补充日志、指标和端点测试及文档。
- 验证：`make check` 全绿（ruff clean、mypy 62 个源文件、912 tests passed），覆盖率 **95.06%**（4777 stmts / 236 miss；门槛 90%）。

## [1.0.0rc1] — 2026-11-06 (v1.0.0 候选，未发布，未打 tag)

基于 main `515a97a`（PR #19 合入后）的冻结候选。**Qi 批准前不打正式 v1.0.0 tag，也不视为正式发布。**

### Stage 0 / 1A / 1B / 1C

- [#1 — Stage 0 skeleton: contracts, seed, budget guard](https://github.com/baciozz/order-ops-superagent/pull/1)：建立可安装工程、合成 seed、能力契约与持久化 $5 预算守卫，默认 FIXTURE。
- [#2 — Stage 1A business kernel: revisions, stock, and atomic commit](https://github.com/baciozz/order-ops-superagent/pull/2)：订单修订、库存预占与追加式流水；计划哈希绑定人工批准，幂等键与原子提交防止重复写入，提交后回读核验。
- [#3 — Stage 1B cross-document impact check](https://github.com/baciozz/order-ops-superagent/pull/3)：跨生产/采购单核对、已执行部分锁定、未开裁澄清与变更草稿；批准绑定下游依赖版本，提交后重链。
- [#4 — Stage 1C sub-agent dispatch in fixture mode](https://github.com/baciozz/order-ops-superagent/pull/4)：Router 将 parser、inventory-check、doc-check 接入真实流程，保存调度轨迹；子 Agent 只提案，内核重算业务数字。

### Stage 2

- [#5 — Stage 2: durable execution (inbox, leases, recovery, R01-R09)](https://github.com/baciozz/order-ops-superagent/pull/5)：持久 inbox 与任务、lease/fencing/heartbeat、取消竞争、watchdog/recovery、outbox 至少一次投递及消费者去重。

### Stage 3

- [#6 — Stage 3: understanding + full UI (R10–R15, R24)](https://github.com/baciozz/order-ops-superagent/pull/6)：静态 TXT/MD/XLSX 上传与任务来源隔离、聊天/表格冲突澄清、安全导出、数据库任务视图与模式标记；完整聊天/审批交互仍受限。
- [#7 — Stage 3 review fixes: export escaping, finite qty, externalLinks, defusedxml](https://github.com/baciozz/order-ops-superagent/pull/7)：加固导出公式转义，拒绝非有限数量、XLSX 外部链接和 XML 实体，启用 defusedxml。

### Stage 4

- [#8 — Stage 4: optional OpenAI live model (gpt-6-luna) with budget guard](https://github.com/baciozz/order-ops-superagent/pull/8)：可选 OpenAI Responses API（gpt-6-luna）、严格 JSON 解析与持久用量记账；默认关闭 live，普通预算硬上限 $5，手动 smoke 上限 $1。

### Post-Stage-4 hardening（#9–#16）

- [#9 — Offline FIXTURE eval set (50 cases) + eval_offline runner](https://github.com/baciozz/order-ops-superagent/pull/9)：新增 50 条 FIXTURE 离线评测与执行器，分别衡量兜底能力、管道/内核回归，不把 fixture 成功当模型理解。
- [#10 — chore: mypy type checking + coverage report (CI steps pending workflow permission)](https://github.com/baciozz/order-ops-superagent/pull/10)：接入 mypy、覆盖率报告与 90% 门槛；增强 CI 步骤暂存，等待 workflow 权限，当前 CI 仍只跑 lint + test。
- [#11 — Robustness: structured input errors, budget reservation reconciliation, multi-process tests](https://github.com/baciozz/order-ops-superagent/pull/11)：异常输入返回结构化错误，预算预留崩溃后保守对账；以真实 SQLite 多进程测试覆盖领取、预算竞争与旧 worker fencing。
- [#12 — docs: ARCHITECTURE.md（模块/数据流/持久执行/安全边界 + mermaid）](https://github.com/baciozz/order-ops-superagent/pull/12)：补充模块、数据流、持久执行和安全边界的完整架构文档及 Mermaid 图。
- [#13 — feat: structured JSON logging + redaction (observability)](https://github.com/baciozz/order-ops-superagent/pull/13)：增加结构化 JSON 日志、耗时/错误关联与递归脱敏；不记录客户原文、prompt 或模型响应。
- [#14 — Provider interface + OpenAI-compatible Chat Completions provider (mock-only)](https://github.com/baciozz/order-ops-superagent/pull/14)：抽出 provider 接口，增加 OpenAI 兼容 Chat Completions 与本地 schema 校验；兼容提供方仅 mock 验证。
- [#15 — Cleanup: drop service placeholders, wire worker_max_active, move task_spans, refresh Stage docs](https://github.com/baciozz/order-ops-superagent/pull/15)：删除服务占位文件，接通 worker_max_active（仍最多两条 RUNNING），整理来源模块并更新阶段文档。
- [#16 — Post-merge cleanup: provider reason table, ARCHITECTURE update, startup log via get_logger](https://github.com/baciozz/order-ops-superagent/pull/16)：补全提供方错误码对照和架构现状，将 live 启动日志接入统一脱敏 logger。

### Review fixes and Qi decisions（#17–#19）

- [#17 — fix: keep commit verification pending until readback passes (review item 1)](https://github.com/baciozz/order-ops-superagent/pull/17)：业务提交事务内即标记核验 pending，只有回读通过才清除，避免崩溃后误报 passed；当时未补重试的缺口已在 #19 修复。
- [#18 — fix: review items 2-4 (customer in approval hash, PO reduce-to-received, order cut scope) + commit-job doc](https://github.com/baciozz/order-ops-superagent/pull/18)：新建订单客户纳入批准哈希；采购量减至已收量时转 RECEIVED；裁剪量按订单隔离并排除取消单。旧的已批准未提交计划需重新规划/批准；同步提交与设计中的 commit job 差异已记录。
- [#19 — Qi decisions: qty 0 is not cancel, received-PO supplementary draft, retry pending verification](https://github.com/baciozz/order-ops-superagent/pull/19)：Qi 决定：数量 0（含 set_total）不代表取消，须显式取消剩余部分；未开裁同 SKU MO 可扩量，已收货 PO 只重链、缺口生成补购草稿。watchdog 在写事务后重试 pending 核验，幂等 commit 重放（含 recovery.durable_commit）重试该操作核验，绝不重新提交业务。

### Stage 5（本 PR）

- 项目版本标记为 PEP 440 候选版 `1.0.0rc1`；子 Agent 契约版本仍为 `0.1.0`。等待 Qi 批准后再决定正式发布与 tag。
- 纳入根目录 `requirements.lock`（运行时）与 `requirements-dev.lock`（运行时+开发），由 `uv pip compile pyproject.toml --universal --python-version 3.11`（开发版另加 `--extra dev`）生成；顶层依赖继续精确钉在 `pyproject.toml`。安装可用 `pip install -r requirements-dev.lock && pip install --no-deps -e .`，也保留 `pip install -e ".[dev]"`。
- 新增本 CHANGELOG；[README 快速上手](README.md#快速上手)承载安装、离线演示、可选 live、架构、验证和后续路线的交接。
- 复核[已知限制](docs/KNOWN_LIMITATIONS.md)：#19 已修复 watchdog/replay 不补核验；残余的调度、重试上限/退避/告警、结构化日志及当前事实核验边界见 [issue #20](https://github.com/baciozz/order-ops-superagent/issues/20)。独立测试报告、交接手册和学习指南延期，本候选以 README + CHANGELOG 交接。
- 更正待启用 CI 的注释：#9 与离线脚本已在 main；YAML 步骤保持不变，仍等待 workflow 权限。

### 验证

2026-11-06 本分支已验证，全部为 FIXTURE，无 live 调用：

- `make check` 全绿：ruff clean；mypy “Success: no issues found in 61 source files”；pytest **902 passed**。
- 覆盖率 **95%**（TOTAL **4673** stmts，**233** miss；`fail_under=90`）。
- `python scripts/eval_offline.py`：**50/50**。
- 兜底能力 safeguards **23/23**：invalid_qty **8/8**、unknown_order **7/7**、injection **8/8**。
- 管道回归 pipeline regression **27/27**：alias **9/9**、ambiguous_wording **9/9**、multi_line_change **8/8**、golden **1/1**。

离线评测衡量给定 fixture 答案后的管道/内核处理与兜底能力，**不衡量模型理解**。历史 live 记录单列于 [LIVE_EVAL](docs/LIVE_EVAL.md)，不计入本次验证。当前 CI 只跑 lint + test，typecheck / cov / offline eval 仍为待授权步骤。
