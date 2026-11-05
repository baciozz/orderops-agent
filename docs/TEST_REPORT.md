# 固定基线测试报告

## 证据范围

本报告对应私有仓库 `baciozz/order-ops-superagent` 的固定提交
[`20836d7bcc90471bb2b3beb9057f11a1349f09e9`](https://github.com/baciozz/order-ops-superagent/commit/20836d7bcc90471bb2b3beb9057f11a1349f09e9)。
复验日期：2026-11-05 UTC。后续修改必须另行复验，不能沿用本表宣布候选版本全绿。

使用 GitHub 提供的 167 个跟踪文件构建源码快照，测试前后核对 Git blob SHA 与远端树一致；该快照没有 Git 历史。
环境为 Linux、Python 3.12.14，独立虚拟环境，按 `requirements-dev.lock` 安装，再以 `--no-deps` 安装本项目。
全部运行使用合成数据和 FIXTURE/MockTransport，没有调用真实模型、连接 ERP、发出业务通信或部署服务。

## 可复现命令

在该提交的新副本中运行：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.lock
.venv/bin/python -m pip install --no-deps -e .
make PY=.venv/bin/python check
make PY=.venv/bin/python eval-offline
```

本次日志中的解释器是隔离环境的绝对路径，等价于上述 `.venv/bin/python`。
测试使用独立临时数据库，不以演示库的当前数据作为预期结果。

## 实际结果

| 检查 | 固定基线结果 | 证据 |
| --- | --- | --- |
| Ruff lint / format | 通过，127 个文件格式不变 | [完整 check 日志](evidence/baseline-20836d7-check.log) |
| mypy | 通过，64 个源文件 | 同上 |
| pytest / coverage | 922 通过，0 失败；95.13%，4868 条语句、237 未覆盖；门槛 90% | 同上 |
| 离线评测 | 53/53：safeguards 23/23，pipeline 30/30 | [完整 eval 日志](evidence/baseline-20836d7-eval.log) |
| 远端 push CI | 成功；只含 lint/test | [该提交的 CI](https://github.com/baciozz/order-ops-superagent/actions/runs/42079766991) |

pytest 报告一个 Starlette TestClient 关于 httpx/httpx2 的弃用警告；本次保留锁定依赖，没有以隐藏警告代替解决。
历史 README 中的 912/95.06% 是较早开发验证记录，不能替代合并后固定基线的 922/95.13%。

## 如何理解这些数字

- [TEST_CHECKLIST](TEST_CHECKLIST.md) 给出 56 个业务/运行 B/R 验收场景的映射。922 是测试展开后的执行数量，不是 922 种业务需求。
- 离线评测向管道提供明确 fixture 答案，验证预检、批准、原子提交、恢复和安全边界，不测自然语言理解准确率。
- TestClient 验证页面响应、字段、按钮状态及 API 链路，不执行浏览器 JavaScript；不能据此宣布浏览器端完整通过。
- 95.13% 是行覆盖率，不是业务正确率，也不是所有崩溃时序已覆盖。

## 未执行和未证明

本次未执行真实 live 评测、真实浏览器点击、移动端/跨浏览器验收、生产认证、多用户授权、真实 ERP 联调、生产负载或备份恢复演练。
历史小样本真实模型结果仅见 [LIVE_EVAL](LIVE_EVAL.md)，不属于本次复验。
待授权 CI 定义仍在 `ci/pending-workflow-changes.yml`；远端 CI 绿不等于完整 `make check` 和离线评测都在 CI 执行。
