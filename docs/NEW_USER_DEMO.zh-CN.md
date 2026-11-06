# 新用户：隔离安装与固定表格演示

这是本机合成演示操作说明，浏览器步骤尚未实机执行。需要 POSIX shell、Python 3.11+，建议使用本候选已验证的 Python 3.12。安装时可能访问包仓库；下述演示运行不调用模型。不使用真实业务库、密钥或客户文件，不公开监听、不使用代理隧道。

## 1. 确认版本，选择一种安装方式

先从本次已验证交付说明读取源码提交/分支、源码清单 SHA256、wheel 文件名与 SHA256，并逐项核对实际文件；本指南不把历史 PR 当作当前候选身份。版本号 1.0.0rc1 本身不足以区分候选，也不表示已合入 main 或正式发布。只有旧交付说明或缺少哈希时先停止，索取对应本次文件的说明。不要用旧 PR #37 替代当前交付。

普通 wheel 不安装本指南和源码脚本；请把指南、匹配的 requirements.lock 及已核验的合成原表随分发包保存。源码使用仓库内指南即可，无附件也可按下文生成同样单元格的输入。

源码安装：在上述完整源码根目录执行，保持源码目录不移动。

```sh
SRC="$(pwd -P)"
python3 -m venv .venv-demo
PY="$SRC/.venv-demo/bin/python"
"$PY" -m pip install -r "$SRC/requirements-build.lock" -r "$SRC/requirements.lock"
"$PY" -m pip install --no-deps --no-build-isolation -e "$SRC"
"$PY" -m pip check
```

这条路径同时使用锁定运行依赖与构建工具，并关闭 editable 构建隔离，避免另行解析未锁定的构建依赖。版本锁不是文件哈希供应链保证。开发测试另需 requirements-dev.lock；正常演示不需要测试工具。

普通 wheel 安装：在专门的新空目录中放入已核对的 wheel 和同候选 requirements.lock，执行以下命令。安装完成后不依赖源码 checkout。

```sh
DIST="$(pwd -P)"
python3 -m venv .venv-demo
PY="$DIST/.venv-demo/bin/python"
"$PY" -m pip install -r "$DIST/requirements.lock"
"$PY" -m pip install --no-deps "$DIST/order_ops_superagent-1.0.0rc1-py3-none-any.whl"
"$PY" -m pip check
```

离线安装须事先准备与 Python/平台匹配的全部依赖 wheels，在每条 pip install 命令上追加 --no-index --find-links /绝对路径/wheelhouse --only-binary=:all:（editable 的本地源码目标仍保留）；缺文件就停止，不临时放宽版本或下载未知来源。普通 wheel 没有 Makefile 和源码验收脚本，使用下面的通用 Python 命令。

## 2. 建立全新合成运行目录

继续在同一终端中操作，保留上一步 PY 的绝对路径。下述命令创建新目录，不重置既有库。不要把已有数据库复制进来。

```sh
DEMO="$(mktemp -d "${TMPDIR:-/tmp}/order-ops-demo.XXXXXX")"
mkdir "$DEMO/home" "$DEMO/tmp"
cd "$DEMO"
demo_python() {
  env -i PATH=/usr/bin:/bin HOME="$DEMO/home" TMPDIR="$DEMO/tmp" \
    OOS_DATABASE_URL="sqlite:///$DEMO/demo.db" OOS_LIVE_ENABLED=false \
    "$PY" -I -B "$@"
}
demo_python -m app.cli init-db
demo_python -m app.cli seed
```

命令必须均成功。干净运行目录不含 .env；清空继承环境、显式关闭 LIVE，并指定绝对数据库路径。不要在源码目录执行默认 make seed 代替此步骤。seed 不是重置命令，再执行不会把已改动的订单和库存恢复原样。

仅在允许启动本机服务的环境中，由操作者执行：

