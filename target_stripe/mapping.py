"""ID mapping persistence layer for source_id → stripe_id mappings."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class EntityType(str, Enum):
    """Types of entities that can be mapped."""

    CUSTOMER = "customer"
    SUBSCRIPTION = "subscription"


class MappingStore:
    """SQLite-based persistence for source_id → stripe_id mappings.

    Thread-safe implementation with connection pooling per thread.
    """

    def __init__(self, db_path: str | Path = ".target-stripe-mapping.db") -> None:
        """Initialize the mapping store.

        Args:
            db_path: Path to the SQLite database file.
        """
        self.db_path = Path(db_path)
        self._local = threading.local()
        self._init_schema()

    def _get_connection(self) -> sqlite3.Connection:
        """Get a thread-local database connection."""
        if not hasattr(self._local, "connection") or self._local.connection is None:
            self._local.connection = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
                isolation_level=None,  # Autocommit mode
            )
            self._local.connection.row_factory = sqlite3.Row
            self._local.connection.execute("PRAGMA journal_mode=WAL")
            self._local.connection.execute("PRAGMA synchronous=NORMAL")
        return self._local.connection  # type: ignore[no-any-return]

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """Context manager for database transactions."""
        conn = self._get_connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def _init_schema(self) -> None:
        """Initialize the database schema."""
        conn = self._get_connection()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS id_mappings (
                entity_type TEXT NOT NULL,
                source_id TEXT NOT NULL,
                stripe_id TEXT NOT NULL,
                metadata TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (entity_type, source_id)
            );

            CREATE INDEX IF NOT EXISTS idx_stripe_id
            ON id_mappings(entity_type, stripe_id);

            CREATE TABLE IF NOT EXISTS idempotency_keys (
                key TEXT PRIMARY KEY,
                entity_type TEXT NOT NULL,
                source_id TEXT NOT NULL,
                stripe_id TEXT,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_idempotency_expires
            ON idempotency_keys(expires_at);
            """
        )

    def get_stripe_id(self, entity_type: EntityType, source_id: str) -> str | None:
        """Get the Stripe ID for a source ID.

        Args:
            entity_type: Type of entity (customer, subscription).
            source_id: Source system ID.

        Returns:
            Stripe ID if found, None otherwise.
        """
        conn = self._get_connection()
        cursor = conn.execute(
            "SELECT stripe_id FROM id_mappings WHERE entity_type = ? AND source_id = ?",
            (entity_type.value, source_id),
        )
        row = cursor.fetchone()
        return row["stripe_id"] if row else None

    def get_source_id(self, entity_type: EntityType, stripe_id: str) -> str | None:
        """Get the source ID for a Stripe ID.

        Args:
            entity_type: Type of entity (customer, subscription).
            stripe_id: Stripe ID.

        Returns:
            Source ID if found, None otherwise.
        """
        conn = self._get_connection()
        cursor = conn.execute(
            "SELECT source_id FROM id_mappings WHERE entity_type = ? AND stripe_id = ?",
            (entity_type.value, stripe_id),
        )
        row = cursor.fetchone()
        return row["source_id"] if row else None

    def set_mapping(
        self,
        entity_type: EntityType,
        source_id: str,
        stripe_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Store a source_id → stripe_id mapping.

        Args:
            entity_type: Type of entity (customer, subscription).
            source_id: Source system ID.
            stripe_id: Stripe ID.
            metadata: Optional metadata to store with the mapping.
        """
        now = datetime.now(timezone.utc).isoformat()
        metadata_json = json.dumps(metadata) if metadata else None

        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO id_mappings (entity_type, source_id, stripe_id, metadata, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(entity_type, source_id) DO UPDATE SET
                    stripe_id = excluded.stripe_id,
                    metadata = excluded.metadata,
                    updated_at = excluded.updated_at
                """,
                (entity_type.value, source_id, stripe_id, metadata_json, now, now),
            )

    def delete_mapping(self, entity_type: EntityType, source_id: str) -> bool:
        """Delete a mapping.

        Args:
            entity_type: Type of entity.
            source_id: Source system ID.

        Returns:
            True if a mapping was deleted, False otherwise.
        """
        conn = self._get_connection()
        cursor = conn.execute(
            "DELETE FROM id_mappings WHERE entity_type = ? AND source_id = ?",
            (entity_type.value, source_id),
        )
        return cursor.rowcount > 0

    def get_all_mappings(self, entity_type: EntityType) -> dict[str, str]:
        """Get all mappings for an entity type.

        Args:
            entity_type: Type of entity.

        Returns:
            Dictionary of source_id → stripe_id mappings.
        """
        conn = self._get_connection()
        cursor = conn.execute(
            "SELECT source_id, stripe_id FROM id_mappings WHERE entity_type = ?",
            (entity_type.value,),
        )
        return {row["source_id"]: row["stripe_id"] for row in cursor.fetchall()}

    def store_idempotency_key(
        self,
        key: str,
        entity_type: EntityType,
        source_id: str,
        stripe_id: str | None = None,
        ttl_hours: int = 24,
    ) -> None:
        """Store an idempotency key.

        Args:
            key: The idempotency key.
            entity_type: Type of entity.
            source_id: Source system ID.
            stripe_id: Resulting Stripe ID (if operation completed).
            ttl_hours: Time to live in hours.
        """
        now = datetime.now(timezone.utc)
        expires = datetime.fromtimestamp(now.timestamp() + (ttl_hours * 3600), tz=timezone.utc)

        with self._transaction() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO idempotency_keys
                (key, entity_type, source_id, stripe_id, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    entity_type.value,
                    source_id,
                    stripe_id,
                    now.isoformat(),
                    expires.isoformat(),
                ),
            )

    def get_idempotency_result(self, key: str) -> str | None:
        """Get the Stripe ID for a previously used idempotency key.

        Args:
            key: The idempotency key.

        Returns:
            Stripe ID if the key was used and has a result, None otherwise.
        """
        conn = self._get_connection()
        now = datetime.now(timezone.utc).isoformat()
        cursor = conn.execute(
            """
            SELECT stripe_id FROM idempotency_keys
            WHERE key = ? AND expires_at > ? AND stripe_id IS NOT NULL
            """,
            (key, now),
        )
        row = cursor.fetchone()
        return row["stripe_id"] if row else None

    def cleanup_expired_keys(self) -> int:
        """Remove expired idempotency keys.

        Returns:
            Number of keys removed.
        """
        conn = self._get_connection()
        now = datetime.now(timezone.utc).isoformat()
        cursor = conn.execute("DELETE FROM idempotency_keys WHERE expires_at < ?", (now,))
        return cursor.rowcount

    def close(self) -> None:
        """Close the database connection."""
        if hasattr(self._local, "connection") and self._local.connection:
            self._local.connection.close()
            self._local.connection = None


def generate_idempotency_key(
    entity_type: EntityType,
    source_id: str,
    operation: str,
    strategy: str = "source_id",
    record_data: dict[str, Any] | None = None,
) -> str:
    """Generate an idempotency key for a Stripe operation.

    Args:
        entity_type: Type of entity.
        source_id: Source system ID.
        operation: Operation type (create, update).
        strategy: Key generation strategy (source_id or hash).
        record_data: Record data for hash strategy.

    Returns:
        Idempotency key string.
    """
    if strategy == "hash" and record_data:
        data_str = json.dumps(record_data, sort_keys=True, default=str)
        data_hash = hashlib.sha256(data_str.encode()).hexdigest()[:16]
        return f"{entity_type.value}:{operation}:{source_id}:{data_hash}"
    else:
        return f"{entity_type.value}:{operation}:{source_id}"
