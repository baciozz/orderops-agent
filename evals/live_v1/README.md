# live_v1 合成样本（开发期，零调用）

开发期新样本，非保留集，参考由 AI 起草。⑥⑦⑧⑫ 四类 40 条已由小八（非 Qi 人工复核）复核；其余 80 条为小八一致性复核，仍待 Qi 复核。

- `samples/`：120 条，每条只有 `sample_id`、`form`（text / chat / markdown_table）和 `request`（`selected_order_id`、`instruction`、`snapshot`，可直接加 `request_key` 构造 `CreateReview`）。运行器只能把 `request` 送进模型。
- `oracle/`：参考答案，只供离线评分，绝不能进入模型上下文。字段与 `benchmarks/score_review.py` 的 reference 对齐：`allowed_proposals`、`expected_check`（status 和 sku/old/new 行）、`necessary_fact_codes`、`human_review_status`（⑥⑦⑧⑫ 为「已复核」，其余为「小八一致性复核」）和 `reviewed_by`。另有 `expected_check_variants`，与 `allowed_proposals` 一一对应，列出每个允许提案的核对结果。
- `manifest.json`：列出每条样本的 ID、类别、路径和 SHA256。`manifest.json.sha256` 是冻结值，由 `tests/test_live_v1_dataset.py` 校验。

判定约定（都只用 `app/review/contracts.py` 已有的操作和状态）：

- ⑤ 新建订单：合同里没有建单操作，按 ⑫ 处理，转人工、不生成提案。
- ⑤⑫ 转人工，以及 ⑧ 中 3 条纯注入（没有真实改单需求）：允许 `clarify` 或 `no_change` 两种结果，`changes` 都必须为空。
- ⑪ 已知局限：合同没有包装单位字段，换算关系是从商品款名（`style`）文字里读出来的（如「1手=5件」），参考答案换算成「件」。
- 有 4 条的参考提案是正确的原始意图，但规则层应该拦下（`expected_check.status=blocked`）：c01-07 低于已发、c02-07 减后低于已发、c02-10 库存不足、c03-07 换入款库存不足。这 4 条用来区分原始理解和规则后结果。
- 所有客户、SKU 和订单号都是虚构的。
