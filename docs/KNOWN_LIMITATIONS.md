# 已知限制

Stage 3 在 Stage 2 的持久执行基础上，加入静态文件切分、安全上传/导出、任务来源隔离、DB 任务视图及聊天/表格数量冲突检查。理解层仍只查精确 fixture，不调用 live 模型。库存和下游数字由内核重算。

- Worker 由 `worker-once` / Python 调用驱动，没有常驻调度服务；outbox 默认消费者只写本地去重回执，浏览器实时推送尚未接入。R01–R09、R16 有确定性故障测试。
- 上传为 `POST /sources` 的 multipart `task_id` + `file`，一次一个文件，每任务最多三个；任务须已存在。服务端生成存储路径，通过 `task_source` 关联限制 spans 查询，原始文件放在服务端临时目录，生产持久存储/清理策略尚未实现。拒绝返回 HTTP 400、parser_status=rejected 和中文原因，不保存部分 spans。
- UTF-8 TXT/MD 按非空行、静态 XLSX 按单元格切分；spans 摘要最多 500 字，完整原文独立保存。XLSM、VBA/宏类型、公式、外部链接和 XML 实体被拒绝；XLSX 有压缩包大小/压缩比及非空单元格上限。上传不会触发调度、批准或外传。
- R14 已接入 worker 的 `source_ids`：只读取当前任务关联的静态 spans，精确 fixture 聊天与匹配表格行数量不同时，保存 conflicts、questions 和聊天/单元格双来源，返回 NEEDS_CLARIFICATION，不创建计划、批准或提交。JSON 和 HTML 可展开来源并显示问题；抽取结果保存在当前事件的 InputPayload 中，原 text/source_ids 保留。
- 数量比较只支持第 1 行为明确列名的 XLSX：改单为 `订单号 / 原SKU / 目标SKU / 数量`，新建为 `客户 / SKU / 数量`，身份值须与 fixture 完全一致；数量为非负整数。不匹配行、不支持的布局或无效数量先澄清。任意自然语言、别名/模糊 SKU、上传单独生成业务意图和通用表格解析仍未实现；未显式选择的上传不参与本次比较。R10 仍验证上传无副作用及越权文本在 fixture worker 中被拒绝。
- 最小黄金链由 `test_fixture_golden_chain_upload_chat_precheck_approve_commit_export` 覆盖：`POST /inputs` 发送 `先上传订单表，再核对聊天数量` → worker 等待上传 → `POST /sources` → 同任务 `POST /tasks/{id}/answers` 带完整 fixture 文本和 source_ids → worker 预检 → 批准/提交 → 带来源的任务视图和下载。它是受控 API/fixture 演示。
- DB 任务视图已有调度、差量、下游、来源、批准过期置灰和 CSV/XLSX 下载，下载清单标注 FIXTURE。完整聊天入口、任务列表、澄清/批准按钮的浏览器提交交互和独立审计报告留待下一波；当前批准按钮只展示可批准状态，操作仍用已有业务 API。T-STUB 保留为演示桩。
- 对照的旧/新数量取计划基础版本和已提交版本；锁定量、可用量及下游状态来自当前业务事实，不是完整历史快照。计划因当前业务条件不可计算时不生成猜测差量。
- live 模型的 HTTP 传输。预算熔断和「缺单价就保持 FIXTURE」已经可用（R22、R23），但没有提供方，也不会发出网络请求
- ORM relationship、生产认证、CSRF、多用户。Alembic 初始迁移已经接入。
- `docs/TEST_REPORT.md`、`HANDOFF.md`、`LEARNING_GUIDE.md` 留到阶段 5。现在的测试清单是 `docs/TEST_CHECKLIST.md`

禁止用途：连接真实客户、ERP、支付、采购或发货；把 fixture 成功说成模型理解；在单价未知时打开 live；把 `T-STUB` 当成业务结果。

56 个 B/R 用例均已接入真实测试，当前没有占位跳过；这不代表完整 UI 或 live 理解已经实现。
