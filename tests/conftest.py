"""Pytest fixtures and configuration."""

from __future__ import annotations

import tempfile
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from target_stripe.config import TargetStripeConfig, parse_config
from target_stripe.mapping import MappingStore


@pytest.fixture
def temp_db_path() -> Generator[Path, None, None]:
    """Create a temporary database path."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        yield Path(f.name)


@pytest.fixture
def mapping_store(temp_db_path: Path) -> Generator[MappingStore, None, None]:
    """Create a test mapping store."""
    store = MappingStore(temp_db_path)
    yield store
    store.close()


@pytest.fixture
def base_config() -> dict[str, Any]:
    """Base configuration for tests."""
    return {
        "stripe_api_key": "sk_test_1234567890abcdefghijklmnop",
        "stripe_mode": "test",
        "dry_run": False,
        "hard_fail": False,
        "state_emit_interval": 100,
        "rate_limit_per_sec": 25.0,
        "batch_size": 50,
        "max_retries": 3,
        "retry_backoff_base": 2.0,
    }


@pytest.fixture
def parsed_config(base_config: dict[str, Any], temp_db_path: Path) -> TargetStripeConfig:
    """Create a parsed configuration."""
    base_config["mapping_db_path"] = str(temp_db_path)
    return parse_config(base_config)


@pytest.fixture
def mock_stripe_customer() -> MagicMock:
    """Create a mock Stripe customer."""
    customer = MagicMock()
    customer.id = "cus_test123"
    customer.email = "test@example.com"
    customer.name = "Test User"
    customer.metadata = {"source_customer_id": "12345"}
    return customer


@pytest.fixture
def mock_stripe_subscription() -> MagicMock:
    """Create a mock Stripe subscription."""
    subscription = MagicMock()
    subscription.id = "sub_test123"
    subscription.customer = "cus_test123"
    subscription.status = "active"
    subscription.metadata = {"source_subscription_id": "67890"}
    return subscription


@pytest.fixture
def mock_stripe() -> Generator[MagicMock, None, None]:
    """Mock the Stripe module."""
    with patch("target_stripe.stripe_client.stripe") as mock:
        mock.RateLimitError = Exception
        mock.APIConnectionError = Exception
        mock.APIError = Exception
        mock.InvalidRequestError = Exception

        mock.Customer = MagicMock()
        mock.Subscription = MagicMock()

        yield mock


@pytest.fixture
def sample_customer_record() -> dict[str, Any]:
    """Sample customer record."""
    return {
        "source_customer_id": "12345",
        "email": "customer@example.com",
        "first_name": "John",
        "last_name": "Doe",
        "phone": "+1234567890",
        "address_line1": "123 Main St",
        "city": "San Francisco",
        "state": "CA",
        "postal_code": "94105",
        "country": "US",
        "metadata": {
            "source": "test",
        },
    }


@pytest.fixture
def sample_subscription_record() -> dict[str, Any]:
    """Sample subscription record."""
    return {
        "source_subscription_id": "67890",
        "customer_id": "12345",
        "price_id": "price_test123",
        "quantity": 1,
        "trial_end": None,
        "coupon": None,
        "cancel_at_period_end": False,
        "metadata": {
            "source": "test",
        },
    }
