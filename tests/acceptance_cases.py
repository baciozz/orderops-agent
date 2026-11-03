"""Checklist of B01–B32 and R01–R24.

A skipped case is not a pass. R17, R22, and R23 are implemented in Stage 0
because this run's scope includes the registry and the budget guard. The
plan schedules those cases later; the mechanisms they need are already here.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Case:
    case_id: str
    stage: str
    title: str
    implemented: bool = False


CASES: tuple[Case, ...] = (
    Case("B01", "1A", "黑100已发60，余40换白"),
    Case("B02", "1A", "再加20未澄清，不写业务表，旧批准失效"),
    Case("B03", "1A", "明确给白色增加20"),
    Case("B04", "1A", "明确把白色余量设为20"),
    Case("B05", "1A", "要求把已发黑60改为白，拒绝"),
    Case("B06", "1A", "总量设为50但已发60，422"),
    Case("B07", "1A", "可用20再要25，缺口5，只出采购建议"),
    Case("B08", "1A", "未知 SKU 或同名客户，澄清且不创建主数据"),
    Case("B09", "1A", "同一计划同一幂等键提交两次，只有一笔修订"),
    Case("B10", "1A", "同一幂等键不同请求体，409"),
    Case("B11", "1A", "批准后新模糊请求入库，旧批准不能提交"),
    Case("B12", "1A", "批准后库存版本变化，STALE_DEPENDENCY"),
    Case("B13", "1A", "计划或 hash 被篡改，拒绝"),
    Case("B14", "1A", "批准过期或审批人不匹配"),
    Case("B15", "1A", "模型或文档伪造 approved，不能创建批准"),
    Case("B16", "1A", "两单抢最后库存，只有一方成功且库存非负"),
    Case("B17", "1A", "同单两个计划同时提交，至多一个成功"),
    Case("B18", "1A", "提交中注入异常，全部回滚"),
    Case("B19", "1B", "C 款黑 L 全部改藏青，已裁部分锁定并澄清"),
    Case("B20", "1B", "选择只改未裁部分后批准提交"),
    Case("B21", "1B", "提交后回读核验；读失败只标记 pending"),
    Case("B22", "1B", "要求把已裁 120 也改藏青，422"),
    Case("B23", "1B", "批准后生产单被裁剪，STALE_DEPENDENCY"),
    Case("B24", "1B", "C 款总量改为 100，低于已裁 120"),
    Case("B25", "1B", "B01 提交时下游单据只重新链接"),
    Case("B26", "1B", "一致性巡检只出报告，不自动写"),
    Case("B27", "1B", "跨单据提交中途失败则全部回滚"),
    Case("B28", "1A", "聊天新建华东服饰 A 款白 M 30"),
    Case("B29", "1A", "XLSX 新建蓝 L 25，缺 5，整单拒绝"),
    Case("B30", "1A", "新建时未知 SKU 或同名客户"),
    Case("B31", "1A", "新建幂等与疑似重复是两层机制"),
    Case("B32", "1A", "新建计划批准后库存 revision 变化"),
    Case("R01", "2", "旧任务仍运行时新输入先持久化并返回 202"),
    Case("R02", "2", "重复投递只产生一次业务效果"),
    Case("R03", "2", "lease 过期被接管，旧 attempt 晚到结果丢弃"),
    Case("R04", "2", "取消先于提交获胜"),
    Case("R05", "2", "提交先成功，取消不能回滚"),
    Case("R06", "2", "回执丢失后用同一幂等键对账"),
    Case("R07", "2", "通知前崩溃，outbox 补投且业务只写一次"),
    Case("R08", "2", "等待澄清时重启，问题和来源仍在"),
    Case("R09", "2", "模型持续报错到步骤上限后失败"),
    Case("R10", "3", "文件中的越权指令只当数据"),
    Case("R11", "3", "XLSX 超限、宏或危险公式被拒绝"),
    Case("R12", "3", "下载文本的公式前缀按安全文本导出"),
    Case("R13", "3", "跨任务 source 或路径遍历被拒绝"),
    Case("R14", "3", "聊天与表格数量冲突时等待确认"),
    Case("R15", "3", "界面和报告区分 FIXTURE 与 LIVE"),
    Case("R16", "2", "重启后预占总和与余额一致，流水可解释"),
    Case("R17", "0", "注册表重复或 schema 缺失则启动失败；缺能力则 CAPABILITY_MISSING", True),
    Case("R18", "1C", "子 Agent 调用白名单外工具被拒绝并审计"),
    Case("R19", "1C", "子 Agent 输出不合 schema，重试后失败且不进预检"),
    Case("R20", "1C", "子 Agent 超时后晚到结果因 fencing 丢弃"),
    Case("R21", "1C", "改单轨迹含三个子 Agent，新建单跳过 doc-check"),
    Case("R22", "0", "预算熔断在调用前拒绝，重启后账本仍在", True),
    Case("R23", "0", "单价或密钥缺失时 live 拒绝启动并保持 FIXTURE", True),
    Case("R24", "3", "任务视图过期时 approvable 为 false 且 disabled_reason 为 EXPIRED"),
)

PENDING: tuple[Case, ...] = tuple(case for case in CASES if not case.implemented)
IMPLEMENTED: tuple[Case, ...] = tuple(case for case in CASES if case.implemented)
