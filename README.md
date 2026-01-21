# target-stripe

A Singer target for Stripe that upserts Customers and Subscriptions via the Stripe API.

Built with the [Meltano Singer SDK](https://sdk.meltano.com).

## Features

- **Upsert Stripe Customers** - Create or update customers with deduplication via metadata search
- **Upsert Stripe Subscriptions** - Create or update subscriptions with automatic customer resolution
- **Idempotent Operations** - Uses Stripe idempotency keys to ensure safe re-runs
- **ID Mapping Persistence** - SQLite-backed storage of source_id → stripe_id mappings
- **Retry Logic** - Exponential backoff for transient Stripe API errors
- **Rate Limiting** - Configurable rate limiting to stay within Stripe API limits
- **Dry Run Mode** - Validate data without making actual Stripe API calls
- **Flexible Error Handling** - Continue on errors or fail fast based on configuration

## Installation

```bash
# Install with pip
pip install target-stripe

# Or install with pipx for isolation
pipx install target-stripe

# Or install from source
git clone https://github.com/marcossantiago/target-stripe.git
cd target-stripe
pip install -e .
```

## Configuration

### Required Settings

| Setting | Type | Description |
|---------|------|-------------|
| `stripe_api_key` | string | Stripe API key (starts with `sk_test_` or `sk_live_`) |

### Optional Settings

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `stripe_mode` | string | `test` | Stripe mode: `test` or `live` |
| `default_currency` | string | `usd` | Default currency for subscriptions (ISO 4217) |
| `dry_run` | boolean | `false` | Validate data without writing to Stripe |
| `hard_fail` | boolean | `false` | Fail immediately on any record error |
| `idempotency.strategy` | string | `source_id` | Strategy for idempotency keys: `source_id` or `hash` |
| `source_fields.customer_source_id_field` | string | `source_customer_id` | Field name for customer source ID |
| `source_fields.customer_metadata_key` | string | `null` | Stripe metadata key for customer ID (defaults to `customer_source_id_field`) |
| `source_fields.subscription_source_id_field` | string | `source_subscription_id` | Field name for subscription source ID |
| `source_fields.subscription_metadata_key` | string | `null` | Stripe metadata key for subscription ID (defaults to `subscription_source_id_field`) |
| `source_fields.subscription_customer_id_field` | string | `null` | Field in subscription records referencing customer |
| `source_fields.cancel_at_period_end_field` | string | `cancel_at_period_end` | Field containing cancellation flag |
| `source_fields.billing_cycle_anchor_field` | string | `null` | Field containing renewal date (enables billing cycle preservation) |
| `source_fields.backdate_start_field` | string | `null` | Field containing period start date |
| `source_fields.proration_behavior` | string | `none` | Stripe proration behavior: `none`, `create_prorations`, `always_invoice` |
| `source_fields.additional_metadata_fields` | array | `[]` | Additional fields to copy to Stripe metadata |
| `past_due_handling` | string | `skip` | Handle past-due subscriptions: `skip` or `create_fresh` |
| `skip_existence_check` | boolean | `false` | Skip metadata search for existing records (faster for initial migrations) |
| `plan_code_to_price_id` | object | `{}` | Mapping of plan codes to Stripe price IDs |
| `plan_code_mapping_file` | string | | Path to JSON/YAML file with plan code mappings |
| `state_emit_interval` | integer | `100` | Records between STATE emissions |
| `rate_limit_per_sec` | number | `25.0` | Max Stripe API requests per second |
| `mapping_db_path` | string | `.target-stripe-mapping.db` | Path to SQLite mapping database |
| `batch_size` | integer | `50` | Records per batch |
| `max_retries` | integer | `3` | Max retries for transient errors |
| `retry_backoff_base` | number | `2.0` | Base for exponential backoff |

### Example Configuration

```json
{
  "stripe_api_key": "sk_test_your_key_here",
  "stripe_mode": "test",
  "dry_run": false,
  "hard_fail": false,
  "idempotency": {
    "strategy": "source_id"
  },
  "source_fields": {
    "customer_source_id_field": "source_customer_id",
    "subscription_source_id_field": "source_subscription_id",
    "additional_metadata_fields": []
  },
  "plan_code_to_price_id": {
    "basic_monthly": "price_1234567890",
    "pro_monthly": "price_0987654321",
    "enterprise_annual": "price_abcdefghij"
  },
  "rate_limit_per_sec": 25,
  "state_emit_interval": 100
}
```

### Configuring Source Fields

The `source_fields` configuration allows you to adapt the target to work with any source system by specifying which field names contain source IDs and what metadata keys to use in Stripe.

**Metadata Keys:** By default, `customer_metadata_key` and `subscription_metadata_key` use the same value as their corresponding `source_id_field`. Only set them explicitly if you want different names in Stripe metadata.

**Example for Chargify migration:**

```json
{
  "source_fields": {
    "customer_source_id_field": "chargify_customer_id",
    "subscription_source_id_field": "chargify_subscription_id",
    "subscription_customer_id_field": "chargify_customer_id",
    "cancel_at_period_end_field": "cancel_at_end_of_period",
    "billing_cycle_anchor_field": "current_period_ends_at",
    "backdate_start_field": "current_period_started_at",
    "proration_behavior": "none",
    "additional_metadata_fields": ["chargify_customer_ref"]
  },
  "past_due_handling": "skip",
  "skip_existence_check": true
}
```

**Example with explicit metadata keys (when you want different names in Stripe):**

```json
{
  "source_fields": {
    "customer_source_id_field": "external_id",
    "customer_metadata_key": "legacy_customer_id",
    "subscription_source_id_field": "external_id",
    "subscription_metadata_key": "legacy_subscription_id",
    "additional_metadata_fields": ["salesforce_id", "hubspot_id"]
  }
}
```

### Billing Cycle Preservation

When migrating subscriptions from another billing system, you can preserve the original renewal dates to avoid double-charging customers who already paid for their current period.

**Enable preservation:** Set `billing_cycle_anchor_field` to the field containing the renewal date in your source data.

**Behavior:**
- Subscriptions with future renewal dates are created with preserved billing cycles
- Past-due subscriptions are handled based on `past_due_handling` (`skip` or `create_fresh`)
- Uses `proration_behavior: "none"` to prevent charges for already-paid periods
- Sets `collection_method: "send_invoice"` for subscriptions without payment methods

**Example:**
```json
{
  "source_fields": {
    "billing_cycle_anchor_field": "current_period_ends_at",
    "backdate_start_field": "current_period_started_at",
    "proration_behavior": "none"
  },
  "past_due_handling": "skip"
}
```

**Omit billing_cycle_anchor_field** to create fresh billing cycles starting from migration date.

## Supported Streams

### customers

Creates or updates Stripe Customers.

**Input Fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `source_customer_id` | string | Yes* | Source customer ID (configurable via `source_fields.customer_source_id_field`) |
| `customer_id` | string | Yes* | Alternative source ID field |
| `id` | string | Yes* | Alternative source ID field |
| `email` | string | No | Customer email |
| `name` | string | No | Full name |
| `first_name` | string | No | First name (combined with last_name) |
| `last_name` | string | No | Last name |
| `phone` | string | No | Phone number |
| `address` | object | No | Address object |
| `address_line1` | string | No | Street address line 1 |
| `city` | string | No | City |
| `state` | string | No | State/province |
| `postal_code` | string | No | Postal/ZIP code |
| `country` | string | No | Country code |
| `metadata` | object | No | Additional metadata |

*One of the source ID fields is required. The configured `customer_source_id_field` takes priority.

**Stripe Metadata Set:**
- Configured `customer_metadata_key` - Always set from source ID
- Any fields listed in `additional_metadata_fields` - Copied if present in source

### subscriptions

Creates or updates Stripe Subscriptions.

**Input Fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `source_subscription_id` | string | Yes* | Source subscription ID (configurable via `source_fields.subscription_source_id_field`) |
| `subscription_id` | string | Yes* | Alternative source ID field |
| `id` | string | Yes* | Alternative source ID field |
| `customer_id` | string | Yes | Customer reference (source or Stripe ID, configurable via `source_fields.subscription_customer_id_field`) |
| `price_id` | string | Yes** | Stripe price ID |
| `plan_code` | string | Yes** | Plan code (mapped to price_id) |
| `quantity` | integer | No | Subscription quantity |
| `trial_end` | string/int | No | Trial end timestamp or "now" |
| `coupon` | string | No | Coupon code to apply |
| `cancel_at_period_end` | boolean | No | Cancel at period end flag (configurable via `source_fields.cancel_at_period_end_field`) |
| `current_period_ends_at` | string/int | No | Renewal date for billing cycle preservation (configurable via `source_fields.billing_cycle_anchor_field`) |
| `current_period_started_at` | string/int | No | Period start date for billing cycle preservation (configurable via `source_fields.backdate_start_field`) |
| `metadata` | object | No | Additional metadata |

*One of the source ID fields is required. The configured `subscription_source_id_field` takes priority.
**Either `price_id` or `plan_code` is required.

**Stripe Metadata Set:**
- Configured `subscription_metadata_key` - Always set from source ID

## Usage

### Standalone

```bash
# Pipe Singer messages directly
cat singer_messages.jsonl | target-stripe --config config.json

# With a tap
tap-chargify | target-stripe --config config.json
```

### With Meltano

Add to your `meltano.yml`:

```yaml
plugins:
  loaders:
    - name: target-stripe
      namespace: target_stripe
      pip_url: target-stripe
      executable: target-stripe
      settings:
        - name: stripe_api_key
          kind: password
          label: Stripe API Key
          description: Stripe API key (sk_test_* or sk_live_*)
        - name: stripe_mode
          kind: options
          options:
            - value: test
              label: Test
            - value: live
              label: Live
          value: test
          description: Stripe environment mode
        - name: dry_run
          kind: boolean
          value: false
          description: Validate without writing to Stripe
        - name: hard_fail
          kind: boolean
          value: false
          description: Fail on first error
        - name: plan_code_to_price_id
          kind: object
          value: {}
          description: Plan code to Stripe price ID mapping
        - name: rate_limit_per_sec
          kind: integer
          value: 25
          description: Max API requests per second
```

Run with Meltano:

```bash
# Install the plugin
meltano install loader target-stripe

# Configure
meltano config target-stripe set stripe_api_key sk_test_your_key

# Run a pipeline
meltano run tap-chargify target-stripe

# Run with select
meltano run tap-chargify target-stripe --select customers
meltano run tap-chargify target-stripe --select subscriptions
```

### Example Pipeline

```bash
# Full migration from Chargify to Stripe
meltano run tap-chargify target-stripe

# Customers only
meltano run tap-chargify target-stripe --select 'customers.*'

# Dry run to validate
meltano config target-stripe set dry_run true
meltano run tap-chargify target-stripe

# Production run
meltano config target-stripe set dry_run false
meltano config target-stripe set stripe_mode live
meltano config target-stripe set stripe_api_key sk_live_your_key
meltano run tap-chargify target-stripe
```

## Behavior

### Customer-Subscription Ordering

The target assumes customers are processed before their subscriptions. If using a tap that provides both streams:

1. Configure the tap to emit customers first
2. Or run separate pipelines: customers first, then subscriptions

If a subscription references a customer that doesn't exist:
- With `hard_fail=true`: The pipeline fails immediately
- With `hard_fail=false`: The subscription is skipped with an error logged

### Deduplication

Customers are deduplicated using:
1. Local SQLite mapping (source_id → stripe_id)
2. Stripe metadata search using configured `customer_metadata_key` (unless `skip_existence_check=true`)

**Performance optimization for initial migrations:** Set `skip_existence_check=true` to skip metadata searches and only use the local mapping DB. This reduces API calls by ~50% when you know records don't exist in Stripe yet.

### Idempotency

Operations use Stripe idempotency keys based on:
- `source_id` strategy: `{entity}:{operation}:{source_id}`
- `hash` strategy: `{entity}:{operation}:{source_id}:{data_hash}`

### Error Handling

Transient errors (rate limits, connection issues, 5xx) are retried with exponential backoff.

Permanent errors (invalid data, 4xx) are:
- Logged and skipped when `hard_fail=false`
- Fatal when `hard_fail=true`

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Run tests with coverage
pytest --cov=target_stripe --cov-report=term-missing

# Type checking
mypy target_stripe

# Linting
ruff check target_stripe tests
ruff format target_stripe tests
```

## Plan Code Mapping

Create a JSON file for plan code to price ID mappings:

```json
{
  "basic_monthly": "price_1ABC123",
  "pro_monthly": "price_2DEF456",
  "enterprise_annual": "price_3GHI789"
}
```

Then reference it in config:

```json
{
  "plan_code_mapping_file": "/path/to/plan_codes.json"
}
```

Or provide mappings directly:

```json
{
  "plan_code_to_price_id": {
    "basic_monthly": "price_1ABC123"
  }
}
```

## License

MIT License - see [LICENSE](LICENSE) for details.
