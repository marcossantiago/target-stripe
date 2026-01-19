"""Configuration schema and validation for target-stripe."""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class StripeMode(str, Enum):
    """Stripe environment mode."""

    TEST = "test"
    LIVE = "live"


class IdempotencyStrategy(str, Enum):
    """Strategy for generating idempotency keys."""

    SOURCE_ID = "source_id"
    HASH = "hash"


class IdempotencyConfig(BaseModel):
    """Configuration for idempotency handling."""

    strategy: IdempotencyStrategy = Field(
        default=IdempotencyStrategy.SOURCE_ID,
        description="Strategy for generating idempotency keys",
    )


class SourceFieldsConfig(BaseModel):
    """Configuration for source system field names.

    This allows the plugin to work with any source system by configuring
    the field names used for customer and subscription IDs, as well as
    the metadata keys stored in Stripe.
    """

    customer_source_id_field: str = Field(
        default="source_customer_id",
        description="Field name in source records containing the customer ID",
    )
    customer_metadata_key: str = Field(
        default="source_customer_id",
        description="Metadata key used in Stripe to store the source customer ID",
    )
    subscription_source_id_field: str = Field(
        default="source_subscription_id",
        description="Field name in source records containing the subscription ID",
    )
    subscription_metadata_key: str = Field(
        default="source_subscription_id",
        description="Metadata key used in Stripe to store the source subscription ID",
    )
    additional_metadata_fields: list[str] = Field(
        default_factory=list,
        description="Additional fields from source records to include in Stripe metadata",
    )


class TargetStripeConfig(BaseModel):
    """Configuration schema for target-stripe."""

    stripe_api_key: str = Field(
        ...,
        description="Stripe API key (starts with sk_test_ or sk_live_)",
    )
    stripe_mode: StripeMode = Field(
        default=StripeMode.TEST,
        description="Stripe mode: test or live",
    )
    default_currency: str = Field(
        default="usd",
        description="Default currency for subscriptions (ISO 4217 code)",
    )
    dry_run: bool = Field(
        default=False,
        description="If true, validate data but do not write to Stripe",
    )
    hard_fail: bool = Field(
        default=False,
        description="If true, fail immediately on any record error; otherwise continue",
    )
    idempotency: IdempotencyConfig = Field(
        default_factory=IdempotencyConfig,
        description="Idempotency configuration",
    )
    source_fields: SourceFieldsConfig = Field(
        default_factory=SourceFieldsConfig,
        description="Configuration for source system field names",
    )
    plan_code_to_price_id: dict[str, str] = Field(
        default_factory=dict,
        description="Mapping of plan codes to Stripe price IDs",
    )
    plan_code_mapping_file: str | None = Field(
        default=None,
        description="Path to JSON/YAML file containing plan code to price ID mapping",
    )
    state_emit_interval: int = Field(
        default=100,
        ge=1,
        description="Number of records between STATE message emissions",
    )
    rate_limit_per_sec: float = Field(
        default=25.0,
        gt=0,
        le=100,
        description="Maximum Stripe API requests per second",
    )
    mapping_db_path: str = Field(
        default=".target-stripe-mapping.db",
        description="Path to SQLite database for ID mappings",
    )
    batch_size: int = Field(
        default=50,
        ge=1,
        le=100,
        description="Number of records to process in a batch",
    )
    max_retries: int = Field(
        default=3,
        ge=0,
        le=10,
        description="Maximum number of retries for transient errors",
    )
    retry_backoff_base: float = Field(
        default=2.0,
        ge=1.0,
        le=5.0,
        description="Base for exponential backoff (seconds)",
    )

    @field_validator("stripe_api_key")
    @classmethod
    def validate_api_key(cls, v: str) -> str:
        """Validate that the API key has the correct format."""
        if not v.startswith(("sk_test_", "sk_live_", "rk_test_", "rk_live_")):
            raise ValueError(
                "Invalid Stripe API key format. Must start with sk_test_, sk_live_, "
                "rk_test_, or rk_live_"
            )
        return v

    @field_validator("default_currency")
    @classmethod
    def validate_currency(cls, v: str) -> str:
        """Normalize currency code to lowercase."""
        return v.lower()

    @model_validator(mode="after")
    def validate_mode_matches_key(self) -> TargetStripeConfig:
        """Validate that stripe_mode matches the API key type."""
        key_is_test = self.stripe_api_key.startswith(("sk_test_", "rk_test_"))
        key_is_live = self.stripe_api_key.startswith(("sk_live_", "rk_live_"))

        if self.stripe_mode == StripeMode.TEST and key_is_live:
            raise ValueError("stripe_mode is 'test' but API key is for live mode")
        if self.stripe_mode == StripeMode.LIVE and key_is_test:
            raise ValueError("stripe_mode is 'live' but API key is for test mode")

        return self

    @model_validator(mode="after")
    def load_plan_code_mapping(self) -> TargetStripeConfig:
        """Load plan code mapping from file if specified."""
        if self.plan_code_mapping_file:
            path = Path(self.plan_code_mapping_file)
            if path.exists():
                content = path.read_text()
                if path.suffix in (".yaml", ".yml"):
                    try:
                        import yaml

                        file_mapping = yaml.safe_load(content)
                    except ImportError:
                        raise ValueError(
                            "PyYAML is required to load YAML mapping files. "
                            "Install with: pip install pyyaml"
                        )
                else:
                    file_mapping = json.loads(content)

                if isinstance(file_mapping, dict):
                    self.plan_code_to_price_id = {
                        **file_mapping,
                        **self.plan_code_to_price_id,
                    }
        return self


def parse_config(raw_config: dict[str, Any]) -> TargetStripeConfig:
    """Parse and validate configuration dictionary.

    Args:
        raw_config: Raw configuration dictionary from Singer.

    Returns:
        Validated TargetStripeConfig instance.

    Raises:
        ValueError: If configuration is invalid.
    """
    if "idempotency" in raw_config and isinstance(raw_config["idempotency"], dict):
        pass
    elif "idempotency" in raw_config and isinstance(raw_config["idempotency"], str):
        raw_config["idempotency"] = {"strategy": raw_config["idempotency"]}

    # Ensure source_fields is a dict if provided
    if "source_fields" in raw_config and not isinstance(raw_config["source_fields"], dict):
        raw_config["source_fields"] = {}

    return TargetStripeConfig(**raw_config)
