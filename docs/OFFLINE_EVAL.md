# 离线合成评测（FIXTURE）

本评测固定为 **FIXTURE**：50 条中文合成输入、逐条写出的 golden parser
payload，测试真实 dispatcher、schema 校验、库存/下游预检和 kernel。
不调用模型，不发送 HTTP，不读取 `.env`，不需要凭据。

```bash
.venv/bin/python scripts/eval_offline.py
.venv/bin/python scripts/eval_offline.py --out /tmp/oos-offline.json
make eval-offline PY=.venv/bin/python
```

默认只向 stdout 输出类别表格及失败明细；`--out` 另外写 JSON 报告。
JSON 包含 `mode: "FIXTURE"`、评测局限说明、逐条实际状态/错误码/计划 diff、
Approval/Operation/Outbox 行数增量、类别通过率及失败原因。
`groups` 提供组名称、中文标签、类别列表、pass/fail/known_issues/pass_rate 小计，
保留原有 `categories` 等字段。
任何没有标注 `known_issue` 的失败使退出码非零；`known_issue` 仍计为失败，
不把已知问题算成通过。

## 数据和范围

数据在 [`evals/offline_cases.json`](../evals/offline_cases.json)。每条包含唯一
`id`、`category`、`input_text`、`expected` 和完整的 `fixture_payload`。
`product_question` 是可选的产品问题注释字段。
这里选择现有 fixture 的 `ExtractionResult` 结构（含 `business_event`），
直接通过 `dispatch_text(..., fixtures={完整输入: 显式payload})` 注入
`FixtureLLMClient`；没有关键词/正则“理解”，没有改变原有默认 fixtures。
`client_factory` 在现有 dispatcher 中仅供 LIVE 路径使用，因此本脚本不使用该入口。

| 类别 | 内容 |
|---|---|
| multi_line_change | 多行增量/总量/改色、新建多SKU、整单拒绝及已执行部分澄清 |
| ambiguous_wording | 数量、单位、订单、颜色、尺码含糊和矛盾信息 |
| alias | 中号/M、大号/L、黑/白/蓝、藏青/海军蓝、只改未开裁 |
| invalid_qty | literal 0、-5、1.5/2.5；覆盖改单及新建 |
| unknown_order | payload 保留不存在的订单号（如 SO-9999），期望 NOT_FOUND |
| injection | 外传、忽略规则、伪造 approved、单价、fence、plan_hash |
| golden | 原黄金现货改色：预检 → 人工批准 API → 提交 API → 修订2 |

先用真实 Alembic migration 和 seed 生成临时 SQLite 模板，关闭连接后为每条
case 复制一个独立数据库。没有状态串扰；整个评测通常约 2–5 秒。
所有待批准用例检查 kernel 存储的规范化变更与显式 `diff_summary` 一致。
所有非 golden 用例检查没有 Approval/Operation/Outbox 新增，且订单行、
库存、预占、生产单和采购单未被修改。golden 检查真实批准/提交结果和修订2
订单行：A-BLACK-M 60、A-WHITE-M 40、C-BLACK-L 300。

脚本复用 `live_smoke._assert_safe`：注入不能增加 Approval/Operation/Outbox，
所有存储字符串/JSON 不得含密钥模式，outbox 不得含 URL；另外扫描全部表的
存储值，拒绝任何外部 HTTP(S) URL。SAFE 可以是合法业务部分进入待人工批准，
但不能由模型批准或提交。六条带非法额外字段的答案应被 schema 拒绝。

本评测入口是 dispatcher，不包含 inbox/UI 原始输入持久化或上传文件流程。
fixture 的来源引用为空，因此不会把用户输入中的攻击 URL 作为原始证据存入库；
外传字段仍以原样恶意 payload 交给 schema 拒绝。这里的全表 URL 断言不能用来
推断 inbox 对原始用户证据的存储策略，也不能证明模型会拒绝输出这些字段。

网络 guard 在执行前安装，外部 DNS、TCP connect/connect_ex、UDP sendto 直接
抛错；仅允许 loopback/localhost。设置显式使用 `_env_file=None`、
`live_enabled=False`，清空模型/凭据/价格字段，并检查 `resolve_mode` 必须为
FIXTURE。测试额外禁止创建任何 socket，并验证网络 guard 在系统调用之前拒绝外部地址。

## 最新结果（FIXTURE，2026-11-03）

### 兜底能力（safeguards）

| Category | Pass | Fail | Known issues | Pass rate |
|---|---:|---:|---:|---:|
| invalid_qty | 8 | 0 | 0 | 100.0% |
| unknown_order | 7 | 0 | 0 | 100.0% |
| injection | 8 | 0 | 0 | 100.0% |
| 小计 | 23 | 0 | 0 | 100.0% |

### 管道回归（pipeline regression）：不代表模型理解准确率

| Category | Pass | Fail | Known issues | Pass rate |
|---|---:|---:|---:|---:|
| alias | 9 | 0 | 0 | 100.0% |
| ambiguous_wording | 9 | 0 | 0 | 100.0% |
| multi_line_change | 8 | 0 | 0 | 100.0% |
| golden | 1 | 0 | 0 | 100.0% |
| 小计 | 27 | 0 | 0 | 100.0% |

合计 **50/50**，当前没有 `known_issue`。0/负数返回 BUSINESS_CONFLICT，非整数返回
SCHEMA_INVALID，不存在订单返回 NOT_FOUND。未修改生产代码或业务检查。

## 诚实说明：这不是模型理解能力分数

**FIXTURE 仅测试“给定结构化答案时，pipeline + kernel 是否正确处理”。**
尤其 alias、ambiguous_wording、multi_line_change 的通过率不能证明模型懂同义词、
能识别含糊措辞或能拆解多项改单。答案是人工显式写出的；这些分数也不是模型
对攻击的抵抗率。恶意字段用例测的是 schema/kernel 边界，而非模型拒绝率。
这里没有运行 `to_extraction` 的 live intent 转换路径；其严格 schema、来源绑定和
澄清逻辑继续由现有 `test_stage4_live.py` 的离线测试覆盖。

所有 14 个 `live_smoke.SAMPLES` 原始输入均被包含。live_smoke 顺序运行，
而这里逐条使用原始 seed：`alias-remaining` 的白色订单行尚不存在，故这里正确
期望 NEEDS_CLARIFICATION；live 在 golden 已提交后期待 AWAITING_APPROVAL。
其它已有输入按本评测中显式提供的 parser 答案检查，不声称复现 live 模型输出。

真实模型理解结果只见 [`docs/LIVE_EVAL.md`](LIVE_EVAL.md)（14 条 live 样本），
本表不可与 live 结果合并计分。扩展数据时，如果观察到不安全行为，请保留原始
payload，写明 `known_issue`，让报告保持失败；不要为通过评测修改生产规则。

兜底能力（safeguards）覆盖非法数量、不存在订单及注入的 schema/kernel 边界。
管道回归（pipeline regression）覆盖显式结构化答案下的别名、含糊措辞、多行变更和黄金链。
管道回归不代表模型理解准确率，不得表述为模型 100% 准确率（如简历）。

待 Qi 确认的产品问题：服装批发里把某行改成 0 可能表示取消这一行。
当前 qty=0 仍按 BUSINESS_CONFLICT 拒绝，评测仅锁定当前行为，不调整业务规则；
负数返回 BUSINESS_CONFLICT，小数返回 SCHEMA_INVALID。改单和新建的零数量样本
均带有 `product_question` 注释；是否支持取消行及适用操作需另行确认。
