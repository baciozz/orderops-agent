# 单次提取与规则核对的配对记录

`benchmarks/review_baseline.py`已提供最小观察适配层。它在真实`service.extract`外包装操作者提供的客户端，同一次模型响应分别保留原始提案与工作区实际规则结果，不请求第二次模型。客户端仍使用原来的同轮费用账本、次数和超时限制；适配层不读取凭据、不创建提供方、不开新预算、不自动重试、不选样本、不读取参考答案。

隔离评测任务可以在已确认原件/快照的新合成工作区中使用：

```python
from benchmarks.review_baseline import run_once

record = run_once(
    sessions, settings, review_id, extract_request,
    client=existing_budgeted_client,
    metadata={
        "code_sha": exact_clean_checkout_sha,
        "dataset_sha256": frozen_dataset_sha256,
        "evidence_kind": "real_provider",  # 本机模拟必须填mock_transport
        "sample": opaque_sample_id,        # 仅写结果，不加入模型输入
        "split": "development",
        "repetition": 1,
    },
)
```

每次重复必须新建工作区，不能复用幂等回放或人工纠正的答案。元数据SHA由执行者核实，不是脚本对Git或提供方身份的独立认证。样本标识、参考、split及评分不能加入模型输入。原始提案、规则结果、提示/输入/schema摘要、实际模型账本、计时与失败分别保存；并发取消后的迟到输出仍作为原始输出记录，但不会冒充被工作区接受的结果。

保存为JSONL后，用模块入口离线汇总：

```bash
python -m benchmarks.review_baseline --records private-records.jsonl --output new-summary.json
```

汇总拒绝重复工作区、不同代码/数据/真实与模拟分类、模型或提示配置混合。全部尝试进入分母，保留超时、格式/Schema错误、HTTP失败和取消丢弃；费用包含失败的悲观预留，账本不完整明确标记。不把同一次提取的费用在两个观察分支重复相加。

计时用单调时钟，分别提供客户端调用与服务提取总耗时的p50/p95。服务提取含准备、持久化及规则，不是浏览器端到端时长；上传/OCR、规则独占耗时、人工等待及下载尚未独立测量，字段为null，不以相减残差冒充规则耗时。没有真人复核真值时，accuracy和quality保持null；ready、blocked、clarify是行为计数，不是正确率。真人确认后的完成质量也不能覆盖模型原始错误。

该工具衡量单次结构提取加确定性规则的取舍，不实现第二个模型Agent，也不提供多Agent速度/质量优势。若后续统一执行真实基线，仍需固定模型、输入/OCR、代码和数据版本，按格式/业务类别评分、交错重复，记录上传与浏览器总时长；当前正式保留集、真人真值和G3门槛仍未通过。

本次适配层用实际服务与预算账本、合成MockTransport运行9项回归：正确/被规则拦截的提案、超时、坏JSON、Schema错误、HTTP失败、取消迟到、禁止重放/人工答案混入、混合证据拒绝及零预算先于传输拒绝。它们验证记录机制，不是8次真实模型评测；本开发实例没有新增付费调用。

调用计数仅表示structured_call的客户端尝试，不是独立观测的HTTP发送数；预算拒绝可有一次客户端尝试而没有任何传输/账本预留。因此transport_requests保持null，不能把尝试次数写成真实API次数。
