"""Tests for mapping persistence module."""

from __future__ import annotations

from target_stripe.mapping import (
    EntityType,
    MappingStore,
    generate_idempotency_key,
)


class TestMappingStore:
    """Tests for MappingStore."""

    def test_set_and_get_mapping(self, mapping_store: MappingStore) -> None:
        """Test setting and getting a mapping."""
        mapping_store.set_mapping(
            EntityType.CUSTOMER,
            "source_123",
            "cus_stripe_123",
        )
        result = mapping_store.get_stripe_id(EntityType.CUSTOMER, "source_123")
        assert result == "cus_stripe_123"

    def test_get_nonexistent_mapping(self, mapping_store: MappingStore) -> None:
        """Test getting a mapping that doesn't exist."""
        result = mapping_store.get_stripe_id(EntityType.CUSTOMER, "nonexistent")
        assert result is None

    def test_update_existing_mapping(self, mapping_store: MappingStore) -> None:
        """Test updating an existing mapping."""
        mapping_store.set_mapping(
            EntityType.CUSTOMER,
            "source_123",
            "cus_old",
        )
        mapping_store.set_mapping(
            EntityType.CUSTOMER,
            "source_123",
            "cus_new",
        )
        result = mapping_store.get_stripe_id(EntityType.CUSTOMER, "source_123")
        assert result == "cus_new"

    def test_separate_entity_types(self, mapping_store: MappingStore) -> None:
        """Test that different entity types are stored separately."""
        mapping_store.set_mapping(
            EntityType.CUSTOMER,
            "id_123",
            "cus_123",
        )
        mapping_store.set_mapping(
            EntityType.SUBSCRIPTION,
            "id_123",
            "sub_456",
        )

        customer_result = mapping_store.get_stripe_id(EntityType.CUSTOMER, "id_123")
        subscription_result = mapping_store.get_stripe_id(
            EntityType.SUBSCRIPTION, "id_123"
        )

        assert customer_result == "cus_123"
        assert subscription_result == "sub_456"

    def test_get_source_id(self, mapping_store: MappingStore) -> None:
        """Test reverse lookup from Stripe ID to source ID."""
        mapping_store.set_mapping(
            EntityType.CUSTOMER,
            "source_123",
            "cus_stripe_123",
        )
        result = mapping_store.get_source_id(EntityType.CUSTOMER, "cus_stripe_123")
        assert result == "source_123"

    def test_delete_mapping(self, mapping_store: MappingStore) -> None:
        """Test deleting a mapping."""
        mapping_store.set_mapping(
            EntityType.CUSTOMER,
            "source_123",
            "cus_stripe_123",
        )
        deleted = mapping_store.delete_mapping(EntityType.CUSTOMER, "source_123")
        assert deleted is True

        result = mapping_store.get_stripe_id(EntityType.CUSTOMER, "source_123")
        assert result is None

    def test_delete_nonexistent_mapping(self, mapping_store: MappingStore) -> None:
        """Test deleting a mapping that doesn't exist."""
        deleted = mapping_store.delete_mapping(EntityType.CUSTOMER, "nonexistent")
        assert deleted is False

    def test_get_all_mappings(self, mapping_store: MappingStore) -> None:
        """Test getting all mappings for an entity type."""
        mapping_store.set_mapping(EntityType.CUSTOMER, "src_1", "cus_1")
        mapping_store.set_mapping(EntityType.CUSTOMER, "src_2", "cus_2")
        mapping_store.set_mapping(EntityType.SUBSCRIPTION, "src_3", "sub_3")

        all_customers = mapping_store.get_all_mappings(EntityType.CUSTOMER)
        assert len(all_customers) == 2
        assert all_customers["src_1"] == "cus_1"
        assert all_customers["src_2"] == "cus_2"

    def test_mapping_with_metadata(self, mapping_store: MappingStore) -> None:
        """Test storing mapping with metadata."""
        mapping_store.set_mapping(
            EntityType.CUSTOMER,
            "source_123",
            "cus_stripe_123",
            metadata={"email": "test@example.com", "created_by": "migration"},
        )
        result = mapping_store.get_stripe_id(EntityType.CUSTOMER, "source_123")
        assert result == "cus_stripe_123"

    def test_idempotency_key_storage(self, mapping_store: MappingStore) -> None:
        """Test storing and retrieving idempotency keys."""
        mapping_store.store_idempotency_key(
            "test_key_123",
            EntityType.CUSTOMER,
            "source_123",
            stripe_id="cus_result_123",
        )
        result = mapping_store.get_idempotency_result("test_key_123")
        assert result == "cus_result_123"

    def test_idempotency_key_without_result(self, mapping_store: MappingStore) -> None:
        """Test idempotency key stored without result."""
        mapping_store.store_idempotency_key(
            "test_key_pending",
            EntityType.CUSTOMER,
            "source_123",
            stripe_id=None,
        )
        result = mapping_store.get_idempotency_result("test_key_pending")
        assert result is None

    def test_cleanup_expired_keys(self, mapping_store: MappingStore) -> None:
        """Test cleanup of expired idempotency keys."""
        mapping_store.store_idempotency_key(
            "will_expire",
            EntityType.CUSTOMER,
            "source_123",
            stripe_id="cus_123",
            ttl_hours=-1,  # Already expired
        )
        removed = mapping_store.cleanup_expired_keys()
        assert removed == 1

        result = mapping_store.get_idempotency_result("will_expire")
        assert result is None


class TestGenerateIdempotencyKey:
    """Tests for generate_idempotency_key function."""

    def test_source_id_strategy(self) -> None:
        """Test source_id idempotency strategy."""
        key = generate_idempotency_key(
            EntityType.CUSTOMER,
            "source_123",
            "create",
            strategy="source_id",
        )
        assert key == "customer:create:source_123"

    def test_hash_strategy(self) -> None:
        """Test hash idempotency strategy."""
        record_data = {"email": "test@example.com", "name": "Test"}
        key = generate_idempotency_key(
            EntityType.CUSTOMER,
            "source_123",
            "create",
            strategy="hash",
            record_data=record_data,
        )
        assert key.startswith("customer:create:source_123:")
        assert len(key) > len("customer:create:source_123:")

    def test_hash_strategy_deterministic(self) -> None:
        """Test that hash strategy produces consistent keys."""
        record_data = {"email": "test@example.com", "name": "Test"}
        key1 = generate_idempotency_key(
            EntityType.CUSTOMER,
            "source_123",
            "create",
            strategy="hash",
            record_data=record_data,
        )
        key2 = generate_idempotency_key(
            EntityType.CUSTOMER,
            "source_123",
            "create",
            strategy="hash",
            record_data=record_data,
        )
        assert key1 == key2

    def test_hash_strategy_different_data(self) -> None:
        """Test that different data produces different keys."""
        key1 = generate_idempotency_key(
            EntityType.CUSTOMER,
            "source_123",
            "create",
            strategy="hash",
            record_data={"email": "test1@example.com"},
        )
        key2 = generate_idempotency_key(
            EntityType.CUSTOMER,
            "source_123",
            "create",
            strategy="hash",
            record_data={"email": "test2@example.com"},
        )
        assert key1 != key2

    def test_different_operations(self) -> None:
        """Test that different operations produce different keys."""
        key_create = generate_idempotency_key(
            EntityType.CUSTOMER,
            "source_123",
            "create",
        )
        key_update = generate_idempotency_key(
            EntityType.CUSTOMER,
            "source_123",
            "update",
        )
        assert key_create != key_update
