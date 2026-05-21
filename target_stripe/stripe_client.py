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
        import uuid

        self.config = config
        self.mapping_store = mapping_store
        self.rate_limiter = RateLimiter(config.rate_limit_per_sec)
        self.dry_run = config.dry_run

        # Generate unique run ID to avoid idempotency conflicts across runs
        self._run_id = str(uuid.uuid4())

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

    def find_existing_customer(
        self,
        source_id: str,
        email: str | None = None,
    ) -> stripe.Customer | None:
        """Find existing customer in Stripe.

        Strategy:
        1. If we have a Stripe ID in local DB, retrieve directly by ID
        2. Otherwise, search by email (natural identifier)
        3. If found, store mapping for future use

        Args:
            source_id: Source system customer ID.
            email: Customer email address (used for searching if no local mapping).

        Returns:
            Customer if found, None otherwise.
        """
        if self.dry_run:
            return None

        # Step 1: Check local DB first
        existing_stripe_id = self.mapping_store.get_stripe_id(EntityType.CUSTOMER, source_id)
        if existing_stripe_id:
            try:
                customer = self._execute_with_retry(
                    stripe.Customer.retrieve,
                    existing_stripe_id,
                )
                logger.debug(
                    "Found customer in local DB: source_id=%s, stripe_id=%s",
                    source_id,
                    existing_stripe_id,
                )
                return customer  # type: ignore[no-any-return]
            except stripe.InvalidRequestError as e:
                if "No such customer" in str(e):
                    # Customer was deleted from Stripe but still in local DB
                    logger.error(
                        "Customer %s mapped to %s not found in Stripe. "
                        "Possible environment mismatch or customer was deleted. "
                        "This indicates a data consistency issue.",
                        source_id,
                        existing_stripe_id,
                    )
                    raise StripePermanentError(
                        f"Customer {source_id} (stripe_id={existing_stripe_id}) not found in Stripe. "
                        f"Check environment configuration or investigate if customer was deleted.",
                        source_id=source_id,
                        stripe_error=e,
                    ) from e
                raise

        # Step 2: No local mapping - search by email if available
        if email and self.config.customers.check_existing:
            try:
                customers = self._execute_with_retry(
                    stripe.Customer.list,
                    email=email,
                    limit=1,
                )
                if customers.data:
                    customer = customers.data[0]
                    logger.info(
                        "Found existing customer by email: source_id=%s, email=%s, stripe_id=%s",
                        source_id,
                        email,
                        customer.id,
                    )
                    # Store mapping for future use
                    self.mapping_store.set_mapping(EntityType.CUSTOMER, source_id, customer.id)
                    return customer  # type: ignore[no-any-return]
            except stripe.InvalidRequestError:
                pass

        return None

    def upsert_customer(
        self,
        source_id: str,
        data: dict[str, Any],
    ) -> tuple[str, bool, bool]:
        """Create or update a Stripe customer.

        Args:
            source_id: Source system customer ID (e.g., chargify_customer_id).
            data: Customer data including email, name, phone, address, metadata.

        Returns:
            Tuple of (stripe_customer_id, was_created, was_updated).

        Raises:
            StripeError: If the operation fails.
        """
        csf = self.config.customers.source_fields
        metadata = dict(data.get("metadata", {}))
        metadata[csf.stripe_metadata_key_for_source_id()] = source_id
        for record_field, stripe_key in csf.metadata:
            if record_field == csf.customer_id:
                continue
            if record_field in data and data[record_field] is not None:
                metadata[stripe_key] = data[record_field]

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
            existing_stripe_id = self.mapping_store.get_stripe_id(EntityType.CUSTOMER, source_id)
            if existing_stripe_id:
                action = "update" if self.config.customers.update_existing else "skip"
                logger.info(
                    "[DRY RUN] Would %s customer: source_id=%s, stripe_id=%s, data=%s",
                    action,
                    source_id,
                    existing_stripe_id,
                    customer_data,
                )
                return (
                    existing_stripe_id,
                    False,
                    self.config.customers.update_existing,
                )
            logger.info(
                "[DRY RUN] Would create customer: source_id=%s, data=%s",
                source_id,
                customer_data,
            )
            return f"dry_run_cus_{source_id}", True, False

        # Try to find existing customer (checks local DB first, then optionally searches by email)
        existing_customer = self.find_existing_customer(source_id, data.get("email"))

        if existing_customer:
            self.mapping_store.set_mapping(
                EntityType.CUSTOMER, source_id, existing_customer.id
            )
            if not self.config.customers.update_existing:
                logger.debug(
                    "Skipped update for existing customer (link only): "
                    "source_id=%s, stripe_id=%s",
                    source_id,
                    existing_customer.id,
                )
                return existing_customer.id, False, False

            idempotency_key = generate_idempotency_key(
                EntityType.CUSTOMER,
                source_id,
                "update",
                strategy=self.config.idempotency.strategy.value,
                record_data=customer_data,
                run_id=self._run_id,
            )

            try:
                customer = self._execute_with_retry(
                    stripe.Customer.modify,
                    existing_customer.id,
                    idempotency_key=idempotency_key,
                    **customer_data,
                )
                self._stats["customers_updated"] += 1
                logger.debug(
                    "Updated customer: source_id=%s, stripe_id=%s",
                    source_id,
                    customer.id,
                )
                return customer.id, False, True
            except stripe.IdempotencyError:
                # Idempotency key was already used - likely a re-run within 24h
                # Return the existing mapping as success
                logger.warning(
                    "Idempotency conflict for customer %s (likely re-run within 24h). "
                    "Returning existing mapping: stripe_id=%s",
                    source_id,
                    existing_customer.id,
                )
                self._stats["customers_updated"] += 1
                return existing_customer.id, False, True
            except stripe.InvalidRequestError as e:
                self._stats["errors"] += 1
                raise StripePermanentError(
                    f"Failed to update customer {source_id}: {e}",
                    source_id=source_id,
                    stripe_error=e,
                ) from e

        # Create new customer
        idempotency_key = generate_idempotency_key(
            EntityType.CUSTOMER,
            source_id,
            "create",
            strategy=self.config.idempotency.strategy.value,
            record_data=customer_data,
            run_id=self._run_id,
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

            # Attach test payment method if configured
            if self.config.customers.add_test_payment_methods:
                try:
                    self.attach_test_payment_method(customer.id)
                except Exception as e:
                    logger.error(
                        "Failed to attach test payment method to customer %s: %s",
                        customer.id,
                        e,
                    )
                    # Don't fail the whole operation if payment method attachment fails
                    # The customer is still created successfully

            return customer.id, True, False
        except stripe.IdempotencyError as e:
            # Idempotency key was already used for a create operation
            # Try to find the customer that was created
            logger.warning(
                "Idempotency conflict on create for customer %s. "
                "Searching for previously created customer.",
                source_id,
            )
            existing_customer = self.find_existing_customer(source_id, data.get("email"))
            if existing_customer:
                self.mapping_store.set_mapping(
                    EntityType.CUSTOMER, source_id, existing_customer.id
                )
                if self.config.customers.update_existing:
                    self._stats["customers_created"] += 1
                    return existing_customer.id, True, False
                return existing_customer.id, False, False
            else:
                # Could not find the customer - re-raise the error
                self._stats["errors"] += 1
                raise StripePermanentError(
                    f"Idempotency conflict for customer {source_id} but could not find created customer",
                    source_id=source_id,
                    stripe_error=e,
                ) from e
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

        subs = self.config.subscriptions
        if plan_code and plan_code in subs.plan_code_to_price_id:
            return subs.plan_code_to_price_id[plan_code]

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

        ssf = self.config.subscriptions.source_fields
        metadata = dict(data.get("metadata", {}))
        metadata[ssf.stripe_metadata_key_for_source_id()] = source_id
        for record_field, stripe_key in ssf.metadata:
            if record_field == ssf.subscription_id:
                continue
            if record_field in data and data[record_field] is not None:
                metadata[stripe_key] = data[record_field]

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
                if self.config.subscriptions.past_due_handling == PastDueHandling.SKIP:
                    logger.warning(
                        "Skipping subscription %s: billing_cycle_anchor is in the past",
                        source_id,
                    )
                    self._stats["subscriptions_skipped"] += 1
                    return f"skipped_{source_id}", False
                elif self.config.subscriptions.past_due_handling == PastDueHandling.CREATE_FRESH:
                    logger.info(
                        "Starting fresh billing cycle for subscription %s (past-due date removed)",
                        source_id,
                    )
                    # Don't set billing_cycle_anchor or backdate_start_date
            else:
                # Billing anchor is in the future - preserve billing cycle
                subscription_data["billing_cycle_anchor"] = billing_anchor
                subscription_data["proration_behavior"] = ssf.proration_behavior
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
                run_id=self._run_id,
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
            except stripe.IdempotencyError:
                # Idempotency key was already used - likely a re-run within 24h
                logger.warning(
                    "Idempotency conflict for subscription %s (likely re-run within 24h). "
                    "Returning existing mapping: stripe_id=%s",
                    source_id,
                    existing_stripe_id,
                )
                self._stats["subscriptions_updated"] += 1
                return existing_stripe_id, False
            except stripe.InvalidRequestError as e:
                if "No such subscription" in str(e):
                    # Subscription was deleted from Stripe but still in local DB
                    logger.error(
                        "Subscription %s mapped to %s not found in Stripe. "
                        "Possible environment mismatch or subscription was deleted. "
                        "This indicates a data consistency issue.",
                        source_id,
                        existing_stripe_id,
                    )
                    raise StripePermanentError(
                        f"Subscription {source_id} (stripe_id={existing_stripe_id}) not found in Stripe. "
                        f"Check environment configuration or investigate if subscription was deleted.",
                        source_id=source_id,
                        stripe_error=e,
                    ) from e
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
            run_id=self._run_id,
        )

        subscription_data["customer"] = stripe_customer_id

        # Use send_invoice collection to avoid requiring payment methods
        # This creates active subscriptions that will be invoiced
        # However, if test payment methods are enabled, use charge_automatically
        if self.config.customers.add_test_payment_methods:
            # In test mode with payment methods, use charge_automatically
            subscription_data["collection_method"] = "charge_automatically"
        elif "default_payment_method" not in subscription_data:
            subscription_data["collection_method"] = "send_invoice"
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
        except stripe.IdempotencyError as e:
            # Idempotency key was already used for a create operation
            # The subscription was likely already created, return existing mapping
            logger.warning(
                "Idempotency conflict on create for subscription %s. "
                "Returning existing mapping if found.",
                source_id,
            )
            existing_stripe_id = self.mapping_store.get_stripe_id(
                EntityType.SUBSCRIPTION, source_id
            )
            if existing_stripe_id:
                self._stats["subscriptions_created"] += 1
                return existing_stripe_id, True
            else:
                # Could not find the subscription - re-raise the error
                self._stats["errors"] += 1
                raise StripePermanentError(
                    f"Idempotency conflict for subscription {source_id} but could not find created subscription",
                    source_id=source_id,
                    stripe_error=e,
                ) from e
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

    def attach_test_payment_method(
        self,
        customer_id: str,
        test_pm_token: str = "pm_card_visa",
    ) -> str:
        """Attach a test payment method to a customer.

        This method should only be used in test mode with the add_test_payment_methods flag enabled.

        Args:
            customer_id: Stripe customer ID.
            test_pm_token: Test payment method token (defaults to pm_card_visa).
                          See https://stripe.com/docs/testing#cards for other test tokens.

        Returns:
            Payment method ID.

        Raises:
            ValueError: If not in test mode or test payment methods not enabled.
            StripeError: If the operation fails.
        """
        from target_stripe.config import StripeMode

        if self.config.stripe_mode != StripeMode.TEST:
            raise ValueError("Test payment methods can only be attached in test mode")

        if not self.config.customers.add_test_payment_methods:
            raise ValueError(
                "add_test_payment_methods must be enabled to attach test payment methods"
            )

        if self.dry_run:
            logger.info(
                "[DRY RUN] Would attach test payment method to customer: %s",
                customer_id,
            )
            return f"dry_run_pm_{customer_id}"

        try:
            # Attach the test payment method token to the customer
            payment_method = self._execute_with_retry(
                stripe.PaymentMethod.attach,
                test_pm_token,
                customer=customer_id,
            )

            # Set as default payment method
            self._execute_with_retry(
                stripe.Customer.modify,
                customer_id,
                invoice_settings={"default_payment_method": payment_method.id},
            )

            logger.info(
                "Attached test payment method %s to customer %s",
                payment_method.id,
                customer_id,
            )

            return payment_method.id  # type: ignore[no-any-return]

        except stripe.InvalidRequestError as e:
            raise StripePermanentError(
                f"Failed to attach test payment method to customer {customer_id}: {e}",
                source_id=customer_id,
                stripe_error=e,
            ) from e
