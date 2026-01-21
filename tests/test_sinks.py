"""Tests for Singer SDK Sinks."""

from __future__ import annotations

from typing import Any

import pytest

from target_stripe.config import SourceFieldsConfig
from target_stripe.sinks import (
    CustomerSink,
    SubscriptionSink,
    get_sink_class,
)


class TestGetSinkClass:
    """Tests for get_sink_class function."""

    def test_customers_stream(self) -> None:
        """Test getting sink for customers stream."""
        assert get_sink_class("customers") == CustomerSink

    def test_customer_singular(self) -> None:
        """Test getting sink for customer (singular) stream."""
        assert get_sink_class("customer") == CustomerSink

    def test_stripe_customers(self) -> None:
        """Test getting sink for stripe_customers stream."""
        assert get_sink_class("stripe_customers") == CustomerSink

    def test_subscriptions_stream(self) -> None:
        """Test getting sink for subscriptions stream."""
        assert get_sink_class("subscriptions") == SubscriptionSink

    def test_subscription_singular(self) -> None:
        """Test getting sink for subscription (singular) stream."""
        assert get_sink_class("subscription") == SubscriptionSink

    def test_stripe_subscriptions(self) -> None:
        """Test getting sink for stripe_subscriptions stream."""
        assert get_sink_class("stripe_subscriptions") == SubscriptionSink

    def test_unknown_stream(self) -> None:
        """Test getting sink for unknown stream returns None."""
        assert get_sink_class("unknown_stream") is None

    def test_case_insensitive(self) -> None:
        """Test that stream name lookup is case insensitive."""
        assert get_sink_class("CUSTOMERS") == CustomerSink
        assert get_sink_class("Subscriptions") == SubscriptionSink

    def test_dash_to_underscore(self) -> None:
        """Test that dashes are converted to underscores."""
        assert get_sink_class("stripe-customers") == CustomerSink


