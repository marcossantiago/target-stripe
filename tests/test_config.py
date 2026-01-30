"""Tests for configuration module."""

from __future__ import annotations

import json
import tempfile

import pytest

from target_stripe.config import (
    IdempotencyStrategy,
    SourceFieldsConfig,
    StripeMode,
    TargetStripeConfig,
    parse_config,
)


class TestTargetStripeConfig:
    """Tests for TargetStripeConfig."""

    def test_valid_test_config(self, base_config: dict) -> None:
        """Test valid test mode configuration."""
        config = TargetStripeConfig(**base_config)
        assert config.stripe_mode == StripeMode.TEST
        assert config.default_currency == "usd"
        assert config.dry_run is False

    def test_valid_live_config(self, base_config: dict) -> None:
        """Test valid live mode configuration."""
        base_config["stripe_api_key"] = "sk_live_1234567890abcdefghijklmnop"
        base_config["stripe_mode"] = "live"
        config = TargetStripeConfig(**base_config)
        assert config.stripe_mode == StripeMode.LIVE

    def test_invalid_api_key_format(self, base_config: dict) -> None:
        """Test that invalid API key format raises error."""
        base_config["stripe_api_key"] = "invalid_key"
        with pytest.raises(ValueError, match="Invalid Stripe API key format"):
            TargetStripeConfig(**base_config)

    def test_mode_key_mismatch_test(self, base_config: dict) -> None:
        """Test that test key with live mode raises error."""
        base_config["stripe_mode"] = "live"
        with pytest.raises(ValueError, match="stripe_mode is 'live' but API key is for test"):
            TargetStripeConfig(**base_config)

    def test_mode_key_mismatch_live(self, base_config: dict) -> None:
        """Test that live key with test mode raises error."""
        base_config["stripe_api_key"] = "sk_live_1234567890abcdefghijklmnop"
        base_config["stripe_mode"] = "test"
        with pytest.raises(ValueError, match="stripe_mode is 'test' but API key is for live"):
            TargetStripeConfig(**base_config)

    def test_currency_normalization(self, base_config: dict) -> None:
        """Test that currency is normalized to lowercase."""
        base_config["default_currency"] = "USD"
        config = TargetStripeConfig(**base_config)
        assert config.default_currency == "usd"

    def test_idempotency_defaults(self, base_config: dict) -> None:
        """Test idempotency defaults."""
        config = TargetStripeConfig(**base_config)
        assert config.idempotency.strategy == IdempotencyStrategy.SOURCE_ID

    def test_idempotency_hash_strategy(self, base_config: dict) -> None:
        """Test hash idempotency strategy."""
        base_config["idempotency"] = {"strategy": "hash"}
        config = TargetStripeConfig(**base_config)
        assert config.idempotency.strategy == IdempotencyStrategy.HASH

    def test_plan_code_mapping(self, base_config: dict) -> None:
        """Test plan code to price ID mapping."""
        base_config["plan_code_to_price_id"] = {
            "basic": "price_basic123",
            "pro": "price_pro456",
        }
        config = TargetStripeConfig(**base_config)
        assert config.plan_code_to_price_id["basic"] == "price_basic123"
        assert config.plan_code_to_price_id["pro"] == "price_pro456"

    def test_plan_code_mapping_from_file(self, base_config: dict) -> None:
        """Test loading plan code mapping from JSON file."""
        mapping = {
            "basic": "price_from_file_basic",
            "enterprise": "price_from_file_enterprise",
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(mapping, f)
            f.flush()
            base_config["plan_code_mapping_file"] = f.name

        config = TargetStripeConfig(**base_config)
        assert config.plan_code_to_price_id["basic"] == "price_from_file_basic"
        assert config.plan_code_to_price_id["enterprise"] == "price_from_file_enterprise"

    def test_rate_limit_bounds(self, base_config: dict) -> None:
        """Test rate limit bounds validation."""
        base_config["rate_limit_per_sec"] = 0
        with pytest.raises(ValueError):
            TargetStripeConfig(**base_config)

        base_config["rate_limit_per_sec"] = 101
        with pytest.raises(ValueError):
            TargetStripeConfig(**base_config)

    def test_restricted_api_key(self, base_config: dict) -> None:
        """Test that restricted API keys are accepted."""
        base_config["stripe_api_key"] = "rk_test_1234567890abcdefghijklmnop"
        config = TargetStripeConfig(**base_config)
        assert config.stripe_api_key.startswith("rk_test_")

    def test_source_fields_defaults(self, base_config: dict) -> None:
        """Test source_fields has sensible defaults."""
        config = TargetStripeConfig(**base_config)
        assert config.source_fields.customer_source_id_field == "source_customer_id"
        assert config.source_fields.customer_metadata_key is None
        assert (
            config.source_fields.get_customer_metadata_key() == "source_customer_id"
        )  # Falls back
        assert config.source_fields.subscription_source_id_field == "source_subscription_id"
        assert config.source_fields.subscription_metadata_key is None
        assert (
            config.source_fields.get_subscription_metadata_key() == "source_subscription_id"
        )  # Falls back
        assert config.source_fields.additional_metadata_fields == []

    def test_source_fields_custom(self, base_config: dict) -> None:
        """Test custom source_fields configuration."""
        base_config["source_fields"] = {
            "customer_source_id_field": "chargify_customer_id",
            "customer_metadata_key": "chargify_customer_id",
            "subscription_source_id_field": "chargify_subscription_id",
            "subscription_metadata_key": "chargify_subscription_id",
            "additional_metadata_fields": ["salesforce_id", "hubspot_id"],
        }
        config = TargetStripeConfig(**base_config)
        assert config.source_fields.customer_source_id_field == "chargify_customer_id"
        assert config.source_fields.customer_metadata_key == "chargify_customer_id"
        assert config.source_fields.subscription_source_id_field == "chargify_subscription_id"
        assert config.source_fields.subscription_metadata_key == "chargify_subscription_id"
        assert config.source_fields.additional_metadata_fields == ["salesforce_id", "hubspot_id"]

    def test_source_fields_partial_override(self, base_config: dict) -> None:
        """Test partial override of source_fields."""
        base_config["source_fields"] = {
            "customer_source_id_field": "my_customer_id",
        }
        config = TargetStripeConfig(**base_config)
        assert config.source_fields.customer_source_id_field == "my_customer_id"
        # Other fields should have defaults
        assert config.source_fields.customer_metadata_key is None
        # Should fall back to customer_source_id_field
        assert config.source_fields.get_customer_metadata_key() == "my_customer_id"


class TestSourceFieldsConfig:
    """Tests for SourceFieldsConfig."""

    def test_defaults(self) -> None:
        """Test default values."""
        config = SourceFieldsConfig()
        assert config.customer_source_id_field == "source_customer_id"
        assert config.customer_metadata_key is None
        assert config.get_customer_metadata_key() == "source_customer_id"
        assert config.subscription_source_id_field == "source_subscription_id"
        assert config.subscription_metadata_key is None
        assert config.get_subscription_metadata_key() == "source_subscription_id"
        assert config.additional_metadata_fields == []

    def test_custom_values(self) -> None:
        """Test custom values with explicit metadata keys."""
        config = SourceFieldsConfig(
            customer_source_id_field="external_customer_id",
            customer_metadata_key="ext_cust_id",
            subscription_source_id_field="external_subscription_id",
            subscription_metadata_key="ext_sub_id",
            additional_metadata_fields=["custom_field_1", "custom_field_2"],
        )
        assert config.customer_source_id_field == "external_customer_id"
        assert config.customer_metadata_key == "ext_cust_id"
        assert config.get_customer_metadata_key() == "ext_cust_id"
        assert config.subscription_source_id_field == "external_subscription_id"
        assert config.subscription_metadata_key == "ext_sub_id"
        assert config.get_subscription_metadata_key() == "ext_sub_id"
        assert config.additional_metadata_fields == ["custom_field_1", "custom_field_2"]

    def test_metadata_key_fallback(self) -> None:
        """Test that metadata keys fall back to source_id_field when not explicitly set."""
        config = SourceFieldsConfig(
            customer_source_id_field="chargify_customer_id",
            subscription_source_id_field="chargify_subscription_id",
        )
        # Metadata keys should be None when not set
        assert config.customer_metadata_key is None
        assert config.subscription_metadata_key is None
        # But helper methods should return the source_id_field values
        assert config.get_customer_metadata_key() == "chargify_customer_id"
        assert config.get_subscription_metadata_key() == "chargify_subscription_id"


class TestTestPaymentMethods:
    """Tests for test payment methods configuration."""

    def test_add_test_payment_methods_in_test_mode(self, base_config: dict) -> None:
        """Test that test payment methods can be enabled in test mode."""
        base_config["add_test_payment_methods"] = True
        config = TargetStripeConfig(**base_config)
        assert config.add_test_payment_methods is True
        assert config.stripe_mode == StripeMode.TEST

    def test_add_test_payment_methods_in_live_mode_fails(self, base_config: dict) -> None:
        """Test that test payment methods cannot be enabled in live mode."""
        base_config["stripe_api_key"] = "sk_live_1234567890abcdefghijklmnop"
        base_config["stripe_mode"] = "live"
        base_config["add_test_payment_methods"] = True
        with pytest.raises(
            ValueError,
            match="add_test_payment_methods can only be enabled in test mode",
        ):
            TargetStripeConfig(**base_config)

    def test_add_test_payment_methods_defaults_to_false(self, base_config: dict) -> None:
        """Test that add_test_payment_methods defaults to False."""
        config = TargetStripeConfig(**base_config)
        assert config.add_test_payment_methods is False


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
