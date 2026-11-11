# 无需模型的合成清单示例

这条路径在本机用预填合成资料和人工录入演示清单；不使用模型、ERP、真实客户数据或已登记的 fixture 答案。安装依赖可能需要网络，**演示运行本身不需要外网或密钥**。页面确认的是处理清单，不提交订单、不预占库存。人工操作和勾选不能作为模型理解能力或识别准确率。

## 1. 安装并准备独立目录

先按 [README](../README.md) 安装源码和锁定依赖。以下示例适用于 Linux / POSIX shell，须从安装源码的根目录开始；如使用普通 wheel，将 `OOS_DEMO_PY` 改成该 wheel 虚拟环境 Python 的绝对路径。普通 wheel 不包含本文或回滚脚本，请同时保留私有交付包中的说明和工具。已验证 Linux / Python 3.12；其他系统未完整验收。

```sh
OOS_DEMO_PY="$(pwd -P)/.venv/bin/python"
OOS_DEMO_DIR="$(mktemp -d /tmp/order-ops-preview.XXXXXXXX)"
if [ -n "$OOS_DEMO_DIR" ] && [ -d "$OOS_DEMO_DIR" ] && [ -x "$OOS_DEMO_PY" ] && cd "$OOS_DEMO_DIR"; then
  printf '本次合成演示目录：%s\n' "$OOS_DEMO_DIR"
  env -i PATH=/usr/local/bin:/usr/bin:/bin LANG=C.UTF-8 \
    OOS_DATABASE_URL="sqlite:///$OOS_DEMO_DIR/review.db" OOS_LIVE_ENABLED=false \
    "$OOS_DEMO_PY" -I -m app.cli init-db && \
  env -i PATH=/usr/local/bin:/usr/bin:/bin LANG=C.UTF-8 \
    OOS_DATABASE_URL="sqlite:///$OOS_DEMO_DIR/review.db" OOS_LIVE_ENABLED=false \
    "$OOS_DEMO_PY" -I -m uvicorn app.main:app --host 127.0.0.1 --port 8000
else
  printf '演示目录或已安装的 Python 不可用；请检查安装步骤。\n' >&2
  false
fi
```

每步应成功后再继续。新目录默认仅当前用户可访问且没有 `.env`；从该目录启动并清空继承环境，避免加载源码目录的配置或凭据。`-I` 不影响已安装在该虚拟环境的项目。不要在已有业务库上初始化，不需要执行 `seed`。端口占用时先确认原因，可换空闲端口并同步修改浏览器地址，不随意结束其他服务。

## 2. 用预填示例生成清单

1. 打开 <http://127.0.0.1:8000/ui/reviews>，点击“开始导入”。保持预填订单 `SYN-2401`、JSON 快照和客户需求不变，点击“保存资料，不执行订单”。这一步不需要上传文件。
2. 在任务页点击上方“人工输入/纠正”。在已有的一项动作里选择原 SKU `00123 · 藏蓝 · M`，数量语义选“总量改成”，数量填 `36`，填写例如“核对合成示例”的理由，点击“保存全部人工动作并重新核对”。不需要点击真实模型提取。
3. 核对一行结果：原数量 **50 件**、新数量 **36 件**、差量 **−14 件**。合成快照中已发为 **10 件**；这份处理清单不会修改已发记录或执行订单。提案来源应标为人工。
4. 点击“确认这份处理清单（不提交订单）”，再下载 CSV 和 XLSX。下载文件也应显示相同数量和“未执行订单”。

若页面提示需先解决约束，保留当前资料并核对是否更改过预填快照、订单、SKU、数量或语义；不要把未知数量改成 0 来消除提示。该示例无需图片识别，其他文件仍须按原件逐项核对。

## 3. 停止和继续

在运行服务的终端按 Ctrl+C。需要继续查看时，在同一终端和同一 `OOS_DEMO_DIR` 下重新执行上面的 uvicorn 命令，不必重新初始化；从清单列表打开已保存任务。临时目录可能被系统清理，若要长期保留，请在运行前选择新建的私有持久目录并按[离线备份说明](REVIEW_OFFLINE_BACKUP.zh-CN.md)保存，不要在线移动原件或手改数据库路径。

这是本机单用户开发预览，未提供身份认证或多人授权，不要对外监听。完整模型评测、独立真人试用和真实业务验收仍未完成；更多边界见[当前限制](LOCAL_BETA_LIMITATIONS.zh-CN.md)。
