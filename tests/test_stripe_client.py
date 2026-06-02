"""Tests for Stripe client wrapper."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from target_stripe.config import TargetStripeConfig
from target_stripe.mapping import EntityType, MappingStore
from target_stripe.stripe_client import (
    RateLimiter,
    StripeClientWrapper,
    is_transient_error,
)


class TestRateLimiter:
    """Tests for RateLimiter."""

    def test_rate_limiter_allows_requests(self) -> None:
        """Test that rate limiter allows requests."""
        limiter = RateLimiter(rate_per_second=100)
        limiter.acquire()  # Should not block or raise

    def test_rate_limiter_min_interval(self) -> None:
        """Test that rate limiter enforces minimum interval."""
        limiter = RateLimiter(rate_per_second=10)
        assert limiter.min_interval == 0.1


class TestIsTransientError:
    """Tests for is_transient_error function."""

    def test_rate_limit_error_is_transient(self) -> None:
        """Test that rate limit errors are transient."""
        with patch("target_stripe.stripe_client.stripe") as mock_stripe:
            mock_stripe.RateLimitError = type("RateLimitError", (Exception,), {})

            with patch(
                "target_stripe.stripe_client.stripe.RateLimitError",
                mock_stripe.RateLimitError,
            ):
                from target_stripe.stripe_client import StripeTransientError

                transient = StripeTransientError("test")
                assert is_transient_error(transient) is True


class TestStripeClientWrapper:
    """Tests for StripeClientWrapper."""

    @pytest.fixture
    def stripe_client(
        self,
        parsed_config: TargetStripeConfig,
        mapping_store: MappingStore,
        mock_stripe: MagicMock,
    ) -> StripeClientWrapper:
        """Create a test Stripe client."""
        return StripeClientWrapper(
            config=parsed_config,
            mapping_store=mapping_store,
        )

    def test_init(
        self,
        stripe_client: StripeClientWrapper,
        parsed_config: TargetStripeConfig,
    ) -> None:
        """Test client initialization."""
        assert stripe_client.config == parsed_config
        assert stripe_client.dry_run is False
        assert stripe_client.stats["customers_created"] == 0

    def test_upsert_customer_dry_run(
        self,
        parsed_config: TargetStripeConfig,
        mapping_store: MappingStore,
        mock_stripe: MagicMock,
    ) -> None:
        """Test customer upsert in dry run mode."""
        parsed_config.dry_run = True
        client = StripeClientWrapper(parsed_config, mapping_store)

        stripe_id, was_created, was_updated = client.upsert_customer(
            source_id="12345",
            data={
                "email": "test@example.com",
                "name": "Test User",
            },
        )

        assert stripe_id.startswith("dry_run_cus_")
        assert was_created is True
        assert was_updated is False
        mock_stripe.Customer.create.assert_not_called()

    def test_upsert_customer_create(
        self,
        stripe_client: StripeClientWrapper,
        mock_stripe: MagicMock,
        mock_stripe_customer: MagicMock,
    ) -> None:
        """Test creating a new customer."""
        mock_stripe.Customer.search.return_value = MagicMock(data=[])
        mock_stripe.Customer.create.return_value = mock_stripe_customer

        stripe_id, was_created, was_updated = stripe_client.upsert_customer(
            source_id="12345",
            data={
                "email": "test@example.com",
                "name": "Test User",
            },
        )

        assert stripe_id == "cus_test123"
        assert was_created is True
        assert was_updated is False
        assert stripe_client.stats["customers_created"] == 1
        mock_stripe.Customer.create.assert_called_once()

    def test_upsert_customer_update_existing(
        self,
        parsed_config: TargetStripeConfig,
        mapping_store: MappingStore,
        mock_stripe: MagicMock,
        mock_stripe_customer: MagicMock,
    ) -> None:
        """Test updating an existing customer when update_existing is enabled."""
        parsed_config.customers.update_existing = True
        client = StripeClientWrapper(parsed_config, mapping_store)
        mapping_store.set_mapping(EntityType.CUSTOMER, "12345", "cus_existing")
        mock_stripe.Customer.retrieve.return_value = mock_stripe_customer
        mock_stripe.Customer.modify.return_value = mock_stripe_customer
        mock_stripe_customer.id = "cus_existing"

        stripe_id, was_created, was_updated = client.upsert_customer(
            source_id="12345",
            data={
                "email": "updated@example.com",
                "name": "Updated User",
            },
        )

        assert stripe_id == "cus_existing"
        assert was_created is False
        assert was_updated is True
        assert client.stats["customers_updated"] == 1
        mock_stripe.Customer.modify.assert_called_once()

    def test_upsert_customer_link_existing_without_update(
        self,
        stripe_client: StripeClientWrapper,
        mapping_store: MappingStore,
        mock_stripe: MagicMock,
        mock_stripe_customer: MagicMock,
    ) -> None:
        """Test linking an existing customer without Customer.modify (default)."""
        mapping_store.set_mapping(EntityType.CUSTOMER, "12345", "cus_existing")
        mock_stripe.Customer.retrieve.return_value = mock_stripe_customer
        mock_stripe_customer.id = "cus_existing"

        stripe_id, was_created, was_updated = stripe_client.upsert_customer(
            source_id="12345",
            data={
                "email": "updated@example.com",
                "name": "Updated User",
            },
        )

        assert stripe_id == "cus_existing"
        assert was_created is False
        assert was_updated is False
        assert stripe_client.stats["customers_updated"] == 0
        mock_stripe.Customer.modify.assert_not_called()

    def test_upsert_customer_check_existing_disabled(
        self,
        parsed_config: TargetStripeConfig,
        mapping_store: MappingStore,
        mock_stripe: MagicMock,
        mock_stripe_customer: MagicMock,
    ) -> None:
        """Test that check_existing=false skips Stripe email lookup."""
        parsed_config.customers.check_existing = False
        client = StripeClientWrapper(parsed_config, mapping_store)

        mock_stripe.Customer.create.return_value = mock_stripe_customer

        stripe_id, was_created, was_updated = client.upsert_customer(
            source_id="12345",
            data={
                "email": "test@example.com",
                "name": "Test User",
            },
        )

        assert stripe_id == "cus_test123"
        assert was_created is True
        assert was_updated is False
        mock_stripe.Customer.list.assert_not_called()
        mock_stripe.Customer.create.assert_called_once()

    def test_resolve_customer_id_stripe_format(
        self,
        stripe_client: StripeClientWrapper,
    ) -> None:
        """Test resolving a Stripe-format customer ID."""
        result = stripe_client.resolve_customer_id("cus_test123")
        assert result == "cus_test123"

    def test_resolve_customer_id_from_mapping(
        self,
        stripe_client: StripeClientWrapper,
        mapping_store: MappingStore,
    ) -> None:
        """Test resolving customer ID from mapping store."""
        mapping_store.set_mapping(EntityType.CUSTOMER, "source_123", "cus_mapped")
        result = stripe_client.resolve_customer_id("source_123")
        assert result == "cus_mapped"

    def test_resolve_customer_id_not_found(
        self,
        stripe_client: StripeClientWrapper,
    ) -> None:
        """Test resolving customer ID that doesn't exist."""
        result = stripe_client.resolve_customer_id("unknown_source")
        assert result is None

    def test_resolve_price_id_direct(
        self,
        stripe_client: StripeClientWrapper,
    ) -> None:
        """Test resolving a direct price ID."""
        result = stripe_client.resolve_price_id(
            plan_code=None,
            price_id="price_test123",
        )
        assert result == "price_test123"

    def test_resolve_price_id_from_mapping(
        self,
        parsed_config: TargetStripeConfig,
        mapping_store: MappingStore,
        mock_stripe: MagicMock,
    ) -> None:
        """Test resolving price ID from plan code mapping."""
        parsed_config.subscriptions.plan_code_to_price_id = {
            "basic": "price_basic123",
            "pro": "price_pro456",
        }
        client = StripeClientWrapper(parsed_config, mapping_store)

        result = client.resolve_price_id(
            plan_code="basic",
            price_id=None,
        )
        assert result == "price_basic123"

    def test_resolve_price_id_price_format_plan_code(
        self,
        stripe_client: StripeClientWrapper,
    ) -> None:
        """Test resolving plan code that looks like a price ID."""
        result = stripe_client.resolve_price_id(
            plan_code="price_already_valid",
            price_id=None,
        )
        assert result == "price_already_valid"

    def test_resolve_price_id_not_found(
        self,
        stripe_client: StripeClientWrapper,
    ) -> None:
        """Test resolving price ID that can't be found."""
        with pytest.raises(ValueError, match="Cannot resolve price"):
            stripe_client.resolve_price_id(
                plan_code="unknown_plan",
                price_id=None,
            )

    def test_upsert_subscription_missing_customer(
        self,
        stripe_client: StripeClientWrapper,
    ) -> None:
        """Test subscription upsert with missing customer reference."""
        with pytest.raises(ValueError, match="missing customer reference"):
            stripe_client.upsert_subscription(
                source_id="sub_123",
                data={
                    "price_id": "price_test",
                },
            )

    def test_upsert_subscription_customer_not_found(
        self,
        stripe_client: StripeClientWrapper,
    ) -> None:
        """Test subscription upsert when customer doesn't exist."""
        with pytest.raises(ValueError, match="Customer not found"):
            stripe_client.upsert_subscription(
                source_id="sub_123",
                data={
                    "customer_id": "nonexistent_customer",
                    "price_id": "price_test",
                },
            )

    def test_upsert_subscription_dry_run(
        self,
        parsed_config: TargetStripeConfig,
        mapping_store: MappingStore,
        mock_stripe: MagicMock,
    ) -> None:
        """Test subscription upsert in dry run mode."""
        parsed_config.dry_run = True
        mapping_store.set_mapping(EntityType.CUSTOMER, "cust_123", "cus_stripe_123")
        client = StripeClientWrapper(parsed_config, mapping_store)

        stripe_id, was_created = client.upsert_subscription(
            source_id="sub_123",
            data={
                "customer_id": "cust_123",
                "price_id": "price_test",
            },
        )

        assert stripe_id.startswith("dry_run_sub_")
        assert was_created is True
        mock_stripe.Subscription.create.assert_not_called()

    def test_upsert_subscription_create(
        self,
        stripe_client: StripeClientWrapper,
        mapping_store: MappingStore,
        mock_stripe: MagicMock,
        mock_stripe_subscription: MagicMock,
    ) -> None:
        """Test creating a new subscription."""
        mapping_store.set_mapping(EntityType.CUSTOMER, "cust_123", "cus_stripe_123")
        mock_stripe.Subscription.create.return_value = mock_stripe_subscription

        stripe_id, was_created = stripe_client.upsert_subscription(
            source_id="sub_123",
            data={
                "customer_id": "cust_123",
                "price_id": "price_test123",
                "quantity": 2,
            },
        )

        assert stripe_id == "sub_test123"
        assert was_created is True
        assert stripe_client.stats["subscriptions_created"] == 1

    def test_upsert_subscription_update_existing(
        self,
        stripe_client: StripeClientWrapper,
        mapping_store: MappingStore,
        mock_stripe: MagicMock,
        mock_stripe_subscription: MagicMock,
    ) -> None:
        """Test updating an existing subscription."""
        mapping_store.set_mapping(EntityType.CUSTOMER, "cust_123", "cus_stripe_123")
        mapping_store.set_mapping(EntityType.SUBSCRIPTION, "sub_123", "sub_existing")
        mock_stripe.Subscription.modify.return_value = mock_stripe_subscription
        mock_stripe_subscription.id = "sub_existing"

        stripe_id, was_created = stripe_client.upsert_subscription(
            source_id="sub_123",
            data={
                "customer_id": "cust_123",
                "price_id": "price_test123",
                "cancel_at_period_end": True,
            },
        )

        assert stripe_id == "sub_existing"
        assert was_created is False
        assert stripe_client.stats["subscriptions_updated"] == 1

    def test_upsert_subscription_with_trial_end(
        self,
        stripe_client: StripeClientWrapper,
        mapping_store: MappingStore,
        mock_stripe: MagicMock,
        mock_stripe_subscription: MagicMock,
    ) -> None:
        """Test subscription with trial_end timestamp."""
        mapping_store.set_mapping(EntityType.CUSTOMER, "cust_123", "cus_stripe_123")
        mock_stripe.Subscription.create.return_value = mock_stripe_subscription

        stripe_id, was_created = stripe_client.upsert_subscription(
            source_id="sub_123",
            data={
                "customer_id": "cust_123",
                "price_id": "price_test123",
                "trial_end": "2025-12-31T23:59:59Z",
            },
        )

        assert stripe_id == "sub_test123"
        call_kwargs = mock_stripe.Subscription.create.call_args[1]
        assert "trial_end" in call_kwargs

    def test_upsert_subscription_with_trial_end_now(
        self,
        stripe_client: StripeClientWrapper,
        mapping_store: MappingStore,
        mock_stripe: MagicMock,
        mock_stripe_subscription: MagicMock,
    ) -> None:
        """Test subscription with trial_end='now'."""
        mapping_store.set_mapping(EntityType.CUSTOMER, "cust_123", "cus_stripe_123")
        mock_stripe.Subscription.create.return_value = mock_stripe_subscription

        stripe_id, was_created = stripe_client.upsert_subscription(
            source_id="sub_123",
            data={
                "customer_id": "cust_123",
                "price_id": "price_test123",
                "trial_end": "now",
            },
        )

        assert stripe_id == "sub_test123"
        call_kwargs = mock_stripe.Subscription.create.call_args[1]
        assert call_kwargs.get("trial_end") == "now"

    def test_stats_tracking(
        self,
        stripe_client: StripeClientWrapper,
    ) -> None:
        """Test that stats are tracked correctly."""
        stats = stripe_client.stats
        assert "customers_created" in stats
        assert "customers_updated" in stats
        assert "subscriptions_created" in stats
        assert "subscriptions_updated" in stats
        assert "errors" in stats
        assert "retries" in stats


