"""Stripe client wrapper with retry logic, rate limiting, and error handling."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, TypeVar

import stripe
from tenacity import (
    RetryError,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from target_stripe.mapping import EntityType, MappingStore, generate_idempotency_key

if TYPE_CHECKING:
    from target_stripe.config import TargetStripeConfig

logger = logging.getLogger(__name__)

T = TypeVar("T")


class StripeError(Exception):
    """Base exception for Stripe-related errors."""

    def __init__(
        self,
        message: str,
        source_id: str | None = None,
        stripe_error: stripe.StripeError | None = None,
    ) -> None:
        super().__init__(message)
        self.source_id = source_id
        self.stripe_error = stripe_error


class StripeTransientError(StripeError):
    """Transient error that can be retried."""

    pass


class StripePermanentError(StripeError):
    """Permanent error that should not be retried."""

    pass


def is_transient_error(exception: BaseException) -> bool:
    """Check if an exception is transient and should be retried."""
    if isinstance(exception, StripeTransientError):
        return True
    if isinstance(exception, stripe.RateLimitError):
        return True
    if isinstance(exception, stripe.APIConnectionError):
        return True
    if isinstance(exception, stripe.APIError):
        return exception.http_status in (500, 502, 503, 504)
    return False


class RateLimiter:
    """Simple token bucket rate limiter."""

    def __init__(self, rate_per_second: float) -> None:
        """Initialize the rate limiter.

        Args:
            rate_per_second: Maximum number of operations per second.
        """
        self.rate_per_second = rate_per_second
        self.min_interval = 1.0 / rate_per_second
        self._last_call_time: float = 0.0

    def acquire(self) -> None:
        """Acquire permission to make a request, blocking if necessary."""
        current_time = time.monotonic()
        elapsed = current_time - self._last_call_time

        if elapsed < self.min_interval:
            sleep_time = self.min_interval - elapsed
            time.sleep(sleep_time)

        self._last_call_time = time.monotonic()


class StripeClientWrapper:
    """Wrapper around the Stripe SDK with retry logic and rate limiting."""

    def __init__(
        self,
        config: TargetStripeConfig,
        mapping_store: MappingStore,
    ) -> None:
        """Initialize the Stripe client wrapper.

        Args:
            config: Target configuration.
            mapping_store: ID mapping store.
        """
        self.config = config
        self.mapping_store = mapping_store
        self.rate_limiter = RateLimiter(config.rate_limit_per_sec)
        self.dry_run = config.dry_run

        stripe.api_key = config.stripe_api_key
        stripe.max_network_retries = 0  # We handle retries ourselves

        self._stats = {
            "customers_created": 0,
            "customers_updated": 0,
            "subscriptions_created": 0,
            "subscriptions_updated": 0,
            "subscriptions_skipped": 0,
            "errors": 0,
            "retries": 0,
        }

    @property
    def stats(self) -> dict[str, int]:
        """Get operation statistics."""
        return self._stats.copy()

    def _make_retry_decorator(self) -> Any:
        """Create a retry decorator with configured parameters."""
        return retry(
            retry=retry_if_exception(is_transient_error),
            stop=stop_after_attempt(self.config.max_retries + 1),
            wait=wait_exponential(
                multiplier=self.config.retry_backoff_base,
                min=1,
                max=60,
            ),
            before_sleep=self._log_retry,
            reraise=True,
        )

    def _log_retry(self, retry_state: Any) -> None:
        """Log retry attempts."""
        self._stats["retries"] += 1
        logger.warning(
            "Retrying Stripe operation (attempt %d/%d): %s",
            retry_state.attempt_number,
            self.config.max_retries + 1,
            str(retry_state.outcome.exception()) if retry_state.outcome else "unknown",
        )

    def _execute_with_retry(self, operation: Any, *args: Any, **kwargs: Any) -> Any:
        """Execute a Stripe operation with retry logic."""
        decorator = self._make_retry_decorator()

        @decorator
        def _inner() -> Any:
            self.rate_limiter.acquire()
            try:
                return operation(*args, **kwargs)
            except stripe.RateLimitError as e:
                raise StripeTransientError(
                    f"Rate limit exceeded: {e}",
                    stripe_error=e,
                ) from e
            except stripe.APIConnectionError as e:
                raise StripeTransientError(
                    f"API connection error: {e}",
                    stripe_error=e,
                ) from e
            except stripe.APIError as e:
                if e.http_status in (500, 502, 503, 504):
                    raise StripeTransientError(
                        f"Stripe server error: {e}",
                        stripe_error=e,
                    ) from e
                raise StripePermanentError(
                    f"Stripe API error: {e}",
                    stripe_error=e,
                ) from e

        try:
            return _inner()
        except RetryError as e:
            self._stats["errors"] += 1
            raise StripeError(f"Max retries exceeded: {e.last_attempt.exception()}") from e

    def find_customer_by_metadata(
        self,
        metadata_key: str,
        metadata_value: str,
    ) -> stripe.Customer | None:
        """Find a customer by metadata value.

        Args:
            metadata_key: Metadata key to search by.
            metadata_value: Metadata value to match.

        Returns:
            Customer if found, None otherwise.
        """
        if self.dry_run:
            return None

        try:
            customers = self._execute_with_retry(
                stripe.Customer.search,
                query=f"metadata['{metadata_key}']:'{metadata_value}'",
                limit=1,
            )
            if customers.data:
                return customers.data[0]  # type: ignore[no-any-return]
            return None
        except stripe.InvalidRequestError:
            return None

    def upsert_customer(
        self,
        source_id: str,
        data: dict[str, Any],
    ) -> tuple[str, bool]:
        """Create or update a Stripe customer.

        Args:
            source_id: Source system customer ID (e.g., chargify_customer_id).
            data: Customer data including email, name, phone, address, metadata.

        Returns:
            Tuple of (stripe_customer_id, was_created).

        Raises:
            StripeError: If the operation fails.
        """
        existing_stripe_id = self.mapping_store.get_stripe_id(EntityType.CUSTOMER, source_id)

        metadata = data.get("metadata", {})
        metadata[self.config.source_fields.get_customer_metadata_key()] = source_id
        # Copy additional metadata fields from source data
        for field in self.config.source_fields.additional_metadata_fields:
            if field in data:
                metadata[field] = data[field]

        customer_data = {
            "email": data.get("email"),
            "name": data.get("name"),
            "phone": data.get("phone"),
            "metadata": metadata,
        }

        if data.get("address"):
            customer_data["address"] = data["address"]

        customer_data = {k: v for k, v in customer_data.items() if v is not None}

        if self.dry_run:
            logger.info(
                "[DRY RUN] Would %s customer: source_id=%s, data=%s",
                "update" if existing_stripe_id else "create",
                source_id,
                customer_data,
            )
            return existing_stripe_id or f"dry_run_cus_{source_id}", not existing_stripe_id

        if existing_stripe_id:
            idempotency_key = generate_idempotency_key(
                EntityType.CUSTOMER,
                source_id,
                "update",
                strategy=self.config.idempotency.strategy.value,
                record_data=customer_data,
            )

            try:
                customer = self._execute_with_retry(
                    stripe.Customer.modify,
                    existing_stripe_id,
                    idempotency_key=idempotency_key,
                    **customer_data,
                )
                self._stats["customers_updated"] += 1
                logger.debug(
                    "Updated customer: source_id=%s, stripe_id=%s",
                    source_id,
                    customer.id,
                )
                return customer.id, False
            except stripe.InvalidRequestError as e:
                if "No such customer" in str(e):
                    existing_stripe_id = None
                else:
                    self._stats["errors"] += 1
                    raise StripePermanentError(
                        f"Failed to update customer {source_id}: {e}",
                        source_id=source_id,
                        stripe_error=e,
                    ) from e

        if not existing_stripe_id:
            # Skip metadata search if configured (useful for initial migrations)
            if not self.config.skip_existence_check:
                existing_customer = self.find_customer_by_metadata(
                    self.config.source_fields.get_customer_metadata_key(), source_id
                )
                if existing_customer:
                    self.mapping_store.set_mapping(
                        EntityType.CUSTOMER, source_id, existing_customer.id
                    )
                    customer = self._execute_with_retry(
                        stripe.Customer.modify,
                        existing_customer.id,
                        **customer_data,
                    )
                    self._stats["customers_updated"] += 1
                    return customer.id, False

        idempotency_key = generate_idempotency_key(
            EntityType.CUSTOMER,
            source_id,
            "create",
            strategy=self.config.idempotency.strategy.value,
            record_data=customer_data,
        )

        try:
            customer = self._execute_with_retry(
                stripe.Customer.create,
                idempotency_key=idempotency_key,
                **customer_data,
            )
            self.mapping_store.set_mapping(EntityType.CUSTOMER, source_id, customer.id)
            self._stats["customers_created"] += 1
            logger.debug(
                "Created customer: source_id=%s, stripe_id=%s",
                source_id,
                customer.id,
            )
            return customer.id, True
        except stripe.InvalidRequestError as e:
            self._stats["errors"] += 1
            raise StripePermanentError(
                f"Failed to create customer {source_id}: {e}",
                source_id=source_id,
                stripe_error=e,
            ) from e

    def resolve_customer_id(self, customer_ref: str) -> str | None:
        """Resolve a customer reference to a Stripe customer ID.

        Args:
            customer_ref: Either a Stripe customer ID (cus_xxx) or source ID.

        Returns:
            Stripe customer ID if found, None otherwise.
        """
        if customer_ref.startswith("cus_"):
            return customer_ref

        return self.mapping_store.get_stripe_id(EntityType.CUSTOMER, customer_ref)

    def resolve_price_id(self, plan_code: str | None, price_id: str | None) -> str:
        """Resolve a plan code or price ID to a Stripe price ID.

        Args:
            plan_code: Plan code to look up.
            price_id: Direct price ID (takes precedence).

        Returns:
            Stripe price ID.

        Raises:
            ValueError: If neither can be resolved.
        """
        if price_id:
            return price_id

        if plan_code and plan_code in self.config.plan_code_to_price_id:
            return self.config.plan_code_to_price_id[plan_code]

        if plan_code and plan_code.startswith("price_"):
            return plan_code

        raise ValueError(
            f"Cannot resolve price: plan_code={plan_code}, price_id={price_id}. "
            "Provide a valid price_id or configure plan_code_to_price_id mapping."
        )

    def upsert_subscription(
        self,
        source_id: str,
        data: dict[str, Any],
    ) -> tuple[str, bool]:
        """Create or update a Stripe subscription.

        Args:
            source_id: Source system subscription ID (e.g., chargify_subscription_id).
            data: Subscription data including customer, price_id, quantity, etc.

        Returns:
            Tuple of (stripe_subscription_id, was_created).

        Raises:
            StripeError: If the operation fails.
            ValueError: If required data is missing or invalid.
        """
        customer_ref = data.get("customer_id") or data.get("customer")
        if not customer_ref:
            raise ValueError(f"Subscription {source_id} missing customer reference")

        stripe_customer_id = self.resolve_customer_id(customer_ref)
        if not stripe_customer_id:
            raise ValueError(
                f"Customer not found for subscription {source_id}: "
                f"customer_ref={customer_ref}. Ensure customer is created first."
            )

        price_id = self.resolve_price_id(
            data.get("plan_code"),
            data.get("price_id"),
        )

        metadata = data.get("metadata", {})
        metadata[self.config.source_fields.get_subscription_metadata_key()] = source_id

        subscription_data: dict[str, Any] = {
            "metadata": metadata,
        }

        if data.get("quantity"):
            subscription_data["items"] = [
                {
                    "price": price_id,
                    "quantity": data["quantity"],
                }
            ]
        else:
            subscription_data["items"] = [{"price": price_id}]

        if data.get("trial_end"):
            trial_end = data["trial_end"]
            if isinstance(trial_end, str):
                if trial_end == "now":
                    subscription_data["trial_end"] = "now"
                else:
                    from datetime import datetime

                    dt = datetime.fromisoformat(trial_end.replace("Z", "+00:00"))
                    subscription_data["trial_end"] = int(dt.timestamp())
            else:
                subscription_data["trial_end"] = trial_end

        if data.get("coupon"):
            subscription_data["coupon"] = data["coupon"]

        if data.get("cancel_at_period_end") is not None:
            subscription_data["cancel_at_period_end"] = data["cancel_at_period_end"]

        # Handle billing cycle preservation
        if data.get("billing_cycle_anchor"):
            import time

            from target_stripe.config import PastDueHandling

            current_time = int(time.time())
            billing_anchor = data["billing_cycle_anchor"]

            if billing_anchor < current_time:
                # Billing date is in the past
                if self.config.past_due_handling == PastDueHandling.SKIP:
                    logger.warning(
                        "Skipping subscription %s: billing_cycle_anchor is in the past",
                        source_id,
                    )
                    self._stats["subscriptions_skipped"] += 1
                    return f"skipped_{source_id}", False
                elif self.config.past_due_handling == PastDueHandling.CREATE_FRESH:
                    logger.info(
                        "Starting fresh billing cycle for subscription %s (past-due date removed)",
                        source_id,
                    )
                    # Don't set billing_cycle_anchor or backdate_start_date
            else:
                # Billing anchor is in the future - preserve billing cycle
                subscription_data["billing_cycle_anchor"] = billing_anchor
                subscription_data["proration_behavior"] = (
                    self.config.source_fields.proration_behavior
                )
                if data.get("backdate_start_date"):
                    subscription_data["backdate_start_date"] = data["backdate_start_date"]

        existing_stripe_id = self.mapping_store.get_stripe_id(EntityType.SUBSCRIPTION, source_id)

        if self.dry_run:
            logger.info(
                "[DRY RUN] Would %s subscription: source_id=%s, customer=%s, data=%s",
                "update" if existing_stripe_id else "create",
                source_id,
                stripe_customer_id,
                subscription_data,
            )
            return existing_stripe_id or f"dry_run_sub_{source_id}", not existing_stripe_id

        if existing_stripe_id:
            idempotency_key = generate_idempotency_key(
                EntityType.SUBSCRIPTION,
                source_id,
                "update",
                strategy=self.config.idempotency.strategy.value,
                record_data=subscription_data,
            )

            update_data = {
                "metadata": subscription_data["metadata"],
            }
            if "cancel_at_period_end" in subscription_data:
                update_data["cancel_at_period_end"] = subscription_data["cancel_at_period_end"]
            if "coupon" in subscription_data:
                update_data["coupon"] = subscription_data["coupon"]

            try:
                subscription = self._execute_with_retry(
                    stripe.Subscription.modify,
                    existing_stripe_id,
                    idempotency_key=idempotency_key,
                    **update_data,
                )
                self._stats["subscriptions_updated"] += 1
                logger.debug(
                    "Updated subscription: source_id=%s, stripe_id=%s",
                    source_id,
                    subscription.id,
                )
                return subscription.id, False
            except stripe.InvalidRequestError as e:
                if "No such subscription" in str(e):
                    existing_stripe_id = None
                else:
                    self._stats["errors"] += 1
                    raise StripePermanentError(
                        f"Failed to update subscription {source_id}: {e}",
                        source_id=source_id,
                        stripe_error=e,
                    ) from e

        idempotency_key = generate_idempotency_key(
            EntityType.SUBSCRIPTION,
            source_id,
            "create",
            strategy=self.config.idempotency.strategy.value,
            record_data=subscription_data,
        )

        subscription_data["customer"] = stripe_customer_id

        # Use send_invoice collection to avoid requiring payment methods
        # This creates active subscriptions that will be invoiced
        if "default_payment_method" not in subscription_data:
            subscription_data["collection_method"] = "send_invoice"
            # Only set days_until_due if we're NOT preserving billing cycles
            # When billing_cycle_anchor is set, the cycle determines invoice timing
            if "billing_cycle_anchor" not in subscription_data:
                subscription_data["days_until_due"] = 30  # Invoice due in 30 days

        try:
            subscription = self._execute_with_retry(
                stripe.Subscription.create,
                idempotency_key=idempotency_key,
                **subscription_data,
            )
            self.mapping_store.set_mapping(EntityType.SUBSCRIPTION, source_id, subscription.id)
            self._stats["subscriptions_created"] += 1
            logger.debug(
                "Created subscription: source_id=%s, stripe_id=%s",
                source_id,
                subscription.id,
            )
            return subscription.id, True
        except stripe.InvalidRequestError as e:
            self._stats["errors"] += 1
            raise StripePermanentError(
                f"Failed to create subscription {source_id}: {e}",
                source_id=source_id,
                stripe_error=e,
            ) from e

    def get_customer(self, stripe_id: str) -> stripe.Customer | None:
        """Retrieve a customer by Stripe ID.

        Args:
            stripe_id: Stripe customer ID.

        Returns:
            Customer object if found, None otherwise.
        """
        if self.dry_run:
            return None

        try:
            return self._execute_with_retry(stripe.Customer.retrieve, stripe_id)  # type: ignore[no-any-return]
        except stripe.InvalidRequestError:
            return None

    def get_subscription(self, stripe_id: str) -> stripe.Subscription | None:
        """Retrieve a subscription by Stripe ID.

        Args:
            stripe_id: Stripe subscription ID.

        Returns:
            Subscription object if found, None otherwise.
        """
        if self.dry_run:
            return None

        try:
            return self._execute_with_retry(stripe.Subscription.retrieve, stripe_id)  # type: ignore[no-any-return]
        except stripe.InvalidRequestError:
            return None
