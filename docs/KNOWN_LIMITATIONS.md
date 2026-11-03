# 已知限制

Stage 3 本波在 Stage 2 的持久执行基础上，加入静态文件切分、安全上传/导出、任务来源隔离和 DB 任务视图。理解层仍只查精确 fixture，不调用 live 模型。库存和下游数字由内核重算。

- Worker 由 `worker-once` / Python 调用驱动，没有常驻调度服务；outbox 默认消费者只写本地去重回执，浏览器实时推送尚未接入。R01–R09、R16 有确定性故障测试。
- 上传为 `POST /sources` 的 multipart `task_id` + `file`，一次一个文件，每任务最多三个；任务须已存在。服务端生成存储路径，通过 `task_source` 关联限制 spans 查询，原始文件放在服务端临时目录，生产持久存储/清理策略尚未实现。拒绝返回 HTTP 400、parser_status=rejected 和中文原因，不保存部分 spans。
- UTF-8 TXT/MD 按非空行、静态 XLSX 按单元格切分；spans 摘要最多 500 字，完整原文独立保存。XLSM、VBA/宏类型和公式被拒绝；XLSX 有压缩包大小/压缩比及非空单元格上限。上传不会触发调度、批准或外传。
- worker 的理解输入仍需完整 fixture 文本；尚未把上传 spans 接入业务抽取或聊天/表格数量冲突流程（R14），不支持任意自然语言理解。R10 的真实测试覆盖上传无副作用及同一越权文本在 fixture worker 中被拒绝。
- DB 任务视图已有调度、差量、下游、来源、批准过期置灰和 CSV/XLSX 下载，下载清单标注 FIXTURE。完整聊天入口、任务列表、澄清/批准按钮的浏览器提交交互和独立审计报告留待下一波；当前批准按钮只展示可批准状态，操作仍用已有业务 API。T-STUB 保留为演示桩。
- 对照的旧/新数量取计划基础版本和已提交版本；锁定量、可用量及下游状态来自当前业务事实，不是完整历史快照。计划因当前业务条件不可计算时不生成猜测差量。
- live 模型的 HTTP 传输。预算熔断和「缺单价就保持 FIXTURE」已经可用（R22、R23），但没有提供方，也不会发出网络请求
- ORM relationship、生产认证、CSRF、多用户。Alembic 初始迁移已经接入。
- `docs/TEST_REPORT.md`、`HANDOFF.md`、`LEARNING_GUIDE.md` 留到阶段 5。现在的测试清单是 `docs/TEST_CHECKLIST.md`

禁止用途：连接真实客户、ERP、支付、采购或发货；把 fixture 成功说成模型理解；在单价未知时打开 live；把 `T-STUB` 当成业务结果。

跳过的测试不是通过。尚未实现的 B/R 用例在 pytest 里会显示跳过原因和计划阶段。
