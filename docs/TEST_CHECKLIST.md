# 测试清单

可执行清单是 `tests/acceptance_cases.py`。当前 56 个 B/R 用例均标记实现，PENDING 为空；跳过不计为通过。

| 范围 | 计划阶段 | 当前 |
|---|---|---|
| B01–B18、B28–B32 | 1A | 通过 |
| B19–B27 | 1B | 通过 |
| R01–R09、R16 | 2 | 通过 |
| R10–R13 | 3（本波） | 通过：注入文本只作数据、静态上传边界、安全导出、任务来源隔离 |
| R14 | 3（wave 2） | 通过：真实上传 + 聊天/source_ids + worker，改单/新建数量冲突均等待确认，双来源持久化且无计划/批准/提交 |
| R15 | 3（本波基础） | 通过：真实任务 JSON、HTML、下载清单均标明 FIXTURE；未调用 live |
| R18–R21 | 1C | 通过 |
| R24 | 3（本波基础） | 通过：字段契约、可控时钟、到期边界及过期后的 JSON/HTML 置灰 |
| R17 | 注册表失败与任务级 `CAPABILITY_MISSING` | 通过 |
| R22 | 方案在阶段 4；预算熔断和重启后的账本已实现 | 真实测试 |
| R23 | 方案在阶段 4；缺单价或密钥时保持 FIXTURE，健康检查和桩页面会标明 | 真实测试 |

另外还有不编号的测试：WAL 与 `BEGIN IMMEDIATE`、外键、seed 数字与幂等、命令行 seed、健康检查、任务视图字段、示例 JSON、上传上限、可用量和未执行量的定义。

阶段 5 的 `TEST_REPORT` 再按 56 条用例填写通过、失败、跳过和未执行原因。本文件不预填通过率。

Stage 3 真实用例在 `tests/test_stage3.py`；R10–R15/R24 均无占位跳过。
R11 覆盖 XLSM、伪装 XLSX 的 VBA/宏类型、所有工作表的公式（含字符串公式）、文件大小、工作表总数、跨表总行数、解压大小/压缩比和每任务文件数。
R15/R24 同时验证批准前后任务视图、下载标识和已提交结果的回读；完整交互界面仍见已知限制。
R14 的 `test_R14_chat_sheet_quantity_conflict_requires_confirmation` 覆盖改单数量低于/高于聊天、零数量和新建订单冲突，验证 conflicts/questions、聊天与具体单元格、JSON/HTML、原输入保留，以及零计划/批准/Operation/新增订单修订。
`test_fixture_golden_chain_upload_chat_precheck_approve_commit_export` 补齐上传→聊天/来源抽取→预检→批准→提交→下载的黄金链；相同数量可继续，无关订单行不会制造冲突。另测多文件冲突、不匹配订单/SKU、未知布局、重复数量列、无效/缺失数量及 worker 跨任务来源拒绝；输入时间相同时也保留最新抽取结果。

验证环境提示：本次沙箱禁止 socketpair.send，原有 FastAPI TestClient 也会挂起。
本波测试使用 `PYTHONPATH=/tmp/order-ops-pytest-runtime` 下的临时 asyncio pipe 唤醒补丁处理跨线程回调；仓库测试和断言未为此放宽，该补丁不属于应用或提交内容。