class TestPaymentMethods:
    """Tests for payment method functionality."""

    @pytest.fixture
    def test_config(
        self,
        parsed_config: TargetStripeConfig,
    ) -> TargetStripeConfig:
        """Create config with test payment methods enabled."""
        parsed_config.customers.add_test_payment_methods = True
        return parsed_config

    def test_attach_test_payment_method(
        self,
        test_config: TargetStripeConfig,
        mapping_store: MappingStore,
        mock_stripe: MagicMock,
    ) -> None:
        """Test attaching a test payment method to a customer."""
        client = StripeClientWrapper(test_config, mapping_store)

        mock_payment_method = MagicMock()
        mock_payment_method.id = "pm_card_visa"
        mock_stripe.PaymentMethod.attach.return_value = mock_payment_method

        pm_id = client.attach_test_payment_method("cus_test123")

        assert pm_id == "pm_card_visa"
        mock_stripe.PaymentMethod.attach.assert_called_once_with(
            "pm_card_visa",
            customer="cus_test123",
        )
        mock_stripe.Customer.modify.assert_called_once_with(
            "cus_test123",
            invoice_settings={"default_payment_method": "pm_card_visa"},
        )

    def test_attach_test_payment_method_not_in_test_mode(
        self,
        parsed_config: TargetStripeConfig,
        mapping_store: MappingStore,
        mock_stripe: MagicMock,
    ) -> None:
        """Test that payment methods cannot be attached in live mode."""
        parsed_config.stripe_mode = "live"
        parsed_config.stripe_api_key = "sk_live_test123"
        client = StripeClientWrapper(parsed_config, mapping_store)

        with pytest.raises(
            ValueError, match="Test payment methods can only be attached in test mode"
        ):
            client.attach_test_payment_method("cus_test123")

    def test_attach_test_payment_method_flag_not_enabled(
        self,
        parsed_config: TargetStripeConfig,
        mapping_store: MappingStore,
        mock_stripe: MagicMock,
    ) -> None:
        """Test that payment methods cannot be attached without flag enabled."""
        client = StripeClientWrapper(parsed_config, mapping_store)

        with pytest.raises(
            ValueError,
            match="add_test_payment_methods must be enabled to attach test payment methods",
        ):
            client.attach_test_payment_method("cus_test123")

    def test_attach_test_payment_method_dry_run(
        self,
        test_config: TargetStripeConfig,
        mapping_store: MappingStore,
        mock_stripe: MagicMock,
    ) -> None:
        """Test attaching test payment method in dry run mode."""
        test_config.dry_run = True
        client = StripeClientWrapper(test_config, mapping_store)

        pm_id = client.attach_test_payment_method("cus_test123")

        assert pm_id.startswith("dry_run_pm_")
        mock_stripe.PaymentMethod.create.assert_not_called()

    def test_upsert_customer_with_test_payment_method(
        self,
        test_config: TargetStripeConfig,
        mapping_store: MappingStore,
        mock_stripe: MagicMock,
    ) -> None:
        """Test that creating a customer automatically attaches payment method when flag is enabled."""
        client = StripeClientWrapper(test_config, mapping_store)

        mock_customer = MagicMock()
        mock_customer.id = "cus_test123"
        mock_stripe.Customer.create.return_value = mock_customer

        mock_payment_method = MagicMock()
        mock_payment_method.id = "pm_card_visa"
        mock_stripe.PaymentMethod.attach.return_value = mock_payment_method

        stripe_id, was_created, was_updated = client.upsert_customer(
            source_id="12345",
            data={
                "email": "test@example.com",
                "name": "Test User",
            },
        )

        assert stripe_id == "cus_test123"
        assert was_created is True
        assert was_updated is False
        # Payment method should be attached
        mock_stripe.PaymentMethod.attach.assert_called_once()

    def test_subscription_collection_method_with_test_payment_methods(
        self,
        test_config: TargetStripeConfig,
        mapping_store: MappingStore,
        mock_stripe: MagicMock,
    ) -> None:
        """Test that subscriptions use charge_automatically when test payment methods are enabled."""
        client = StripeClientWrapper(test_config, mapping_store)

        mapping_store.set_mapping(EntityType.CUSTOMER, "cust_123", "cus_stripe_123")

        mock_subscription = MagicMock()
        mock_subscription.id = "sub_test123"
        mock_stripe.Subscription.create.return_value = mock_subscription

        stripe_id, was_created = client.upsert_subscription(
            source_id="sub_123",
            data={
                "customer_id": "cust_123",
                "price_id": "price_test123",
            },
        )

        assert stripe_id == "sub_test123"
        call_kwargs = mock_stripe.Subscription.create.call_args[1]
        assert call_kwargs.get("collection_method") == "charge_automatically"
        assert "days_until_due" not in call_kwargs

    def test_subscription_collection_method_without_test_payment_methods(
        self,
        parsed_config: TargetStripeConfig,
        mapping_store: MappingStore,
        mock_stripe: MagicMock,
    ) -> None:
        """Test that subscriptions use send_invoice when test payment methods are not enabled."""
        client = StripeClientWrapper(parsed_config, mapping_store)

        mapping_store.set_mapping(EntityType.CUSTOMER, "cust_123", "cus_stripe_123")

        mock_subscription = MagicMock()
        mock_subscription.id = "sub_test123"
        mock_stripe.Subscription.create.return_value = mock_subscription

        stripe_id, was_created = client.upsert_subscription(
            source_id="sub_123",
            data={
                "customer_id": "cust_123",
                "price_id": "price_test123",
            },
        )

        assert stripe_id == "sub_test123"
        call_kwargs = mock_stripe.Subscription.create.call_args[1]
        assert call_kwargs.get("collection_method") == "send_invoice"
        assert call_kwargs.get("days_until_due") == 30

    def test_subscription_with_explicit_payment_method_id(
        self,
        parsed_config: TargetStripeConfig,
        mapping_store: MappingStore,
        mock_stripe: MagicMock,
    ) -> None:
        """Test that a per-subscription payment_method_id sets default_payment_method."""
        client = StripeClientWrapper(parsed_config, mapping_store)
        mapping_store.set_mapping(EntityType.CUSTOMER, "cust_123", "cus_stripe_123")

        mock_subscription = MagicMock()
        mock_subscription.id = "sub_pm_test"
        mock_stripe.Subscription.create.return_value = mock_subscription

        stripe_id, was_created = client.upsert_subscription(
            source_id="sub_123",
            data={
                "customer_id": "cust_123",
                "price_id": "price_test123",
                "payment_method_id": "pm_test_visa",
            },
        )

        assert stripe_id == "sub_pm_test"
        assert was_created is True
        call_kwargs = mock_stripe.Subscription.create.call_args[1]
        assert call_kwargs.get("default_payment_method") == "pm_test_visa"
        assert call_kwargs.get("collection_method") == "charge_automatically"
        assert "days_until_due" not in call_kwargs

    def test_subscription_payment_method_id_priority_over_test_flag(
        self,
        test_config: TargetStripeConfig,
        mapping_store: MappingStore,
        mock_stripe: MagicMock,
    ) -> None:
        """payment_method_id in data takes priority: sets default_payment_method explicitly."""
        client = StripeClientWrapper(test_config, mapping_store)
        mapping_store.set_mapping(EntityType.CUSTOMER, "cust_123", "cus_stripe_123")

        mock_subscription = MagicMock()
        mock_subscription.id = "sub_pm_priority"
        mock_stripe.Subscription.create.return_value = mock_subscription

        stripe_id, _ = client.upsert_subscription(
            source_id="sub_123",
            data={
                "customer_id": "cust_123",
                "price_id": "price_test123",
                "payment_method_id": "pm_specific_card",
            },
        )

        call_kwargs = mock_stripe.Subscription.create.call_args[1]
        assert call_kwargs.get("default_payment_method") == "pm_specific_card"
        assert call_kwargs.get("collection_method") == "charge_automatically"

    def test_subscription_direct_stripe_customer_id_with_payment_method(
        self,
        parsed_config: TargetStripeConfig,
        mapping_store: MappingStore,
        mock_stripe: MagicMock,
    ) -> None:
        """Stripe customer ID (cus_) passed directly works with per-sub payment method."""
        client = StripeClientWrapper(parsed_config, mapping_store)

        mock_subscription = MagicMock()
        mock_subscription.id = "sub_direct_cus"
        mock_stripe.Subscription.create.return_value = mock_subscription

        stripe_id, was_created = client.upsert_subscription(
            source_id="sub_456",
            data={
                "customer_id": "cus_direct_stripe_id",
                "price_id": "price_test123",
                "payment_method_id": "pm_mastercard_test",
            },
        )

        assert stripe_id == "sub_direct_cus"
        call_kwargs = mock_stripe.Subscription.create.call_args[1]
        assert call_kwargs.get("customer") == "cus_direct_stripe_id"
        assert call_kwargs.get("default_payment_method") == "pm_mastercard_test"
