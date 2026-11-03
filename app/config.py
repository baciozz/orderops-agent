"""Process settings. Live requires the flag, explicit prices, and any required key."""

from decimal import Decimal
from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

HARD_BUDGET_CEILING_USD = Decimal("5.00")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OOS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    log_json: bool = True
    log_level: str = "INFO"

    live_enabled: bool = False
    live_budget_usd: Decimal = Decimal("5.00")
    price_in_per_mtok: Decimal | None = None
    price_out_per_mtok: Decimal | None = None
    llm_api_key: SecretStr | None = None
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_base_url: str | None = Field(
        default_factory=lambda data: (
            None if data.get("llm_provider") == "openai_compatible" else "https://api.openai.com/v1"
        )
    )
    llm_json_schema_supported: bool = False
    model_reasoning_effort: str = "none"
    database_url: str = "sqlite:///./data/oos.db"
    approval_ttl_seconds: int = 600
    worker_max_active: int = 2
    model_timeout_s: int = 60
    budget_reservation_ttl_s: int | None = Field(default=None, gt=0)
    model_max_tokens: int = 1000
    model_max_calls_per_task: int = 3

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

    @property
    def effective_budget_reservation_ttl_s(self) -> int:
        """Never orphan an in-flight call merely because its timeout was raised."""
        return max(self.budget_reservation_ttl_s or 0, 2 * self.model_timeout_s + 60)

    def llm_api_key_value(self) -> str | None:
        if self.llm_api_key is None:
            return None
        raw = self.llm_api_key.get_secret_value().strip()
        return raw or None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
