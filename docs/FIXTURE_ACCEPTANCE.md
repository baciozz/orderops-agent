# 三条合成订单验收

前两条旅程证明固定答案后的 FIXTURE 管道，第三条证明显式合成输入的库存新单服务生命周期。脚本会**模拟 demo-user 批准并提交**，不是业务人员签字，不连接模型、浏览器、供应商或 ERP。请先阅读预期结果，再运行。使用当前候选分支的源码；README 中固定 main SHA 的历史结果不等于候选分支已经合并或发布。

## 一条命令，三个独立新库

需要 POSIX 系统（Linux/macOS）、Python 3.11+、完整源码及已安装的项目依赖（含 httpx/openpyxl）。从仓库根目录运行；使用当前完整源码（可 editable 安装），保留 alembic.ini 和 order_ops_migrations/。普通解包 wheel 支持运行时迁移，但不包含本源码验收工具链。脚本不安装、下载或重建依赖。

```bash
python scripts/accept_fixture.py --output ./fixture-acceptance-01
```

output 必须不存在，父目录必须存在，且不能位于 app/、schemas/、seeds/、order_ops_migrations/ 运行源码目录内。第二次运行改用新名称，例如 fixture-acceptance-02；已有目录、文件或同名符号链接一律拒绝，不覆盖证据，不删除或重置默认数据库。不接受数据库 URL 或已有库。输出目录所在位置应由你控制，不支持敌对进程同时替换路径。

脚本以干净环境启动隔离的 Python 控制进程，每条旅程创建自己的临时 SQLite 库。父启动器不导入应用；控制进程和 CLI 子进程在导入 app.main/app.cli 前禁用 dotenv、清除继承配置、固定 FIXTURE 和新库。不会读取你的 OOS 参数、密钥、代理或用户 HOME。已安装的 Python/依赖必须可信；这不是抵御解释器启动钩子、恶意依赖或所有操作系统文件写入的沙箱。应用阶段有 socket 网络/监听拦截，不启动服务。

三条旅程的 init-db、seed，以及前两条的 worker-once 都是单独的 CLI 子进程，调用 app.cli.main（等效 CLI 命令而非内嵌 worker 函数）。单次子进程最多 30 秒，整个流程最多 180 秒。超时或 SIGINT/SIGTERM 会终止控制进程及其 CLI 子进程组。正常结束清理自己创建的临时库，保留结果；强制中断可能留下不完整临时库，不自动重用或删除这些失败证据。控制进程失败或超时返回非零，保留已有输出，不发布成功摘要。SUMMARY.md 只在控制进程成功后写入，证明合成检查完成，不承诺操作系统中断时的原子性或最终控制台输出送达。摘要写入失败或中断仍可能留下部分文件，须同时查看命令退出状态、摘要和 run.log；不要把部分结果当成通过。用另一个新 output 重试。

## 旅程一：只改未发库存

固定输入，不要改写：

```text
SO-1001 把未发的 40 件黑色 M 改成白色 M
```

持久化输入返回 event/task ID，单独 worker 规划到 AWAITING_APPROVAL。脚本先检查订单 revision 1 和业务表未变，再模拟「批准当前计划」，确认批准本身仍不改业务，然后单独「提交生效」。

预期：A-BLACK-M 100 → 60，已发 60 不动；A-WHITE-M 0 → 40。C-BLACK-L 300 不变，其中已裁 120 不变。黑/白预留分别 -40/+40；四张下游单据内容不变，仅重新关联版本。

## 旅程二：拒绝全改，明确恢复

在另一全新 seed 库选择 SO-1001，固定输入：

```text
C 款黑色 L 全部改藏青
```

首次 worker 必须进入 NEEDS_CLARIFICATION，指出已裁 120 不能改色，只有未执行 180。明确回答：

```text
只改未开裁
```

回答使用当前 intent revision 和原始输入 event ID 作 fence；重复/过时页面不能悄悄覆盖新意图。再运行一次 worker，得到新计划，旧澄清计划不批准。脚本通过 GET /tasks/{id} 的 JSON plans 列表选择唯一计划；恢复时排除已记录的旧计划 ID，断言只剩一个，不猜最新、不抓 HTML。

预期：C-BLACK-L 300 → 120，已裁 120 保留；C-NAVY-L 0 → 180。A-BLACK-M 100（已发 60）不变。MO-2001/CUT 黑色 120 不变，MO-2002/NOT_STARTED 改藏青 180，已收料 PUR-3001 不变。PUR-3002 的 FAB-NAVY 360 为 **PENDING_SUPPLIER** 草案，需要另行供应商确认；脚本没有联系供应商，也没有证明对方接受。