class TestCustomerSinkTransform:
    """Tests for CustomerSink field transformation."""

    @pytest.fixture
    def customer_sink_instance(self) -> CustomerSink:
        """Create a CustomerSink for testing transformation methods.

        Note: This creates a minimal sink for testing the transform methods only.
        """

        class MockSink(CustomerSink):
            def __init__(self) -> None:
                self._source_fields = SourceFieldsConfig()

            @property
            def source_fields(self) -> SourceFieldsConfig:
                return self._source_fields

        return MockSink()

    def test_extract_source_id_configured_field(
        self,
        customer_sink_instance: CustomerSink,
        sample_customer_record: dict[str, Any],
    ) -> None:
        """Test extracting source ID from configured source_customer_id field."""
        source_id = customer_sink_instance._extract_source_id(sample_customer_record)
        assert source_id == "12345"

    def test_extract_source_id_customer_id(
        self,
        customer_sink_instance: CustomerSink,
    ) -> None:
        """Test extracting source ID from customer_id field."""
        record = {"customer_id": "cust_789"}
        source_id = customer_sink_instance._extract_source_id(record)
        assert source_id == "cust_789"

    def test_extract_source_id_from_metadata(
        self,
        customer_sink_instance: CustomerSink,
    ) -> None:
        """Test extracting source ID from metadata using configured key."""
        record = {
            "email": "test@example.com",
            "metadata": {"source_customer_id": "meta_123"},
        }
        source_id = customer_sink_instance._extract_source_id(record)
        assert source_id == "meta_123"

    def test_extract_source_id_missing(
        self,
        customer_sink_instance: CustomerSink,
    ) -> None:
        """Test that missing source ID raises ValueError."""
        record = {"email": "test@example.com"}
        with pytest.raises(ValueError, match="Cannot extract source ID"):
            customer_sink_instance._extract_source_id(record)

    def test_transform_record_basic(
        self,
        customer_sink_instance: CustomerSink,
    ) -> None:
        """Test basic record transformation."""
        record = {
            "email": "test@example.com",
            "name": "Test User",
            "phone": "+1234567890",
        }
        result = customer_sink_instance._transform_record(record)

        assert result["email"] == "test@example.com"
        assert result["name"] == "Test User"
        assert result["phone"] == "+1234567890"

    def test_transform_record_name_from_parts(
        self,
        customer_sink_instance: CustomerSink,
    ) -> None:
        """Test name construction from first_name and last_name."""
        record = {
            "email": "test@example.com",
            "first_name": "John",
            "last_name": "Doe",
        }
        result = customer_sink_instance._transform_record(record)
        assert result["name"] == "John Doe"

    def test_transform_record_full_name_priority(
        self,
        customer_sink_instance: CustomerSink,
    ) -> None:
        """Test that full_name takes priority over first_name/last_name."""
        record = {
            "email": "test@example.com",
            "full_name": "Dr. John Smith",
            "first_name": "John",
            "last_name": "Doe",
        }
        result = customer_sink_instance._transform_record(record)
        assert result["name"] == "Dr. John Smith"

    def test_transform_record_address(
        self,
        customer_sink_instance: CustomerSink,
        sample_customer_record: dict[str, Any],
    ) -> None:
        """Test address field transformation."""
        result = customer_sink_instance._transform_record(sample_customer_record)

        assert "address" in result
        assert result["address"]["line1"] == "123 Main St"
        assert result["address"]["city"] == "San Francisco"
        assert result["address"]["state"] == "CA"
        assert result["address"]["postal_code"] == "94105"
        assert result["address"]["country"] == "US"

    def test_transform_record_address_object(
        self,
        customer_sink_instance: CustomerSink,
    ) -> None:
        """Test that existing address object is preserved."""
        record = {
            "email": "test@example.com",
            "address": {
                "line1": "456 Oak Ave",
                "city": "Los Angeles",
            },
        }
        result = customer_sink_instance._transform_record(record)
        assert result["address"]["line1"] == "456 Oak Ave"

    def test_transform_record_additional_metadata_fields(
        self,
        customer_sink_instance: CustomerSink,
    ) -> None:
        """Test that additional_metadata_fields are extracted."""
        # Configure additional metadata fields
        customer_sink_instance._source_fields = SourceFieldsConfig(
            additional_metadata_fields=["salesforce_id", "hubspot_id"]
        )
        record = {
            "email": "test@example.com",
            "salesforce_id": "sf_123",
            "hubspot_id": "hub_456",
            "other_field": "should_not_be_copied",
        }
        result = customer_sink_instance._transform_record(record)
        assert result["salesforce_id"] == "sf_123"
        assert result["hubspot_id"] == "hub_456"
        assert "other_field" not in result

    def test_transform_record_metadata(
        self,
        customer_sink_instance: CustomerSink,
    ) -> None:
        """Test metadata field handling."""
        record = {
            "email": "test@example.com",
            "metadata": {"source": "chargify", "plan": "basic"},
        }
        result = customer_sink_instance._transform_record(record)
        assert result["metadata"]["source"] == "chargify"
        assert result["metadata"]["plan"] == "basic"

    def test_extract_address_from_flat_fields(
        self,
        customer_sink_instance: CustomerSink,
    ) -> None:
        """Test extracting address from flat record fields."""
        record = {
            "street": "123 Main St",
            "city": "Boston",
            "province": "MA",
            "zip_code": "02101",
            "country_code": "US",
        }
        address = customer_sink_instance._extract_address(record)

        assert address["line1"] == "123 Main St"
        assert address["city"] == "Boston"
        assert address["state"] == "MA"
        assert address["postal_code"] == "02101"
        assert address["country"] == "US"

    def test_extract_address_empty(
        self,
        customer_sink_instance: CustomerSink,
    ) -> None:
        """Test that empty address returns None."""
        record = {"email": "test@example.com"}
        address = customer_sink_instance._extract_address(record)
        assert address is None


