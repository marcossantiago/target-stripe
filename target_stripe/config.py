"""Configuration schema and validation for target-stripe."""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StripeMode(str, Enum):
    """Stripe environment mode."""

    TEST = "test"
    LIVE = "live"


class IdempotencyStrategy(str, Enum):
    """Strategy for generating idempotency keys."""

    SOURCE_ID = "source_id"
    HASH = "hash"


class PastDueHandling(str, Enum):
    """Strategy for handling subscriptions with past-due billing dates."""

    SKIP = "skip"
    CREATE_FRESH = "create_fresh"


class IdempotencyConfig(BaseModel):
    """Configuration for idempotency handling."""

    strategy: IdempotencyStrategy = Field(
        default=IdempotencyStrategy.SOURCE_ID,
        description="Strategy for generating idempotency keys",
    )


def _coerce_metadata_pairs(v: Any) -> List[Tuple[str, str]]:
    """Normalize metadata from JSON (list of two-element lists) to pairs."""
    if v is None:
        return []
    if not isinstance(v, list):
        raise TypeError("metadata must be a list of [record_field, stripe_metadata_key] pairs")
    out: List[Tuple[str, str]] = []
    for i, item in enumerate(v):
        if isinstance(item, (list, tuple)) and len(item) == 2:
            out.append((str(item[0]), str(item[1])))
        else:
            raise ValueError(
                f"metadata[{i}] must be a two-element array [record_field, stripe_metadata_key]"
            )
    return out


class CustomerSourceFieldsConfig(BaseModel):
    """Source column names and Stripe metadata mapping for customer records."""

    customer_id: str = Field(
        default="source_customer_id",
        description="Top-level record field containing the source customer ID",
    )
    metadata: List[Tuple[str, str]] = Field(
        default_factory=lambda: [("source_customer_id", "source_customer_id")],
        description="Pairs: source column name → Stripe metadata key",
    )

    @field_validator("metadata", mode="before")
    @classmethod
    def _validate_metadata_input(cls, v: Any) -> Any:
        return _coerce_metadata_pairs(v)

    @model_validator(mode="after")
    def _validate_metadata_pairs(self) -> CustomerSourceFieldsConfig:
        seen: set[str] = set()
        for rf, _ in self.metadata:
            if rf in seen:
                raise ValueError(f"Duplicate record_field in metadata: {rf!r}")
            seen.add(rf)
        matches = [p for p in self.metadata if p[0] == self.customer_id]
        if len(matches) != 1:
            raise ValueError(
                "metadata must contain exactly one pair whose first element equals "
                f"customer_id ({self.customer_id!r})"
            )
        return self

    def stripe_metadata_key_for_source_id(self) -> str:
        """Stripe metadata key where the canonical source customer ID is stored."""
        for rf, sk in self.metadata:
            if rf == self.customer_id:
                return sk
        raise RuntimeError("metadata/customer_id invariant violated")


class SubscriptionSourceFieldsConfig(BaseModel):
    """Source column names and Stripe metadata mapping for subscription records."""

    subscription_id: str = Field(
        default="source_subscription_id",
        description="Top-level record field containing the source subscription ID",
    )
    subscription_customer_id: Optional[str] = Field(
        default=None,
        description=(
            "Record field referencing the customer (source id or Stripe id). "
            "If unset, common fallbacks are used."
        ),
    )
    cancel_at_period_end: str = Field(
        default="cancel_at_period_end",
        description="Record field for cancel-at-period-end flag",
    )
    billing_cycle_anchor: Optional[str] = Field(
        default=None,
        description="Record field for billing cycle anchor / renewal date",
    )
    backdate_start: Optional[str] = Field(
        default=None,
        description="Record field for period start / backdate start",
    )
    proration_behavior: str = Field(
        default="none",
        description="Stripe proration behavior for billing-cycle preservation",
    )
    metadata: List[Tuple[str, str]] = Field(
        default_factory=lambda: [("source_subscription_id", "source_subscription_id")],
        description="Pairs: source column name → Stripe metadata key",
    )

    @field_validator("metadata", mode="before")
    @classmethod
    def _validate_metadata_input(cls, v: Any) -> Any:
        return _coerce_metadata_pairs(v)

    @model_validator(mode="after")
    def _validate_metadata_pairs(self) -> SubscriptionSourceFieldsConfig:
        seen: set[str] = set()
        for rf, _ in self.metadata:
            if rf in seen:
                raise ValueError(f"Duplicate record_field in metadata: {rf!r}")
            seen.add(rf)
        matches = [p for p in self.metadata if p[0] == self.subscription_id]
        if len(matches) != 1:
            raise ValueError(
                "metadata must contain exactly one pair whose first element equals "
                f"subscription_id ({self.subscription_id!r})"
            )
        return self

    def stripe_metadata_key_for_source_id(self) -> str:
        """Stripe metadata key where the canonical source subscription ID is stored."""
        for rf, sk in self.metadata:
            if rf == self.subscription_id:
                return sk
        raise RuntimeError("metadata/subscription_id invariant violated")


