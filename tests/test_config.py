"""Tests for configuration module."""

from __future__ import annotations

import json
import tempfile

import pytest
from pydantic import ValidationError

from target_stripe.config import (
    CustomerSourceFieldsConfig,
    IdempotencyStrategy,
    StripeMode,
    SubscriptionSourceFieldsConfig,
    TargetStripeConfig,
    parse_config,
)


class TestTargetStripeConfig:
    """Tests for TargetStripeConfig."""

    def test_valid_test_config(self, base_config: dict) -> None:
        """Test valid test mode configuration."""
        config = parse_config(base_config)
        assert config.stripe_mode == StripeMode.TEST
        assert config.subscriptions.default_currency == "usd"
        assert config.dry_run is False

    def test_valid_live_config(self, base_config: dict) -> None:
        """Test valid live mode configuration."""
        base_config["stripe_api_key"] = "sk_live_1234567890abcdefghijklmnop"
        base_config["stripe_mode"] = "live"
        config = parse_config(base_config)
        assert config.stripe_mode == StripeMode.LIVE

    def test_invalid_api_key_format(self, base_config: dict) -> None:
        """Test that invalid API key format raises error."""
        base_config["stripe_api_key"] = "invalid_key"
        with pytest.raises(ValueError, match="Invalid Stripe API key format"):
            parse_config(base_config)

    def test_mode_key_mismatch_test(self, base_config: dict) -> None:
        """Test that test key with live mode raises error."""
        base_config["stripe_mode"] = "live"
        with pytest.raises(ValueError, match="stripe_mode is 'live' but API key is for test"):
            parse_config(base_config)

    def test_mode_key_mismatch_live(self, base_config: dict) -> None:
        """Test that live key with test mode raises error."""
        base_config["stripe_api_key"] = "sk_live_1234567890abcdefghijklmnop"
        base_config["stripe_mode"] = "test"
        with pytest.raises(ValueError, match="stripe_mode is 'test' but API key is for live"):
            parse_config(base_config)

    def test_currency_normalization(self, base_config: dict) -> None:
        """Test that currency is normalized to lowercase."""
        base_config["subscriptions"] = {"default_currency": "USD"}
        config = parse_config(base_config)
        assert config.subscriptions.default_currency == "usd"

    def test_idempotency_defaults(self, base_config: dict) -> None:
        """Test idempotency defaults."""
        config = parse_config(base_config)
        assert config.idempotency.strategy == IdempotencyStrategy.SOURCE_ID

    def test_idempotency_hash_strategy(self, base_config: dict) -> None:
        """Test hash idempotency strategy."""
        base_config["idempotency"] = {"strategy": "hash"}
        config = parse_config(base_config)
        assert config.idempotency.strategy == IdempotencyStrategy.HASH

    def test_plan_code_mapping(self, base_config: dict) -> None:
        """Test plan code to price ID mapping."""
        base_config["subscriptions"] = {
            "plan_code_to_price_id": {
                "basic": "price_basic123",
                "pro": "price_pro456",
            },
        }
        config = parse_config(base_config)
        assert config.subscriptions.plan_code_to_price_id["basic"] == "price_basic123"
        assert config.subscriptions.plan_code_to_price_id["pro"] == "price_pro456"

    def test_plan_code_mapping_from_file(self, base_config: dict) -> None:
        """Test loading plan code mapping from JSON file."""
        mapping = {
            "basic": "price_from_file_basic",
            "enterprise": "price_from_file_enterprise",
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(mapping, f)
            f.flush()
            base_config["subscriptions"] = {"plan_code_mapping_file": f.name}

        config = parse_config(base_config)
        assert config.subscriptions.plan_code_to_price_id["basic"] == "price_from_file_basic"
        assert (
            config.subscriptions.plan_code_to_price_id["enterprise"] == "price_from_file_enterprise"
        )

    def test_rate_limit_bounds(self, base_config: dict) -> None:
        """Test rate limit bounds validation."""
        base_config["rate_limit_per_sec"] = 0
        with pytest.raises(ValueError):
            parse_config(base_config)

        base_config["rate_limit_per_sec"] = 101
        with pytest.raises(ValueError):
            parse_config(base_config)

    def test_restricted_api_key(self, base_config: dict) -> None:
        """Test that restricted API keys are accepted."""
        base_config["stripe_api_key"] = "rk_test_1234567890abcdefghijklmnop"
        config = parse_config(base_config)
        assert config.stripe_api_key.startswith("rk_test_")

    def test_source_fields_defaults(self, base_config: dict) -> None:
        """Test default nested source_fields."""
        config = parse_config(base_config)
        assert config.customers.source_fields.customer_id == "source_customer_id"
        assert config.customers.source_fields.metadata == [
            ("source_customer_id", "source_customer_id"),
        ]
        assert (
            config.customers.source_fields.stripe_metadata_key_for_source_id()
            == "source_customer_id"
        )
        assert config.subscriptions.source_fields.subscription_id == "source_subscription_id"
        assert config.subscriptions.source_fields.metadata == [
            ("source_subscription_id", "source_subscription_id"),
        ]

    def test_source_fields_custom(self, base_config: dict) -> None:
        """Test custom metadata pairs and id column names."""
        base_config["customers"] = {
            "source_fields": {
                "customer_id": "chargify_customer_id",
                "metadata": [
                    ["chargify_customer_id", "chargify_customer_id"],
                    ["salesforce_id", "salesforce_id"],
                    ["hubspot_id", "hubspot_id"],
                ],
            },
        }
        base_config["subscriptions"] = {
            "source_fields": {
                "subscription_id": "chargify_subscription_id",
                "metadata": [
                    ["chargify_subscription_id", "chargify_subscription_id"],
                ],
            },
        }
        config = parse_config(base_config)
        assert config.customers.source_fields.customer_id == "chargify_customer_id"
        meta = config.customers.source_fields.metadata
        assert ("chargify_customer_id", "chargify_customer_id") in meta
        assert ("salesforce_id", "salesforce_id") in meta
        assert ("hubspot_id", "hubspot_id") in meta
        assert config.subscriptions.source_fields.subscription_id == "chargify_subscription_id"

    def test_source_fields_partial_override(self, base_config: dict) -> None:
        """Override only customer id column via nested config."""
        base_config["customers"] = {
            "source_fields": {
                "customer_id": "my_customer_id",
                "metadata": [["my_customer_id", "my_customer_id"]],
            },
        }
        config = parse_config(base_config)
        assert config.customers.source_fields.customer_id == "my_customer_id"
        assert config.customers.source_fields.metadata == [("my_customer_id", "my_customer_id")]
        assert (
            config.customers.source_fields.stripe_metadata_key_for_source_id() == "my_customer_id"
        )

    def test_rejects_legacy_flat_source_fields(self, base_config: dict) -> None:
        """Unknown top-level keys (e.g. legacy layout) are rejected."""
        base_config["source_fields"] = {"customer_source_id_field": "x"}
        with pytest.raises(ValidationError):
            parse_config(base_config)

    def test_nested_config_explicit(self, base_config: dict) -> None:
        """Accept fully nested customers/subscriptions config."""
        base_config["customers"] = {
            "source_fields": {
                "customer_id": "ext_cust",
                "metadata": [["ext_cust", "stripe_cust_key"]],
            },
            "add_test_payment_methods": False,
        }
        base_config["subscriptions"] = {
            "source_fields": {
                "subscription_id": "ext_sub",
                "metadata": [["ext_sub", "stripe_sub_key"]],
            },
            "default_currency": "eur",
            "plan_code_to_price_id": {},
            "past_due_handling": "skip",
        }
        config = parse_config(base_config)
        assert config.customers.source_fields.customer_id == "ext_cust"
        assert config.subscriptions.default_currency == "eur"


class TestCustomerSourceFieldsConfig:
    """Tests for CustomerSourceFieldsConfig."""

    def test_defaults(self) -> None:
        """Test default values."""
        config = CustomerSourceFieldsConfig()
        assert config.customer_id == "source_customer_id"
        assert config.stripe_metadata_key_for_source_id() == "source_customer_id"

    def test_duplicate_record_field_rejected(self) -> None:
        """Duplicate source columns in metadata must fail validation."""
        with pytest.raises(ValueError, match="Duplicate record_field"):
            CustomerSourceFieldsConfig(
                customer_id="a",
                metadata=[("a", "x"), ("a", "y")],
            )

    def test_missing_entity_pair_rejected(self) -> None:
        """Metadata must include the canonical customer_id pair."""
        with pytest.raises(ValueError, match="exactly one pair"):
            CustomerSourceFieldsConfig(
                customer_id="source_customer_id",
                metadata=[("other", "other")],
            )


class TestSubscriptionSourceFieldsConfig:
    """Tests for SubscriptionSourceFieldsConfig."""

    def test_defaults(self) -> None:
        config = SubscriptionSourceFieldsConfig()
        assert config.subscription_id == "source_subscription_id"
        assert config.stripe_metadata_key_for_source_id() == "source_subscription_id"

    def test_payment_method_id_defaults_to_none(self) -> None:
        config = SubscriptionSourceFieldsConfig()
        assert config.payment_method_id is None

    def test_payment_method_id_can_be_set(self) -> None:
        config = SubscriptionSourceFieldsConfig(
            subscription_id="source_subscription_id",
            metadata=[("source_subscription_id", "source_subscription_id")],
            payment_method_id="stripe_payment_method_id",
        )
        assert config.payment_method_id == "stripe_payment_method_id"

    def test_plan_code_field_defaults_to_none(self) -> None:
        config = SubscriptionSourceFieldsConfig()
        assert config.plan_code_field is None

    def test_plan_code_field_can_be_set(self) -> None:
        config = SubscriptionSourceFieldsConfig(
            subscription_id="source_subscription_id",
            metadata=[("source_subscription_id", "source_subscription_id")],
            plan_code_field="source_product_handle",
        )
        assert config.plan_code_field == "source_product_handle"

    def test_all_new_fields_in_full_config(self, base_config: dict) -> None:
        """Test that payment_method_id and plan_code_field are accepted in full config."""
        base_config["subscriptions"] = {
            "source_fields": {
                "subscription_id": "ext_sub_id",
                "subscription_customer_id": "customer_ref",
                "payment_method_id": "payment_method_ref",
                "plan_code_field": "source_product_handle",
                "cancel_at_period_end": "cancel_at_end",
                "billing_cycle_anchor": "period_ends_at",
                "backdate_start": "period_started_at",
                "metadata": [
                    ["ext_sub_id", "ext_sub_id"],
                ],
            },
            "plan_code_to_price_id": {"basic": "price_123"},
            "past_due_handling": "create_fresh",
        }
        config = parse_config(base_config)
        assert config.subscriptions.source_fields.payment_method_id == "payment_method_ref"
        assert config.subscriptions.source_fields.plan_code_field == "source_product_handle"


class TestTestPaymentMethods:
    """Tests for test payment methods configuration."""

    def test_add_test_payment_methods_in_test_mode(self, base_config: dict) -> None:
        """Test that test payment methods can be enabled in test mode."""
        base_config["customers"] = {"add_test_payment_methods": True}
        config = parse_config(base_config)
        assert config.customers.add_test_payment_methods is True
        assert config.stripe_mode == StripeMode.TEST

    def test_add_test_payment_methods_in_live_mode_fails(self, base_config: dict) -> None:
        """Test that test payment methods cannot be enabled in live mode."""
        base_config["stripe_api_key"] = "sk_live_1234567890abcdefghijklmnop"
        base_config["stripe_mode"] = "live"
        base_config["customers"] = {"add_test_payment_methods": True}
        with pytest.raises(
            ValueError,
            match="add_test_payment_methods can only be enabled in test mode",
        ):
            parse_config(base_config)

    def test_add_test_payment_methods_defaults_to_false(self, base_config: dict) -> None:
        """Test that add_test_payment_methods defaults to False."""
        config = parse_config(base_config)
        assert config.customers.add_test_payment_methods is False

    def test_check_existing_defaults_to_true(self, base_config: dict) -> None:
        """Test that customers.check_existing defaults to True."""
        config = parse_config(base_config)
        assert config.customers.check_existing is True

    def test_check_existing_can_be_disabled(self, base_config: dict) -> None:
        """Test that customers.check_existing can be set to False."""
        base_config["customers"] = {"check_existing": False}
        config = parse_config(base_config)
        assert config.customers.check_existing is False

    def test_skip_mapped_records_defaults_to_false(self, base_config: dict) -> None:
        """Test that skip_mapped_records defaults to False on both streams."""
        config = parse_config(base_config)
        assert config.customers.skip_mapped_records is False
        assert config.subscriptions.skip_mapped_records is False

    def test_update_existing_defaults_to_false(self, base_config: dict) -> None:
        """Test that customers.update_existing defaults to False."""
        config = parse_config(base_config)
        assert config.customers.update_existing is False


class TestParseConfig:
    """Tests for parse_config function."""

    def test_parse_valid_config(self, base_config: dict) -> None:
        """Test parsing valid configuration."""
        config = parse_config(base_config)
        assert isinstance(config, TargetStripeConfig)

    def test_parse_idempotency_string(self, base_config: dict) -> None:
        """Test parsing idempotency as string."""
        base_config["idempotency"] = "hash"
        config = parse_config(base_config)
        assert config.idempotency.strategy == IdempotencyStrategy.HASH

    def test_parse_idempotency_dict(self, base_config: dict) -> None:
        """Test parsing idempotency as dict."""
        base_config["idempotency"] = {"strategy": "hash"}
        config = parse_config(base_config)
        assert config.idempotency.strategy == IdempotencyStrategy.HASH