class TestSubscriptionSinkTransform:
    """Tests for SubscriptionSink field transformation."""

    @pytest.fixture
    def subscription_sink_instance(self) -> SubscriptionSink:
        """Create a SubscriptionSink for testing transformation methods."""

        class MockSink(SubscriptionSink):
            def __init__(self) -> None:
                self._source_fields = SourceFieldsConfig()

            @property
            def source_fields(self) -> SourceFieldsConfig:
                return self._source_fields

        return MockSink()

    def test_extract_source_id_configured_field(
        self,
        subscription_sink_instance: SubscriptionSink,
        sample_subscription_record: dict[str, Any],
    ) -> None:
        """Test extracting source ID from configured source_subscription_id field."""
        source_id = subscription_sink_instance._extract_source_id(sample_subscription_record)
        assert source_id == "67890"

    def test_extract_source_id_subscription_id(
        self,
        subscription_sink_instance: SubscriptionSink,
    ) -> None:
        """Test extracting source ID from subscription_id field."""
        record = {"subscription_id": "sub_123", "customer_id": "cust_456"}
        source_id = subscription_sink_instance._extract_source_id(record)
        assert source_id == "sub_123"

    def test_extract_source_id_missing(
        self,
        subscription_sink_instance: SubscriptionSink,
    ) -> None:
        """Test that missing source ID raises ValueError."""
        record = {"customer_id": "cust_456", "price_id": "price_123"}
        with pytest.raises(ValueError, match="Cannot extract source ID"):
            subscription_sink_instance._extract_source_id(record)

    def test_transform_record_basic(
        self,
        subscription_sink_instance: SubscriptionSink,
        sample_subscription_record: dict[str, Any],
    ) -> None:
        """Test basic subscription record transformation."""
        result = subscription_sink_instance._transform_record(sample_subscription_record)

        assert result["customer_id"] == "12345"
        assert result["price_id"] == "price_test123"
        assert result["quantity"] == 1
        assert result["cancel_at_period_end"] is False

    def test_transform_record_plan_code(
        self,
        subscription_sink_instance: SubscriptionSink,
    ) -> None:
        """Test transformation with plan_code instead of price_id."""
        record = {
            "source_subscription_id": "sub_123",
            "customer_id": "cust_456",
            "plan_code": "basic_monthly",
        }
        result = subscription_sink_instance._transform_record(record)
        assert result["plan_code"] == "basic_monthly"
        assert "price_id" not in result

    def test_transform_record_product_handle(
        self,
        subscription_sink_instance: SubscriptionSink,
    ) -> None:
        """Test transformation with product_handle as plan code."""
        record = {
            "source_subscription_id": "sub_123",
            "customer_id": "cust_456",
            "product_handle": "enterprise_annual",
        }
        result = subscription_sink_instance._transform_record(record)
        assert result["plan_code"] == "enterprise_annual"

    def test_transform_record_customer_fields(
        self,
        subscription_sink_instance: SubscriptionSink,
    ) -> None:
        """Test various customer reference field names (fallback fields only)."""
        for field_name in [
            "customer_id",
            "stripe_customer_id",
            "customer",
        ]:
            record = {
                "source_subscription_id": "sub_123",
                field_name: "cust_value",
                "price_id": "price_123",
            }
            result = subscription_sink_instance._transform_record(record)
            assert result["customer_id"] == "cust_value"

    def test_transform_record_configured_customer_field(
        self,
    ) -> None:
        """Test using configured subscription_customer_id_field for source-specific fields."""
        from target_stripe.config import SourceFieldsConfig

        class MockSink(SubscriptionSink):
            def __init__(self) -> None:
                self._source_fields = SourceFieldsConfig(
                    subscription_customer_id_field="chargify_customer_id"
                )

            @property
            def source_fields(self) -> SourceFieldsConfig:
                return self._source_fields

        sink = MockSink()
        record = {
            "source_subscription_id": "sub_123",
            "chargify_customer_id": "chargify_cust_456",
            "price_id": "price_123",
        }
        result = sink._transform_record(record)
        assert result["customer_id"] == "chargify_cust_456"

    def test_transform_record_trial_end(
        self,
        subscription_sink_instance: SubscriptionSink,
    ) -> None:
        """Test trial_end field transformation."""
        record = {
            "source_subscription_id": "sub_123",
            "customer_id": "cust_456",
            "price_id": "price_123",
            "trial_end": "2025-06-30T23:59:59Z",
        }
        result = subscription_sink_instance._transform_record(record)
        assert result["trial_end"] == "2025-06-30T23:59:59Z"

    def test_transform_record_trial_ended_at(
        self,
        subscription_sink_instance: SubscriptionSink,
    ) -> None:
        """Test trial_ended_at as alternative field name."""
        record = {
            "source_subscription_id": "sub_123",
            "customer_id": "cust_456",
            "price_id": "price_123",
            "trial_ended_at": "2025-06-30T23:59:59Z",
        }
        result = subscription_sink_instance._transform_record(record)
        assert result["trial_end"] == "2025-06-30T23:59:59Z"

    def test_transform_record_coupon(
        self,
        subscription_sink_instance: SubscriptionSink,
    ) -> None:
        """Test coupon field transformation."""
        record = {
            "source_subscription_id": "sub_123",
            "customer_id": "cust_456",
            "price_id": "price_123",
            "coupon": "SUMMER20",
        }
        result = subscription_sink_instance._transform_record(record)
        assert result["coupon"] == "SUMMER20"

    def test_transform_record_coupon_code(
        self,
        subscription_sink_instance: SubscriptionSink,
    ) -> None:
        """Test coupon_code as alternative field name."""
        record = {
            "source_subscription_id": "sub_123",
            "customer_id": "cust_456",
            "price_id": "price_123",
            "coupon_code": "WINTER30",
        }
        result = subscription_sink_instance._transform_record(record)
        assert result["coupon"] == "WINTER30"

    def test_transform_record_metadata(
        self,
        subscription_sink_instance: SubscriptionSink,
    ) -> None:
        """Test metadata field handling."""
        record = {
            "source_subscription_id": "sub_123",
            "customer_id": "cust_456",
            "price_id": "price_123",
            "metadata": {"source": "migration", "batch": "2024-01"},
        }
        result = subscription_sink_instance._transform_record(record)
        assert result["metadata"]["source"] == "migration"
        assert result["metadata"]["batch"] == "2024-01"