## 旅程三：新建库存单，再改单与历史重放

第三个全新 seed 库只用显式合成数据，直接调用现有 plan_new_order / plan_change、approve_plan、durable_commit、同步订单读回与导出服务。它不经过 /inputs、上传解析、文本提取或 worker，也不新增新单 API。与前两条一样，运行在禁用网络和 dotenv 的隔离控制进程内，并受同一超时、进程组清理及 SOURCE.json 指纹保护。

1. 先尝试白色库存 8 + 藏青 MTO 6 的混合新单，必须返回 MTO_CREATE_UNSUPPORTED，订单、库存、预留、执行单据和操作记录不变。这不是新增 MTO 创建功能。
2. 客户 HD 新建 A-WHITE-M 8 + B-BLUE-L 6 + A-WHITE-M 2，重复 SKU 合并为白色 10、蓝色 6。规划后业务表未变；相同输入 key 重放返回原计划且全部表不变。
3. 单独模拟批准，再单独提交。批准本身不改业务；提交后订单 OPEN、revision 1、SUCCEEDED、verification passed，预留为白色 10、蓝色 6。保存 CSV/XLSX/audit。
4. 把白色 4 换为黑色 4，再增加蓝色 3。重新规划、模拟批准和提交，订单 revision 2 为白色 6、黑色 4、蓝色 9，预留完全一致。原 seed 的生产、采购、发货记录不变。
5. 创建和改单各自同 key 重放均返回原 Operation ID，所有表不变。改单后再重放原创建提交，仍返回原 revision 1 收据，当前订单保持 revision 2，不回滚、不重复扣库存。
6. 再导出原创建任务，历史差量仍是白色 10、蓝色 6；改单任务是白色 6、黑色 4、蓝色 9。导出来自任务当前视图，其中计划差量保留历史、库存及核验具有当前事实语义，**不是完整的历史库存快照**。不要把旧任务导出当作当前订单行。脚本还断言原创建导出的可用库存从白110/蓝14变为改单后的白114/蓝11，避免误把历史差量当成冻结库存。

来源证明仅检查显式合成 source_id 和 Orders!B2:C4 在任务视图中保留，不是实际文件上传、提取质量或上传归属验证。审计按自身 allowlist/归属规则导出，不保证包含这些未上传的引用。

## 看哪些文件

- SOURCE.json：本地运行相关 Python/网页资源、runner 与明确配置/锁文件的 SHA-256 和 Python 版本；拒绝该范围内的符号链接，不读取真实 .env。它不覆盖全部仓库、docs/tests/evals 或已装依赖，也不声明远端分支身份。
- SUMMARY.md：只有三条旅程全部成功才生成，列出业务结果与限制。
- stock-result.json / uncut-result.json：输入与回答、task/event/plan ID、批准和提交请求、同 key 重放、前后订单、只读业务表证据、FIXTURE 视图、实际 CLI/HTTP 步骤。
- stock-audit.json / uncut-audit.json：每条旅程恰好一个 committed operation。虽然审计 actor_type 为 human，本脚本的批准是模拟，不可当作真实授权证明。
- 原有两组 changes.csv / changes.xlsx：脚本解析 CSV 并打开 XLSX 检查。它们是当前视图，不是冻结的批准时库存；audit 自带历史、来源和核验语义限制。
- new-stock-result.json：创建/改单输入、计划与模拟批准/提交请求、两次订单读回、预留、业务表快照、重放与混合新单拒绝证据。
- new-stock-create-* / new-stock-amend-* / new-stock-create-after-amend-*：三组 changes.csv、changes.xlsx 和 audit.json；最后一组是改单后重读原创建任务。各审计只有该任务的一次 committed operation。
- run.log：包含「批准前业务未变」检查；失败时查这里，不删除原证据。

前两条旅程最终都是 SUCCEEDED、revision 2、verification passed；新单创建为 revision 1，后续改单为 revision 2；重放**原** approval ID、plan hash 和幂等 key 返回同一 Operation ID，不能换 key 盲重发。IDs 和时间戳每次不同，数量、保护边界和单操作结果相同。没有上传流程、真实浏览器交互、模型理解质量或部署就绪结论。

## 网页验收仍需单独做

当前页面已实现回答、上传、取消、批准和提交入口。真实浏览器 JavaScript、刷新、重复点击、网络中断尚未在本交付验证；API/TestClient 和 CLI 成功不能替代浏览器验收。以后手动使用页面时，照上述原文与数量核对，不要用正在处理真实业务的库演示。