```sh
demo_python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

打开 http://127.0.0.1:8000/ui/demo/new-stock-sheet，启用 JavaScript。端口占用时先确认占用者，不随意杀掉其他服务。服务器启动及 HTTP 重启恢复已按本指南实测，见[上手记录](ONBOARDING_HTTP.md)；浏览器交互仍未验收。另一个终端不会自动继承上述函数；本轮优先用网页 worker 按钮，不另开消费者。

## 3. 三次处理，批准与提交分开

优先使用交付包中已核验的 synthetic-original-white30-blue25.xlsx，不能用结果导出表代替。它只有「订单」工作表，A:E 为客户、款、颜色、尺码、数量；两行是华东服饰/A/白/M/30 与华东服饰/B/蓝/L/25。

没有附件时，在启动服务之前、上节同一终端和全新 DEMO 目录执行以下最小生成器。它只生成合成样例输入，不增加解析器 fixture，也不证明模型能力。新生成文件的 ZIP 时间戳可能不同于交付附件，不能冒称两者字节哈希相同；验收核对的是下面精确单元格。

```sh
demo_python - <<'PYTHON'
from pathlib import Path
from openpyxl import Workbook

path = Path("synthetic-original-white30-blue25.xlsx")
if path.exists():
    raise SystemExit("原表已存在，请核对现有文件，不覆盖证据")
book = Workbook()
sheet = book.active
sheet.title = "订单"
for row in (
    ("客户", "款", "颜色", "尺码", "数量"),
    ("华东服饰", "A", "白", "M", 30),
    ("华东服饰", "B", "蓝", "L", 25),
):
    sheet.append(row)
book.save(path)
print(path.resolve())
PYTHON
```

生成后再启动上述 localhost 服务，使用打印的绝对文件路径上传。不要为运行生成器在正在服务的终端输入命令；必要时先 Ctrl+C 停止服务，同一终端生成后重启。

1. 点击「保存入口指令并查看任务」，记录任务 ID。首次输入是「新建现货订单（表格）」，没有数量，也不选择旧订单。
2. 点击「触发 worker 一次」，核对同一任务变为 NEEDS_CLARIFICATION，要求上传。worker 面向整个队列，不是指定当前任务；本轮不创建其他任务。
3. 上传原表，确认 parser_status 为 ok 并记录来源 ID。勾选该来源，精确回答「按附件新建订单」，再触发 worker。应显示蓝 L 缺5，不能批准；尚未采购。
4. 不新增或替换文件，精确回答「蓝 L 改成 20」，三个间隔都用半角空格，再触发 worker。纠正时可以选择相同来源，或全部不选以继承已经验证的来源；第一次表格回答不能省略选择。回答框会随表格阶段显示对应提示，「只改未开裁」属于生产改单流程。
5. 应到 AWAITING_APPROVAL。完整计划必须含白 M 30、蓝 L 20。新订单旧量是0；独立纠正证据才是原表25→回答20。原文件保留25，历史缺5建议仍为 DRAFT，标明旧意图，非当前采购指令。
6. 单独点击「批准当前计划」后核对仍未建单，再单独点击「提交生效」。检查仅一张新订单、一个 operation_id、revision 1、白30/蓝20及对应预留；核验应显示限定字段一致（partial），整单来源仍不确定，不代表全库通过。异常或 pending 需先核对原因，不通过重提业务来修复。
7. 刷新只读核对结果，不重开入口或发送新答案。页面没有专用重放按钮。结果不确定时先看任务及操作记录，不换提交标识盲重试。

默认三次 worker 尝试，错误回答也占用。第四个不同输入会使旧计划失效，即使没有剩余处理预算。耗尽不能原任务恢复，换表也需新任务重新上传。重演整条黄金路径使用另一套全新合成目录，避免上次预占改变库存；不要删除旧证据。

## 4. 停止、重启与证据

Ctrl+C 停止服务。在同一终端、同一 DEMO 目录和相同环境下重新执行启动命令会读取原库，不是重做验收。换终端需明确恢复原 PY、DEMO 和函数，不能退回默认数据库。保留任务/订单/操作 ID 与截图；不要公开整个数据库或日志。上传原件仍属于临时文件保留，不承诺跨清理、迁移或长期重启可下载。已有回执不能恢复丢失原件，不应借重试替换它。

已存在的自动证据证明源码与普通安装 wheel 的持久处理链路；本指南中的浏览器点击、截图、文件选择与返回/刷新时序仍需实际验收。固定表格流程是确定性解析，不证明通用中文或 LIVE 模型能力。
