"""Tests for schema normalization."""

from target_stripe.target import TargetStripe


class TestSchemaNormalization:
    """Test schema normalization for compatibility with different JSON Schema drafts."""

    def test_normalize_exclusive_maximum_true(self) -> None:
        """Test normalization of old-style exclusiveMaximum: true."""
        old_schema = {
            "type": "object",
            "properties": {
                "field": {
                    "type": "number",
                    "maximum": 100,
                    "exclusiveMaximum": True,
                }
            },
        }

        normalized = TargetStripe._normalize_schema(old_schema)

        prop = normalized["properties"]["field"]
        assert prop["exclusiveMaximum"] == 100
        assert "maximum" not in prop

    def test_normalize_exclusive_maximum_false(self) -> None:
        """Test normalization of old-style exclusiveMaximum: false."""
        old_schema = {
            "type": "object",
            "properties": {
                "field": {
                    "type": "number",
                    "maximum": 100,
                    "exclusiveMaximum": False,
                }
            },
        }

        normalized = TargetStripe._normalize_schema(old_schema)

        prop = normalized["properties"]["field"]
        assert prop["maximum"] == 100
        assert "exclusiveMaximum" not in prop

    def test_normalize_exclusive_minimum_true(self) -> None:
        """Test normalization of old-style exclusiveMinimum: true."""
        old_schema = {
            "type": "object",
            "properties": {
                "field": {
                    "type": "number",
                    "minimum": 0,
                    "exclusiveMinimum": True,
                }
            },
        }

        normalized = TargetStripe._normalize_schema(old_schema)

        prop = normalized["properties"]["field"]
        assert prop["exclusiveMinimum"] == 0
        assert "minimum" not in prop

    def test_remove_multipleof_one(self) -> None:
        """Test removal of multipleOf: 1 constraint."""
        schema = {
            "type": "object",
            "properties": {
                "id": {
                    "type": "number",
                    "multipleOf": 1,
                }
            },
        }

        normalized = TargetStripe._normalize_schema(schema)

        prop = normalized["properties"]["id"]
        assert "multipleOf" not in prop

    def test_keep_multipleof_for_non_numeric(self) -> None:
        """Test that multipleOf is kept for non-numeric types like integer."""
        schema = {
            "type": "object",
            "properties": {
                "count": {
                    "type": "integer",
                    "multipleOf": 10,
                }
            },
        }

        normalized = TargetStripe._normalize_schema(schema)

        prop = normalized["properties"]["count"]
        assert prop["multipleOf"] == 10

    def test_normalize_postgres_numeric_field(self) -> None:
        """Test normalization of PostgreSQL numeric field schema."""
        # This is the exact schema that tap-postgres sends for numeric columns
        postgres_schema = {
            "type": "object",
            "properties": {
                "chargify_customer_id": {
                    "type": ["null", "number"],
                    "maximum": 99999999999999999999,
                    "exclusiveMaximum": True,
                    "multipleOf": 1,
                }
            },
        }

        normalized = TargetStripe._normalize_schema(postgres_schema)

        prop = normalized["properties"]["chargify_customer_id"]
        # exclusiveMaximum should be converted
        assert prop["exclusiveMaximum"] == 99999999999999999999
        assert "maximum" not in prop
        # multipleOf: 1 should be removed
        assert "multipleOf" not in prop
        # Type should remain unchanged
        assert prop["type"] == ["null", "number"]

    def test_normalize_nested_schema(self) -> None:
        """Test normalization of nested schemas."""
        schema = {
            "type": "object",
            "properties": {
                "address": {
                    "type": "object",
                    "properties": {
                        "zip": {
                            "type": "number",
                            "maximum": 99999,
                            "exclusiveMaximum": True,
                            "multipleOf": 1,
                        }
                    },
                }
            },
        }

        normalized = TargetStripe._normalize_schema(schema)

        zip_prop = normalized["properties"]["address"]["properties"]["zip"]
        assert zip_prop["exclusiveMaximum"] == 99999
        assert "maximum" not in zip_prop
        assert "multipleOf" not in zip_prop

    def test_normalize_preserves_other_fields(self) -> None:
        """Test that normalization preserves other schema fields."""
        schema = {
            "type": "object",
            "properties": {
                "id": {
                    "type": "number",
                    "maximum": 100,
                    "exclusiveMaximum": True,
                    "multipleOf": 1,
                    "description": "User ID",
                    "examples": [1, 2, 3],
                }
            },
            "required": ["id"],
        }

        normalized = TargetStripe._normalize_schema(schema)

        prop = normalized["properties"]["id"]
        assert prop["description"] == "User ID"
        assert prop["examples"] == [1, 2, 3]
        assert normalized["required"] == ["id"]
