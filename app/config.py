"""Process settings. Live requires the flag, explicit prices, and any required key.

Prices are never defaulted. Each model is priced either by OOS_MODEL_PRICES
(a JSON object keyed by model id) or, for the configured OOS_LLM_MODEL only, by
the legacy OOS_PRICE_IN_PER_MTOK / OOS_PRICE_OUT_PER_MTOK pair. A model with no
price is refused before any request is sent.
"""

from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

HARD_BUDGET_CEILING_USD = Decimal("30.00")
# Every ledger refuses the call after this many rows, whatever the caller asks for.
HARD_MAX_TOTAL_CALLS = 3000


class ModelPrice(BaseModel):
    """Standard USD price per million tokens, copied from the provider's official page."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    in_per_mtok: Decimal
    out_per_mtok: Decimal

    @field_validator("in_per_mtok", "out_per_mtok", mode="before")
    @classmethod
    def parse_price(cls, value: object) -> Decimal:
        if (
            value is None
            or isinstance(value, bool)
            or (isinstance(value, str) and not value.strip())
        ):
            raise ValueError("单价不能为空")
        try:
            amount = Decimal(str(value))
        except InvalidOperation:
            raise ValueError("单价须为十进制数") from None
        if not amount.is_finite() or amount < 0:
            raise ValueError("单价须为非负有限数")
        return amount


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OOS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
        frozen=True,
    )

    log_json: bool = True
    log_level: str = "INFO"

    live_enabled: bool = False
    live_budget_usd: Decimal = Decimal("5.00")
    price_in_per_mtok: Decimal | None = None
    price_out_per_mtok: Decimal | None = None
    # OOS_MODEL_PRICES='{"<model id>": {"in_per_mtok": "…", "out_per_mtok": "…"}}'
    model_prices: dict[str, ModelPrice] = Field(default_factory=dict)
    llm_api_key: SecretStr | None = None
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_base_url: str | None = Field(
        default_factory=lambda data: (
            None if data.get("llm_provider") == "openai_compatible" else "https://api.openai.com/v1"
        )
    )
    llm_json_schema_supported: bool = False
    # Optional external tracer; default off. This codebase never sends data.
    trace_langfuse: bool = False
    model_reasoning_effort: str = "none"
    database_url: str = "sqlite:///./data/oos.db"
    source_storage_dir: Path | None = None
    ocr_data_dir: Path | None = None
    approval_ttl_seconds: int = 600
    worker_max_active: int = 2
    model_timeout_s: int = 60
    budget_reservation_ttl_s: int | None = Field(default=None, gt=0)
    model_max_tokens: int = 1000
    model_max_calls_per_task: int = 3
    verification_max_retries: int = Field(default=5, ge=0, le=100)
    verification_base_delay_seconds: int = Field(default=30, ge=1, le=86400)
    verification_max_delay_seconds: int = Field(default=3600, ge=1, le=86400)
    verification_lease_seconds: int = Field(default=120, ge=1, le=3600)
    verification_batch_size: int = Field(default=100, ge=1, le=1000)

    @field_validator("source_storage_dir", mode="before")
    @classmethod
    def parse_source_storage_dir(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value

    @field_validator("source_storage_dir")
    @classmethod
    def validate_source_storage_dir(cls, value: Path | None) -> Path | None:
        if value is not None and (
            not value.is_absolute() or value == Path("/") or ".." in value.parts
        ):
            raise ValueError("原件目录须为不含上级跳转的绝对路径，不能是根目录")
        return value

    @field_validator("worker_max_active")
    @classmethod
    def validate_worker_max_active(cls, value: int) -> int:
        if value < 1:
            raise ValueError("Worker 最大并发任务数必须大于等于 1")
        return value

    @field_validator("price_in_per_mtok", "price_out_per_mtok", mode="before")
    @classmethod
    def parse_optional_price(cls, value: object) -> Decimal | None:
        if value is None or (isinstance(value, str) and value.strip() == ""):
            return None
        amount = Decimal(str(value))
        if amount < 0:
            raise ValueError("单价不能为负")
        return amount

    @field_validator("live_budget_usd", mode="before")
    @classmethod
    def parse_budget(cls, value: object) -> Decimal:
        amount = Decimal(str(value))
        if amount < 0:
            raise ValueError("预算不能为负")
        return amount

    @field_validator("model_prices", mode="before")
    @classmethod
    def parse_model_prices(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return {}
        return value

    @field_validator("model_prices")
    @classmethod
    def validate_model_ids(cls, value: dict[str, ModelPrice]) -> dict[str, ModelPrice]:
        if any(not model.strip() or model != model.strip() for model in value):
            raise ValueError("OOS_MODEL_PRICES 的模型 ID 不能为空或含首尾空白")
        return value

    @model_validator(mode="after")
    def reject_conflicting_prices(self) -> "Settings":
        """Two different prices for the active model would force a guess."""
        mapped = self.model_prices.get(self.llm_model or "")
        if (
            mapped is not None
            and self.price_in_per_mtok is not None
            and self.price_out_per_mtok is not None
            and (mapped.in_per_mtok, mapped.out_per_mtok)
            != (self.price_in_per_mtok, self.price_out_per_mtok)
        ):
            raise ValueError("当前模型在 OOS_MODEL_PRICES 与 OOS_PRICE_*_PER_MTOK 中单价冲突")
        return self

    @field_validator("llm_api_key", "llm_provider", "llm_model", "llm_base_url", mode="before")
    @classmethod
    def blank_string_to_none(cls, value: object) -> object:
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    @property
    def effective_live_budget_usd(self) -> Decimal:
        """Config cap and the code ceiling both apply. The smaller one wins."""
        return min(self.live_budget_usd, HARD_BUDGET_CEILING_USD)

    def prices_for(self, model: str | None) -> tuple[Decimal, Decimal] | None:
        """(input, output) USD per million tokens, or None when the model is unpriced.

        The legacy pair prices only the configured OOS_LLM_MODEL (or an unnamed
        model, preserving the old readiness check); it never prices other models.
        """
        if model is not None and model in self.model_prices:
            price = self.model_prices[model]
            return price.in_per_mtok, price.out_per_mtok
        if model is not None and model != self.llm_model:
            return None
        if self.price_in_per_mtok is None or self.price_out_per_mtok is None:
            return None
        return self.price_in_per_mtok, self.price_out_per_mtok

    def priced_models(self) -> dict[str, tuple[Decimal, Decimal]]:
        """Every explicitly priced model id; the ledger refuses all others."""
        table = {
            model: (price.in_per_mtok, price.out_per_mtok)
            for model, price in self.model_prices.items()
        }
        if self.llm_model and self.llm_model not in table:
            legacy = self.prices_for(self.llm_model)
            if legacy is not None:
                table[self.llm_model] = legacy
        return table

    @property
    def effective_budget_reservation_ttl_s(self) -> int:
        """Never orphan an in-flight call merely because its timeout was raised."""
        return max(self.budget_reservation_ttl_s or 0, 2 * self.model_timeout_s + 60)

    def llm_api_key_value(self) -> str | None:
        if self.llm_api_key is None:
            return None
        raw = self.llm_api_key.get_secret_value().strip()
        return raw or None

    @property
    def has_llm_api_key(self) -> bool:
        """Local readiness checks need presence, never the plaintext credential."""
        return bool(self.llm_api_key)

    @property
    def has_official_llm_base_url(self) -> bool:
        return (self.llm_base_url or "").rstrip("/") == "https://api.openai.com/v1"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
