"""Recorded, limited readback. Never a full order/database verification claim."""

from datetime import datetime
from typing import Literal

from schemas.common import ApiModel
from schemas.execution_audit import ExecutionAudit


class VerificationObservation(ApiModel):
    protocol: Literal["execution_guard_v1"] = "execution_guard_v1"
    operation_id: str
    observed_at: datetime
    status: Literal["partial", "findings", "inconclusive"]
    existing_fact_checks: Literal["consistent", "findings"]
    execution: ExecutionAudit
    order_graph: Literal["inconclusive"] = "inconclusive"
    global_completeness: Literal["not_assessed"] = "not_assessed"
    authorizes_business_action: Literal[False] = False
    observation: Literal["recorded_readback_not_continuous_monitoring"] = (
        "recorded_readback_not_continuous_monitoring"
    )
