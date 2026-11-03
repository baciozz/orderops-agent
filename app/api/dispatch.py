"""Synchronous fixture dispatch. Sub-agents propose; the kernel prechecks."""

from uuid import uuid4

from fastapi import APIRouter, Request
from schemas.api import DispatchRequest, DispatchResponse, DispatchStepBody

from app.orchestration.dispatch import dispatch_text

router = APIRouter()


@router.post("/dispatch", response_model=DispatchResponse)
def dispatch_input(body: DispatchRequest, request: Request) -> DispatchResponse:
    outcome = dispatch_text(
        request.app.state.session_factory,
        text=body.text,
        input_mode=body.input_mode.value,
        client_event_key=body.client_event_key or str(uuid4()),
        registry=request.app.state.registry,
        settings=request.app.state.settings,
    )
    return DispatchResponse(
        request_id=request.state.request_id,
        correlation_id=request.state.correlation_id,
        task_id=outcome.task_id,
        chain_id=outcome.chain_id,
        status=outcome.status,
        plan_id=outcome.plan_id,
        order_id=outcome.order_id,
        intent_revision=outcome.intent_revision,
        plan_hash=outcome.plan_hash,
        requires_human_approval=outcome.requires_human_approval,
        issues=outcome.issues,
        error_code=outcome.error_code,
        steps=[
            DispatchStepBody(
                step_no=step.step_no,
                agent_id=step.agent_id,
                agent_version=step.agent_version,
                capability=step.capability,
                status=step.status,
                error_code=step.error_code,
                skip_reason=step.skip_reason,
            )
            for step in outcome.steps
        ],
    )
