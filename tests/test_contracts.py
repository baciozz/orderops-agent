"""Frozen request examples and the extra=forbid boundary."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from schemas.api import CommitSuccess, PlanChangeRequest, PlanPreviewResponse
from schemas.common import UsageReport
from schemas.subagent import AgentProposal

_EXAMPLES = Path(__file__).resolve().parents[1] / "schemas" / "examples"


def test_examples_match_frozen_models() -> None:
    request = json.loads((_EXAMPLES / "plan_change_request.json").read_text(encoding="utf-8"))
    parsed = PlanChangeRequest.model_validate(request)
    assert parsed.order_id == "SO-1001"
    assert parsed.changes[0].mode == "replace_remaining"
    assert parsed.changes[0].qty == 40
    preview = PlanPreviewResponse.model_validate(
        json.loads((_EXAMPLES / "plan_preview_response.json").read_text(encoding="utf-8"))
    )
    assert preview.requires_human_approval is True
    success = CommitSuccess.model_validate(
        json.loads((_EXAMPLES / "commit_success.json").read_text(encoding="utf-8"))
    )
    assert success.status == "SUCCEEDED"
    assert success.revision == 2


def test_plan_request_rejects_client_approved_flag() -> None:
    payload = json.loads((_EXAMPLES / "plan_change_request.json").read_text(encoding="utf-8"))
    payload["approved"] = True
    with pytest.raises(ValidationError):
        PlanChangeRequest.model_validate(payload)


def test_agent_proposal_rejects_extra_approved_field() -> None:
    with pytest.raises(ValidationError):
        AgentProposal.model_validate(
            {
                "agent_id": "parser",
                "agent_version": "0.1.0",
                "task_id": "T-01",
                "intent_revision": 1,
                "fencing_token": 1,
                "input_hash": "abc",
                "payload": {},
                "usage": UsageReport(mode="FIXTURE").model_dump(),
                "approved": True,
            }
        )
