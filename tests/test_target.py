"""Tests for the main Target class."""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from target_stripe.sinks import CustomerSink, SubscriptionSink
from target_stripe.target import TargetStripe


class TestTargetStripe:
    """Tests for TargetStripe class."""

    @pytest.fixture
    def target_config(self, temp_db_path: Path) -> dict[str, Any]:
        """Create test configuration."""
        return {
            "stripe_api_key": "sk_test_1234567890abcdefghijklmnop",
            "stripe_mode": "test",
            "dry_run": True,
            "mapping_db_path": str(temp_db_path),
        }

    @pytest.fixture
    def target(
        self,
        target_config: dict[str, Any],
        mock_stripe: MagicMock,
    ) -> TargetStripe:
        """Create a test target instance."""
        return TargetStripe(config=target_config)

    def test_target_init(
        self,
        target: TargetStripe,
    ) -> None:
        """Test target initialization."""
        assert target.name == "target-stripe"
        assert target._parsed_config.dry_run is True

    def test_get_sink_class_customers(
        self,
        target: TargetStripe,
    ) -> None:
        """Test getting sink class for customers stream."""
        sink_class = target.get_sink_class("customers")
        assert sink_class == CustomerSink

    def test_get_sink_class_subscriptions(
        self,
        target: TargetStripe,
    ) -> None:
        """Test getting sink class for subscriptions stream."""
        sink_class = target.get_sink_class("subscriptions")
        assert sink_class == SubscriptionSink

    def test_get_sink_class_unknown(
        self,
        target: TargetStripe,
    ) -> None:
        """Test getting sink class for unknown stream raises error."""
        with pytest.raises(ValueError, match="Unsupported stream"):
            target.get_sink_class("unknown_stream")

    def test_state_emit_interval(
        self,
        target: TargetStripe,
    ) -> None:
        """Test state emit interval from config."""
        assert target.state_emit_interval == 100

    def test_config_schema_has_required_fields(self) -> None:
        """Test that config schema includes all required fields."""
        schema = TargetStripe.config_jsonschema
        properties = schema.get("properties", {})

        assert "stripe_api_key" in properties
        assert "stripe_mode" in properties
        assert "dry_run" in properties
        assert "hard_fail" in properties
        assert "idempotency" in properties
        assert "customers" in properties
        assert "subscriptions" in properties
        assert "state_emit_interval" in properties
        assert "rate_limit_per_sec" in properties

    def test_config_schema_source_fields(self) -> None:
        """Test that nested customers/subscriptions schema is defined."""
        schema = TargetStripe.config_jsonschema
        cust = schema["properties"]["customers"]
        ctype = cust["type"]
        assert "object" in ctype if isinstance(ctype, list) else ctype == "object"
        cprops = cust.get("properties", {})
        assert "source_fields" in cprops
        subs = schema["properties"]["subscriptions"]
        sprops = subs.get("properties", {})
        assert "source_fields" in sprops
        assert "plan_code_to_price_id" in sprops

    def test_config_schema_stripe_api_key_is_secret(self) -> None:
        """Test that stripe_api_key is marked as secret."""
        schema = TargetStripe.config_jsonschema
        api_key_schema = schema["properties"]["stripe_api_key"]
        assert api_key_schema.get("secret") is True


class TestTargetStripeIntegration:
    """Integration tests for TargetStripe with Singer messages."""

    @pytest.fixture
    def target_config(self, temp_db_path: Path) -> dict[str, Any]:
        """Create test configuration."""
        return {
            "stripe_api_key": "sk_test_1234567890abcdefghijklmnop",
            "stripe_mode": "test",
            "dry_run": True,
            "mapping_db_path": str(temp_db_path),
            "state_emit_interval": 2,
        }

    def create_singer_message(
        self,
        msg_type: str,
        **kwargs: Any,
    ) -> str:
        """Create a Singer message as JSON string."""
        message = {"type": msg_type, **kwargs}
        return json.dumps(message)

    def test_process_schema_message(
        self,
        target_config: dict[str, Any],
        mock_stripe: MagicMock,
    ) -> None:
        """Test processing a SCHEMA message."""
        schema_msg = self.create_singer_message(
            "SCHEMA",
            stream="customers",
            schema={
                "type": "object",
                "properties": {
                    "source_customer_id": {"type": "string"},
                    "email": {"type": "string"},
                },
            },
            key_properties=["source_customer_id"],
        )

        target = TargetStripe(config=target_config)

        input_stream = StringIO(schema_msg + "\n")
        with patch("sys.stdin", input_stream):
            target.listen(input_stream)

    def test_process_record_message(
        self,
        target_config: dict[str, Any],
        mock_stripe: MagicMock,
    ) -> None:
        """Test processing a RECORD message."""
        messages = [
            self.create_singer_message(
                "SCHEMA",
                stream="customers",
                schema={
                    "type": "object",
                    "properties": {
                        "source_customer_id": {"type": "string"},
                        "email": {"type": "string"},
                    },
                },
                key_properties=["source_customer_id"],
            ),
            self.create_singer_message(
                "RECORD",
                stream="customers",
                record={
                    "source_customer_id": "12345",
                    "email": "test@example.com",
                },
            ),
        ]

        target = TargetStripe(config=target_config)
        input_stream = StringIO("\n".join(messages) + "\n")
        target.listen(input_stream)

    def test_process_state_message(
        self,
        target_config: dict[str, Any],
        mock_stripe: MagicMock,
    ) -> None:
        """Test processing a STATE message."""
        messages = [
            self.create_singer_message(
                "STATE",
                value={"bookmarks": {"customers": {"last_id": "123"}}},
            ),
        ]

        target = TargetStripe(config=target_config)
        input_stream = StringIO("\n".join(messages) + "\n")
        target.listen(input_stream)

    def test_multiple_streams(
        self,
        target_config: dict[str, Any],
        mock_stripe: MagicMock,
    ) -> None:
        """Test processing records from multiple streams."""
        messages = [
            self.create_singer_message(
                "SCHEMA",
                stream="customers",
                schema={"type": "object", "properties": {}},
                key_properties=["source_customer_id"],
            ),
            self.create_singer_message(
                "SCHEMA",
                stream="subscriptions",
                schema={"type": "object", "properties": {}},
                key_properties=["source_subscription_id"],
            ),
            self.create_singer_message(
                "RECORD",
                stream="customers",
                record={
                    "source_customer_id": "cust_1",
                    "email": "cust1@example.com",
                },
            ),
            self.create_singer_message(
                "RECORD",
                stream="customers",
                record={
                    "source_customer_id": "cust_2",
                    "email": "cust2@example.com",
                },
            ),
        ]

        target = TargetStripe(config=target_config)
        input_stream = StringIO("\n".join(messages) + "\n")
        target.listen(input_stream)
