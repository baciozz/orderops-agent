"""Decode recorded observations for display, never as authorization or fresh facts."""

from pydantic import ValidationError
from schemas.verification_observation import VerificationObservation

from app.db.models import Operation


def recorded_observation(operation: Operation | None) -> VerificationObservation | None:
    if operation is None or operation.verification_observation is None:
        return None
    try:
        report = VerificationObservation.model_validate(operation.verification_observation)
        if report.operation_id != operation.id or report.execution.operation_id != operation.id:
            return None
        if report.status == "partial" and (
            report.existing_fact_checks != "consistent"
            or report.execution.status != "consistent_for_retained_fields"
        ):
            return None
        return report
    except (ValidationError, TypeError, ValueError):
        return None
