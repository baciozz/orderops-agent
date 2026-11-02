"""Fields shared by API envelopes, errors, and source evidence."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from schemas import SCHEMA_VERSION
from schemas.enums import InputMode


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceRef(ApiModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    source_id: str
    line_start: int | None = None
    line_end: int | None = None
    sheet: str | None = None
    cell_range: str | None = None
    message_index: int | None = None
    field: str | None = None
    text_excerpt: str | None = None
    inferred: bool = False


class Issue(ApiModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    code: str
    message: str
    field: str | None = None


class UsageReport(ApiModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    mode: Literal["FIXTURE", "LIVE"]
    provider: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: str = "0"
    price_version: str | None = None


class ErrorDetail(ApiModel):
    code: str
    message: str
    retryable: bool
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(ApiModel):
    request_id: str
    correlation_id: str
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    error: ErrorDetail


class InputDescriptor(ApiModel):
    """How an unstructured input should be parsed. Not a business event."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    input_mode: InputMode
    source_ids: list[str] = Field(default_factory=list)
