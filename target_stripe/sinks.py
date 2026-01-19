"""Singer SDK Sinks for Stripe entities."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from singer_sdk.sinks import BatchSink

from target_stripe.config import SourceFieldsConfig
from target_stripe.stripe_client import (
    StripeClientWrapper,
    StripeError,
    StripePermanentError,
)

if TYPE_CHECKING:
    from singer_sdk import Target

logger = logging.getLogger(__name__)


class StripeBaseSink(BatchSink):
    """Base sink class for Stripe entities."""

    max_size = 50  # Default batch size

    def __init__(
        self,
        target: Target,
        stream_name: str,
        schema: dict[str, Any],
        key_properties: list[str] | None,
    ) -> None:
        """Initialize the sink.

        Args:
            target: The target instance.
            stream_name: Name of the stream.
            schema: JSON Schema for the stream.
            key_properties: Key properties for the stream.
        """
        super().__init__(target, stream_name, schema, key_properties)
        self._records_processed = 0
        self._errors: list[dict[str, Any]] = []
        # Cache config values during init (needed for joblib serialization)
        self._stripe_client: StripeClientWrapper = target._stripe_client  # type: ignore
        self._source_fields = target._parsed_config.source_fields  # type: ignore
        self._hard_fail = target._parsed_config.hard_fail  # type: ignore
        self._dry_run = target._parsed_config.dry_run  # type: ignore

    @property
    def stripe_client(self) -> StripeClientWrapper:
        """Get the cached Stripe client."""
        return self._stripe_client

    @property
    def hard_fail(self) -> bool:
        """Get cached hard_fail setting."""
        return self._hard_fail

    @property
    def dry_run(self) -> bool:
        """Get cached dry_run setting."""
        return self._dry_run

    @property
    def source_fields(self) -> SourceFieldsConfig:
        """Get cached source_fields config."""
        return self._source_fields

    def _handle_error(
        self,
        record: dict[str, Any],
        error: Exception,
    ) -> None:
        """Handle a record processing error.

        Args:
            record: The record that failed.
            error: The exception that occurred.

        Raises:
            Exception: Re-raises if hard_fail is True.
        """
        error_info = {
            "stream": self.stream_name,
            "record": record,
            "error": str(error),
            "error_type": type(error).__name__,
        }
        self._errors.append(error_info)

        if isinstance(error, StripePermanentError):
            logger.error(
                "Permanent error processing record in %s: %s (source_id=%s)",
                self.stream_name,
                error,
                error.source_id,
            )
        else:
            logger.error(
                "Error processing record in %s: %s",
                self.stream_name,
                error,
            )

        if self.hard_fail:
            raise error

    def start_batch(self, context: dict) -> None:
        """Start a new batch.

        Args:
            context: Batch context.
        """
        context["processed"] = 0
        context["created"] = 0
        context["updated"] = 0
        context["errors"] = 0

    def process_batch(self, context: dict) -> None:
        """Process a batch of records.

        This method should be overridden by subclasses.

        Args:
            context: Batch context containing records.
        """
        raise NotImplementedError("Subclasses must implement process_batch")


class CustomerSink(StripeBaseSink):
    """Sink for upserting Stripe Customers."""

    name = "customers"

    def process_batch(self, context: dict) -> None:
        """Process a batch of customer records.

        Args:
            context: Batch context containing records.
        """
        records = context.get("records", [])
        logger.info("Processing batch of %d customer records", len(records))

        for record in records:
            try:
                source_id = self._extract_source_id(record)
                customer_data = self._transform_record(record)

                stripe_id, was_created = self.stripe_client.upsert_customer(
                    source_id=source_id,
                    data=customer_data,
                )

                context["processed"] += 1
                if was_created:
                    context["created"] += 1
                else:
                    context["updated"] += 1

                self._records_processed += 1

            except (StripeError, ValueError) as e:
                context["errors"] += 1
                self._handle_error(record, e)

        logger.info(
            "Batch complete: %d processed, %d created, %d updated, %d errors",
            context["processed"],
            context["created"],
            context["updated"],
            context["errors"],
        )

    def _extract_source_id(self, record: dict[str, Any]) -> str:
        """Extract the source ID from a record.

        The source ID is used as the primary key for deduplication.
        Supports multiple field names for compatibility with different taps.
        The configured customer_source_id_field takes priority.

        Args:
            record: The input record.

        Returns:
            Source ID string.

        Raises:
            ValueError: If no source ID can be found.
        """
        # Configured field takes priority
        configured_field = self.source_fields.customer_source_id_field
        if configured_field in record and record[configured_field]:
            return str(record[configured_field])

        # Fall back to common field names for compatibility
        fallback_fields = [
            "customer_id",
            "id",
            "source_id",
            "external_id",
        ]

        for field in fallback_fields:
            if field in record and record[field]:
                return str(record[field])

        # Check metadata using configured key
        metadata_key = self.source_fields.customer_metadata_key
        if "metadata" in record and isinstance(record["metadata"], dict):
            if metadata_key in record["metadata"]:
                return str(record["metadata"][metadata_key])

        raise ValueError(
            f"Cannot extract source ID from customer record. "
            f"Looked for fields: [{configured_field}] + {fallback_fields}"
        )

    def _transform_record(self, record: dict[str, Any]) -> dict[str, Any]:
        """Transform a Singer record to Stripe customer data.

        Args:
            record: The input record.

        Returns:
            Transformed data suitable for Stripe API.
        """
        customer_data: dict[str, Any] = {}

        field_mapping = {
            "email": "email",
            "name": "name",
            "full_name": "name",
            "phone": "phone",
            "phone_number": "phone",
            "description": "description",
        }

        for source_field, target_field in field_mapping.items():
            if source_field in record and record[source_field]:
                customer_data[target_field] = record[source_field]

        if "first_name" in record or "last_name" in record:
            if "name" not in customer_data:
                first = record.get("first_name", "")
                last = record.get("last_name", "")
                full_name = f"{first} {last}".strip()
                if full_name:
                    customer_data["name"] = full_name

        address = self._extract_address(record)
        if address:
            customer_data["address"] = address

        metadata = record.get("metadata", {})
        if isinstance(metadata, dict):
            customer_data["metadata"] = metadata.copy()
        else:
            customer_data["metadata"] = {}

        # Copy additional metadata fields from source record
        for field in self.source_fields.additional_metadata_fields:
            if field in record:
                customer_data[field] = record[field]

        return customer_data

    def _extract_address(self, record: dict[str, Any]) -> dict[str, str] | None:
        """Extract address fields from a record.

        Args:
            record: The input record.

        Returns:
            Address dictionary or None if no address fields found.
        """
        if "address" in record and isinstance(record["address"], dict):
            return record["address"]

        address: dict[str, str] = {}
        address_mapping = {
            "address_line1": "line1",
            "address_line_1": "line1",
            "street": "line1",
            "address_line2": "line2",
            "address_line_2": "line2",
            "city": "city",
            "state": "state",
            "province": "state",
            "postal_code": "postal_code",
            "zip": "postal_code",
            "zip_code": "postal_code",
            "country": "country",
            "country_code": "country",
        }

        for source_field, target_field in address_mapping.items():
            if source_field in record and record[source_field]:
                address[target_field] = str(record[source_field])

        return address if address else None


class SubscriptionSink(StripeBaseSink):
    """Sink for upserting Stripe Subscriptions."""

    name = "subscriptions"

    def process_batch(self, context: dict) -> None:
        """Process a batch of subscription records.

        Args:
            context: Batch context containing records.
        """
        records = context.get("records", [])
        logger.info("Processing batch of %d subscription records", len(records))

        for record in records:
            try:
                source_id = self._extract_source_id(record)
                subscription_data = self._transform_record(record)

                stripe_id, was_created = self.stripe_client.upsert_subscription(
                    source_id=source_id,
                    data=subscription_data,
                )

                context["processed"] += 1
                if was_created:
                    context["created"] += 1
                else:
                    context["updated"] += 1

                self._records_processed += 1

            except (StripeError, ValueError) as e:
                context["errors"] += 1
                self._handle_error(record, e)

        logger.info(
            "Batch complete: %d processed, %d created, %d updated, %d errors",
            context["processed"],
            context["created"],
            context["updated"],
            context["errors"],
        )

    def _extract_source_id(self, record: dict[str, Any]) -> str:
        """Extract the source ID from a subscription record.

        The configured subscription_source_id_field takes priority.

        Args:
            record: The input record.

        Returns:
            Source ID string.

        Raises:
            ValueError: If no source ID can be found.
        """
        # Configured field takes priority
        configured_field = self.source_fields.subscription_source_id_field
        if configured_field in record and record[configured_field]:
            return str(record[configured_field])

        # Fall back to common field names for compatibility
        fallback_fields = [
            "subscription_id",
            "id",
            "source_id",
            "external_id",
        ]

        for field in fallback_fields:
            if field in record and record[field]:
                return str(record[field])

        # Check metadata using configured key
        metadata_key = self.source_fields.subscription_metadata_key
        if "metadata" in record and isinstance(record["metadata"], dict):
            if metadata_key in record["metadata"]:
                return str(record["metadata"][metadata_key])

        raise ValueError(
            f"Cannot extract source ID from subscription record. "
            f"Looked for fields: [{configured_field}] + {fallback_fields}"
        )

    def _transform_record(self, record: dict[str, Any]) -> dict[str, Any]:
        """Transform a Singer record to Stripe subscription data.

        Args:
            record: The input record.

        Returns:
            Transformed data suitable for Stripe API.
        """
        subscription_data: dict[str, Any] = {}

        customer_fields = [
            "customer_id",
            "chargify_customer_id",
            "stripe_customer_id",
            "customer",
        ]
        for field in customer_fields:
            if field in record and record[field]:
                subscription_data["customer_id"] = str(record[field])
                break

        if "price_id" in record and record["price_id"]:
            subscription_data["price_id"] = record["price_id"]
        elif "plan_code" in record and record["plan_code"]:
            subscription_data["plan_code"] = record["plan_code"]
        elif "plan_id" in record and record["plan_id"]:
            subscription_data["plan_code"] = record["plan_id"]
        elif "product_handle" in record and record["product_handle"]:
            subscription_data["plan_code"] = record["product_handle"]

        if "quantity" in record and record["quantity"]:
            subscription_data["quantity"] = int(record["quantity"])

        if "trial_end" in record and record["trial_end"]:
            subscription_data["trial_end"] = record["trial_end"]
        elif "trial_ended_at" in record and record["trial_ended_at"]:
            subscription_data["trial_end"] = record["trial_ended_at"]

        if "coupon" in record and record["coupon"]:
            subscription_data["coupon"] = record["coupon"]
        elif "coupon_code" in record and record["coupon_code"]:
            subscription_data["coupon"] = record["coupon_code"]

        if "cancel_at_period_end" in record:
            subscription_data["cancel_at_period_end"] = bool(record["cancel_at_period_end"])

        metadata = record.get("metadata", {})
        if isinstance(metadata, dict):
            subscription_data["metadata"] = metadata.copy()
        else:
            subscription_data["metadata"] = {}

        return subscription_data


SINK_TYPES = {
    "customers": CustomerSink,
    "customer": CustomerSink,
    "stripe_customers": CustomerSink,
    "subscriptions": SubscriptionSink,
    "subscription": SubscriptionSink,
    "stripe_subscriptions": SubscriptionSink,
}


def get_sink_class(stream_name: str) -> type[StripeBaseSink] | None:
    """Get the appropriate sink class for a stream name.

    Args:
        stream_name: Name of the stream.

    Returns:
        Sink class or None if not supported.
    """
    normalized = stream_name.lower().replace("-", "_")

    if normalized in SINK_TYPES:
        return SINK_TYPES[normalized]

    for key, sink_class in SINK_TYPES.items():
        if key in normalized:
            return sink_class

    return None
