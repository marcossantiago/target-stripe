"""Singer Target for Stripe - upserts Customers and Subscriptions."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import PurePath
from typing import Any, Dict

from singer_sdk import Target
from singer_sdk import typing as th

from target_stripe.config import parse_config
from target_stripe.mapping import MappingStore
from target_stripe.sinks import CustomerSink, SubscriptionSink, get_sink_class
from target_stripe.stripe_client import StripeClientWrapper

logger = logging.getLogger(__name__)


class TargetStripe(Target):
    """Singer Target for Stripe API.

    Supports upserting Customers and Subscriptions with:
    - Idempotent operations using Stripe idempotency keys
    - Source ID → Stripe ID mapping persistence
    - Retry logic with exponential backoff
    - Rate limiting to stay within Stripe API limits
    - Dry run mode for validation without writes
    """

    name = "target-stripe"

    config_jsonschema = th.PropertiesList(
        th.Property(
            "stripe_api_key",
            th.StringType,
            required=True,
            secret=True,
            description="Stripe API key (starts with sk_test_ or sk_live_)",
        ),
        th.Property(
            "stripe_mode",
            th.StringType,
            default="test",
            allowed_values=["test", "live"],
            description="Stripe mode: test or live",
        ),
        th.Property(
            "default_currency",
            th.StringType,
            default="usd",
            description="Default currency for subscriptions (ISO 4217 code)",
        ),
        th.Property(
            "dry_run",
            th.BooleanType,
            default=False,
            description="If true, validate data but do not write to Stripe",
        ),
        th.Property(
            "hard_fail",
            th.BooleanType,
            default=False,
            description="If true, fail immediately on any record error",
        ),
        th.Property(
            "idempotency",
            th.ObjectType(
                th.Property(
                    "strategy",
                    th.StringType,
                    default="source_id",
                    allowed_values=["source_id", "hash"],
                    description="Strategy for generating idempotency keys",
                ),
            ),
            description="Idempotency configuration",
        ),
        th.Property(
            "source_fields",
            th.ObjectType(
                th.Property(
                    "customer_source_id_field",
                    th.StringType,
                    default="source_customer_id",
                    description="Field name in source records containing the customer ID",
                ),
                th.Property(
                    "customer_metadata_key",
                    th.StringType,
                    default="source_customer_id",
                    description="Metadata key used in Stripe to store the source customer ID",
                ),
                th.Property(
                    "subscription_source_id_field",
                    th.StringType,
                    default="source_subscription_id",
                    description="Field name in source records containing the subscription ID",
                ),
                th.Property(
                    "subscription_metadata_key",
                    th.StringType,
                    default="source_subscription_id",
                    description="Metadata key used in Stripe to store the source subscription ID",
                ),
                th.Property(
                    "additional_metadata_fields",
                    th.ArrayType(th.StringType),
                    default=[],
                    description="Additional fields from source records to include in Stripe metadata",
                ),
            ),
            description="Configuration for source system field names",
        ),
        th.Property(
            "plan_code_to_price_id",
            th.ObjectType(),
            default={},
            description="Mapping of plan codes to Stripe price IDs",
        ),
        th.Property(
            "plan_code_mapping_file",
            th.StringType,
            description="Path to JSON/YAML file containing plan code mapping",
        ),
        th.Property(
            "state_emit_interval",
            th.IntegerType,
            default=100,
            description="Number of records between STATE message emissions",
        ),
        th.Property(
            "rate_limit_per_sec",
            th.NumberType,
            default=25.0,
            description="Maximum Stripe API requests per second",
        ),
        th.Property(
            "mapping_db_path",
            th.StringType,
            default=".target-stripe-mapping.db",
            description="Path to SQLite database for ID mappings",
        ),
        th.Property(
            "batch_size",
            th.IntegerType,
            default=50,
            description="Number of records to process in a batch",
        ),
        th.Property(
            "max_retries",
            th.IntegerType,
            default=3,
            description="Maximum number of retries for transient errors",
        ),
        th.Property(
            "retry_backoff_base",
            th.NumberType,
            default=2.0,
            description="Base for exponential backoff (seconds)",
        ),
    ).to_dict()

    default_sink_class = CustomerSink

    def __init__(
        self,
        config: dict | PurePath | str | list[PurePath | str] | None = None,
        parse_env_config: bool = False,
        validate_config: bool = True,
    ) -> None:
        """Initialize the Target.

        Args:
            config: Target configuration.
            parse_env_config: Whether to parse config from environment.
            validate_config: Whether to validate the config.
        """
        super().__init__(
            config=config,
            parse_env_config=parse_env_config,
            validate_config=validate_config,
        )

        self._parsed_config = parse_config(dict(self.config))
        self._mapping_store = MappingStore(self._parsed_config.mapping_db_path)
        self._stripe_client = StripeClientWrapper(
            config=self._parsed_config,
            mapping_store=self._mapping_store,
        )

        self._records_processed = 0
        self._state_emit_counter = 0

        logger.info(
            "Initialized target-stripe (mode=%s, dry_run=%s, hard_fail=%s)",
            self._parsed_config.stripe_mode.value,
            self._parsed_config.dry_run,
            self._parsed_config.hard_fail,
        )

    @property
    def state_emit_interval(self) -> int:
        """Get the state emit interval."""
        return self._parsed_config.state_emit_interval

    def get_sink_class(self, stream_name: str) -> type:
        """Get the sink class for a stream.

        Args:
            stream_name: Name of the stream.

        Returns:
            Sink class for the stream.

        Raises:
            ValueError: If stream is not supported.
        """
        sink_class = get_sink_class(stream_name)
        if sink_class is None:
            logger.warning(
                "Unsupported stream '%s'. Supported streams: customers, subscriptions",
                stream_name,
            )
            raise ValueError(
                f"Unsupported stream: {stream_name}. Supported streams: customers, subscriptions"
            )
        return sink_class

    @staticmethod
    def _normalize_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize JSON Schema to be compatible with newer draft versions.

        Converts old-style exclusiveMaximum/exclusiveMinimum (boolean) to new style.

        Args:
            schema: JSON Schema dictionary.

        Returns:
            Normalized schema.
        """
        if not isinstance(schema, dict):
            return schema

        # Create a copy to avoid modifying the original
        normalized = schema.copy()

        # Recursively normalize properties
        if "properties" in normalized and isinstance(normalized["properties"], dict):
            normalized["properties"] = {
                key: TargetStripe._normalize_schema(value)
                for key, value in normalized["properties"].items()
            }

        # Handle old-style exclusiveMaximum (boolean + maximum)
        if "exclusiveMaximum" in normalized:
            if normalized["exclusiveMaximum"] is True and "maximum" in normalized:
                # Old style: exclusiveMaximum: true, maximum: N
                # New style: exclusiveMaximum: N (no maximum field)
                normalized["exclusiveMaximum"] = normalized["maximum"]
                del normalized["maximum"]
            elif normalized["exclusiveMaximum"] is False:
                # Old style: exclusiveMaximum: false, maximum: N
                # New style: just use maximum: N
                del normalized["exclusiveMaximum"]

        # Handle old-style exclusiveMinimum (boolean + minimum)
        if "exclusiveMinimum" in normalized:
            if normalized["exclusiveMinimum"] is True and "minimum" in normalized:
                # Old style: exclusiveMinimum: true, minimum: N
                # New style: exclusiveMinimum: N (no minimum field)
                normalized["exclusiveMinimum"] = normalized["minimum"]
                del normalized["minimum"]
            elif normalized["exclusiveMinimum"] is False:
                # Old style: exclusiveMinimum: false, minimum: N
                # New style: just use minimum: N
                del normalized["exclusiveMinimum"]

        # Remove multipleOf constraint to avoid decimal operation errors
        # tap-postgres adds multipleOf: 1 for numeric fields, which can cause
        # decimal.InvalidOperation errors with large numbers
        if "multipleOf" in normalized and normalized["multipleOf"] == 1:
            del normalized["multipleOf"]

        # Recursively normalize items (for arrays)
        if "items" in normalized and isinstance(normalized["items"], dict):
            normalized["items"] = TargetStripe._normalize_schema(normalized["items"])

        # Recursively normalize anyOf, allOf, oneOf
        for key in ["anyOf", "allOf", "oneOf"]:
            if key in normalized and isinstance(normalized[key], list):
                normalized[key] = [TargetStripe._normalize_schema(item) for item in normalized[key]]

        return normalized

    def get_sink(
        self,
        stream_name: str,
        *,
        record: dict | None = None,
        schema: dict | None = None,
        key_properties: Sequence[str] | None = None,
    ) -> CustomerSink | SubscriptionSink:
        """Get or create a sink for a stream.

        Args:
            stream_name: Name of the stream.
            record: Optional record (unused).
            schema: JSON Schema for the stream.
            key_properties: Key properties.

        Returns:
            Sink instance.
        """
        # Normalize schema to handle old JSON Schema draft formats
        if schema is not None:
            schema = self._normalize_schema(schema)

        sink = super().get_sink(
            stream_name,
            record=record,
            schema=schema,
            key_properties=key_properties,
        )
        sink.max_size = self._parsed_config.batch_size  # type: ignore[misc]
        return sink  # type: ignore

    def _process_record_message(self, message_dict: dict) -> None:
        """Process a RECORD message.

        Override to add state emission tracking.

        Args:
            message_dict: The message dictionary.
        """
        super()._process_record_message(message_dict)
        self._records_processed += 1
        self._state_emit_counter += 1

        if self._state_emit_counter >= self.state_emit_interval:
            self._state_emit_counter = 0
            self._emit_state_message()

    def _emit_state_message(self) -> None:
        """Emit a STATE message with current bookmarks."""
        if self._latest_state:
            self._write_state_message(self._latest_state)
            logger.debug("Emitted state after %d records", self._records_processed)

    def process_endofpipe(self) -> None:
        """Process end of input.

        Clean up resources and log final statistics.
        """
        super().process_endofpipe()

        stats = self._stripe_client.stats
        logger.info(
            "Target-stripe completed: "
            "customers_created=%d, customers_updated=%d, "
            "subscriptions_created=%d, subscriptions_updated=%d, "
            "errors=%d, retries=%d",
            stats["customers_created"],
            stats["customers_updated"],
            stats["subscriptions_created"],
            stats["subscriptions_updated"],
            stats["errors"],
            stats["retries"],
        )

        self._mapping_store.cleanup_expired_keys()
        self._mapping_store.close()


if __name__ == "__main__":
    TargetStripe.cli()