class CustomerBlockConfig(BaseModel):
    """Customer stream settings."""

    source_fields: CustomerSourceFieldsConfig = Field(
        default_factory=CustomerSourceFieldsConfig,
    )
    add_test_payment_methods: bool = Field(
        default=False,
        description="Attach test payment methods to customers (test mode only)",
    )
    check_existing: bool = Field(
        default=True,
        description=(
            "If true, search Stripe for an existing customer by email when no local "
            "mapping exists (Customer.list)."
        ),
    )
    skip_mapped_records: bool = Field(
        default=False,
        description=(
            "If true, skip customer records that already have a row in the local mapping database."
        ),
    )
    update_existing: bool = Field(
        default=False,
        description=(
            "If true, call Customer.modify when an existing customer is found "
            "(local mapping or email lookup). If false, only link source_id to "
            "stripe_id without updating Stripe fields."
        ),
    )


class SubscriptionBlockConfig(BaseModel):
    """Subscription stream settings."""

    source_fields: SubscriptionSourceFieldsConfig = Field(
        default_factory=SubscriptionSourceFieldsConfig,
    )
    plan_code_to_price_id: Dict[str, str] = Field(
        default_factory=dict,
        description="Plan code → Stripe price ID",
    )
    plan_code_mapping_file: Optional[str] = Field(
        default=None,
        description="Path to JSON/YAML file with plan code mappings",
    )
    default_currency: str = Field(
        default="usd",
        description="Default currency for subscriptions (ISO 4217)",
    )
    past_due_handling: PastDueHandling = Field(
        default=PastDueHandling.SKIP,
        description="Handling when billing_cycle_anchor is in the past",
    )
    skip_mapped_records: bool = Field(
        default=False,
        description=(
            "If true, skip subscription records that already have a row in the local "
            "mapping database."
        ),
    )

    @field_validator("default_currency")
    @classmethod
    def validate_currency(cls, v: str) -> str:
        return v.lower()

    @model_validator(mode="after")
    def load_plan_code_mapping(self) -> SubscriptionBlockConfig:
        """Merge plan code mapping from file if specified."""
        if self.plan_code_mapping_file:
            path = Path(self.plan_code_mapping_file)
            if path.exists():
                content = path.read_text()
                if path.suffix in (".yaml", ".yml"):
                    try:
                        import yaml

                        file_mapping = yaml.safe_load(content)
                    except ImportError as e:
                        raise ValueError(
                            "PyYAML is required to load YAML mapping files. "
                            "Install with: pip install pyyaml"
                        ) from e
                else:
                    file_mapping = json.loads(content)

                if isinstance(file_mapping, dict):
                    self.plan_code_to_price_id = {
                        **file_mapping,
                        **self.plan_code_to_price_id,
                    }
        return self


class TargetStripeConfig(BaseModel):
    """Configuration schema for target-stripe."""

    model_config = ConfigDict(extra="forbid")

    stripe_api_key: str = Field(
        ...,
        description="Stripe API key (starts with sk_test_ or sk_live_)",
    )
    stripe_mode: StripeMode = Field(
        default=StripeMode.TEST,
        description="Stripe mode: test or live",
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
    customers: CustomerBlockConfig = Field(
        default_factory=CustomerBlockConfig,
        description="Customer stream configuration",
    )
    subscriptions: SubscriptionBlockConfig = Field(
        default_factory=SubscriptionBlockConfig,
        description="Subscription stream configuration",
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
    def validate_test_payment_methods(self) -> TargetStripeConfig:
        """Validate that test payment methods are only enabled in test mode."""
        if self.customers.add_test_payment_methods and self.stripe_mode != StripeMode.TEST:
            raise ValueError(
                "add_test_payment_methods can only be enabled in test mode (stripe_mode='test')"
            )

        return self


# Keys injected by singer-sdk Target / Meltano that are not part of our schema.
_STRIP_BEFORE_VALIDATE = frozenset(
    {
        "load_method",
        "validate_records",
    }
)

_NESTED_JSON_KEYS = frozenset({"customers", "subscriptions"})


def _coerce_nested_json_objects(raw: Dict[str, Any]) -> None:
    """Parse JSON strings for nested objects (some runners pass object settings as strings)."""
    for key in _NESTED_JSON_KEYS:
        val = raw.get(key)
        if isinstance(val, str) and val.strip():
            try:
                raw[key] = json.loads(val)
            except json.JSONDecodeError as e:
                raise ValueError(
                    f"Config key {key!r} must be a JSON object or a JSON-encoded string; "
                    f"failed to parse: {e}"
                ) from e
    idem = raw.get("idempotency")
    if isinstance(idem, str) and idem.strip():
        try:
            parsed = json.loads(idem)
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(parsed, dict):
                raw["idempotency"] = parsed


def parse_config(raw_config: Dict[str, Any]) -> TargetStripeConfig:
    """Parse and validate configuration dictionary.

    Args:
        raw_config: Raw configuration dictionary from Singer.

    Returns:
        Validated TargetStripeConfig instance.

    Raises:
        ValueError: If configuration is invalid.
    """
    raw = dict(raw_config)
    for k in _STRIP_BEFORE_VALIDATE:
        raw.pop(k, None)

    _coerce_nested_json_objects(raw)

    if "idempotency" in raw and isinstance(raw["idempotency"], dict):
        pass
    elif "idempotency" in raw and isinstance(raw["idempotency"], str):
        raw["idempotency"] = {"strategy": raw["idempotency"]}

    return TargetStripeConfig(**raw)
