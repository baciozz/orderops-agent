# S3 单/双角色对照结果（v1）

本文是结果占位，还没有真实调用，也没有对照数字。正式结果须在预登记协议 [AGENT_COMPARISON_PREREG_V1.zh-CN.md](AGENT_COMPARISON_PREREG_V1.zh-CN.md) 合并之后、按该协议开跑，再把脱敏摘要写入 `evals/agent_comparison_v1/results/`。

这是 120 条合成样本上的开发期诊断，`formal_qualified=false`，不是正式准确率，也不能当作生产性能或发布门槛。参考答案由小八复核，不是 Qi 人工复核。S3 全部组在 `model_max_tokens=3000` 下重测，不得与 S2B 在 1000 上限下测得的 100/120（83.3%）直接对比。

各组、各次重复的原始/规则后准确率、危险错误、转人工召回（⑤、⑫、合并 20）、静默忽略、应澄清召回、不必要澄清（按类）、分歧率、`MODEL_INCOMPLETE` 次数、费用与墙钟，待跑完后按预登记口径填写。分母 ≤20 的指标只列数字，不下结论；总体指标差异不足 3 条或三次方向不一致时标「无结论」。

运行摘要将写入 `reports/agent_comparison_v1/summary.json`（该目录不入库）。续跑状态默认写在同目录的 `state.json`，也可用 `--state` 另指定。共享账本为仓库外的 `/workspace/oos-ledger/ledger.db`（S2B 与 S3 共用；含已重建的 S2B 全量与冒烟，累计 $0.046117）。本文件在采集完成前保持为空结果。
