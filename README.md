# target-stripe

A Singer target for Stripe that upserts Customers and Subscriptions via the Stripe API.

Built with the [Meltano Singer SDK](https://sdk.meltano.com).

## Features

- **Upsert Stripe Customers** - Create or update customers with deduplication via metadata search
- **Upsert Stripe Subscriptions** - Create or update subscriptions with automatic customer resolution
- **Test Payment Methods** - Automatically attach test credit cards to customers in test mode for realistic testing
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
| `dry_run` | boolean | `false` | Validate data without writing to Stripe |
| `hard_fail` | boolean | `false` | Fail immediately on any record error |
| `idempotency.strategy` | string | `source_id` | Strategy for idempotency keys: `source_id` or `hash` |
| `customers.source_fields.customer_id` | string | `source_customer_id` | Top-level record column for customer source ID |
| `customers.source_fields.metadata` | array | see sample | List of `[record_field, stripe_metadata_key]` pairs (must include one pair for `customer_id`) |
| `customers.add_test_payment_methods` | boolean | `false` | Attach test payment methods (test mode only) |
| `customers.check_existing` | boolean | `true` | Search Stripe for existing customer by email when no local mapping |
| `customers.skip_mapped_records` | boolean | `false` | Skip customer rows already in local mapping DB |
| `customers.update_existing` | boolean | `false` | Update Stripe when customer found; if false, link only (counts as `skipped`) |
| `subscriptions.source_fields.subscription_id` | string | `source_subscription_id` | Column for subscription source ID |
| `subscriptions.source_fields.subscription_customer_id` | string | `null` | Column linking subscription row to customer |
| `subscriptions.source_fields.cancel_at_period_end` | string | `cancel_at_period_end` | Column for cancel-at-period-end flag |
| `subscriptions.source_fields.billing_cycle_anchor` | string | `null` | Column for renewal / anchor date |
| `subscriptions.source_fields.backdate_start` | string | `null` | Column for period start / backdate |
| `subscriptions.source_fields.proration_behavior` | string | `none` | Stripe proration when preserving billing cycle |
| `subscriptions.source_fields.metadata` | array | see sample | `[record_field, stripe_metadata_key]` pairs (must include `subscription_id`) |
| `subscriptions.plan_code_to_price_id` | object | `{}` | Plan code → Stripe price ID |
| `subscriptions.plan_code_mapping_file` | string | `null` | File merged into `plan_code_to_price_id` |
| `subscriptions.default_currency` | string | `usd` | Default currency (ISO 4217) |
| `subscriptions.past_due_handling` | string | `skip` | `skip` or `create_fresh` when anchor is in the past |
| `subscriptions.skip_mapped_records` | boolean | `false` | Skip subscription rows already in local mapping DB |
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
  "customers": {
    "source_fields": {
      "customer_id": "source_customer_id",
      "metadata": [
        ["source_customer_id", "source_customer_id"]
      ]
    },
    "add_test_payment_methods": false,
    "check_existing": true,
    "skip_mapped_records": false,
    "update_existing": false
  },
  "subscriptions": {
    "source_fields": {
      "subscription_id": "source_subscription_id",
      "metadata": [
        ["source_subscription_id", "source_subscription_id"]
      ]
    },
    "plan_code_to_price_id": {
      "basic_monthly": "price_1234567890",
      "pro_monthly": "price_0987654321",
      "enterprise_annual": "price_abcdefghij"
    },
    "default_currency": "usd",
    "past_due_handling": "skip",
    "skip_mapped_records": false
  },
  "rate_limit_per_sec": 25,
  "state_emit_interval": 100
}
```

### Configuring Source Fields and Metadata

Under `customers.source_fields` and `subscriptions.source_fields`, set **`customer_id`** / **`subscription_id`** to the source-system column name, and list **`metadata`** as pairs `[source_column, stripe_metadata_key]`. There must be exactly one pair whose first element equals that id column; its second element is where the target writes the source ID in Stripe metadata. Additional pairs copy other top-level columns into Stripe metadata.

**Example for Chargify migration:**

```json
{
  "customers": {
    "source_fields": {
      "customer_id": "chargify_customer_id",
      "metadata": [
        ["chargify_customer_id", "chargify_customer_id"],
        ["chargify_customer_ref", "chargify_customer_ref"]
      ]
    },
    "check_existing": false
  },
  "subscriptions": {
    "source_fields": {
      "subscription_id": "chargify_subscription_id",
      "subscription_customer_id": "chargify_customer_id",
      "cancel_at_period_end": "cancel_at_end_of_period",
      "billing_cycle_anchor": "current_period_ends_at",
      "backdate_start": "current_period_started_at",
      "proration_behavior": "none",
      "metadata": [
        ["chargify_subscription_id", "chargify_subscription_id"]
      ]
    },
    "past_due_handling": "skip"
  }
}
```

**For re-runs or continuous pipelines:**

```json
{
  "customers": {
    "source_fields": {
      "customer_id": "chargify_customer_id",
      "metadata": [["chargify_customer_id", "chargify_customer_id"]]
    },
    "check_existing": false,
    "skip_mapped_records": true
  },
  "subscriptions": {
    "source_fields": {
      "subscription_id": "chargify_subscription_id",
      "metadata": [["chargify_subscription_id", "chargify_subscription_id"]]
    },
    "skip_mapped_records": true
  }
}
```

**Different Stripe metadata keys than column names:**

```json
{
  "customers": {
    "source_fields": {
      "customer_id": "external_id",
      "metadata": [
        ["external_id", "legacy_customer_id"],
        ["salesforce_id", "salesforce_id"],
        ["hubspot_id", "hubspot_id"]
      ]
    }
  },
  "subscriptions": {
    "source_fields": {
      "subscription_id": "external_id",
      "metadata": [
        ["external_id", "legacy_subscription_id"]
      ]
    }
  }
}
```

### Billing Cycle Preservation

When migrating subscriptions from another billing system, you can preserve the original renewal dates to avoid double-charging customers who already paid for their current period.

**Enable preservation:** Set `subscriptions.source_fields.billing_cycle_anchor` to the column containing the renewal date in your source data.

**Behavior:**
- Subscriptions with future renewal dates are created with preserved billing cycles
- Past-due subscriptions are handled based on `subscriptions.past_due_handling` (`skip` or `create_fresh`)
- Uses `proration_behavior: "none"` to prevent charges for already-paid periods
- Sets `collection_method: "send_invoice"` for subscriptions without payment methods

**Example:**
```json
{
  "subscriptions": {
    "source_fields": {
      "billing_cycle_anchor": "current_period_ends_at",
      "backdate_start": "current_period_started_at",
      "proration_behavior": "none",
      "subscription_id": "source_subscription_id",
      "metadata": [["source_subscription_id", "source_subscription_id"]]
    },
    "past_due_handling": "skip"
  }
}
```

**Omit `billing_cycle_anchor`** to create fresh billing cycles starting from migration date.

### Test Payment Methods

When testing your integration with Stripe, you can automatically attach test payment methods to customers by enabling `customers.add_test_payment_methods`. This is only allowed in test mode for safety.

**Benefits:**
- Test subscriptions with realistic payment scenarios using `charge_automatically` collection method
- Avoid manual payment method setup for each test customer
- Automatically uses Stripe's recommended test card: `4242 4242 4242 4242` (Visa)

**Configuration:**
```json
{
  "stripe_api_key": "sk_test_YOUR_KEY",
  "stripe_mode": "test",
  "customers": {
    "source_fields": {
      "customer_id": "source_customer_id",
      "metadata": [["source_customer_id", "source_customer_id"]]
    },
    "add_test_payment_methods": true
  }
}
```

**Behavior:**
- When creating customers, a test credit card (4242 4242 4242 4242) is automatically attached as the default payment method
- Subscriptions are created with `collection_method: "charge_automatically"` instead of `send_invoice`
- Payment method attachment errors are logged but don't fail customer creation
- This feature is **only available in test mode** - attempting to enable it in live mode will raise a validation error

**Without test payment methods (default):**
- Subscriptions use `collection_method: "send_invoice"` with `days_until_due: 30`
- No payment methods are attached to customers
- Suitable for migrations where payment methods will be added separately

**Example configuration file:** See `config.test-payment-methods.json` for a complete example.

## Supported Streams

### customers

Creates or updates Stripe Customers.

**Input Fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `source_customer_id` | string | Yes* | Source customer ID (defaults; override via `customers.source_fields.customer_id`) |
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

*One of the source ID fields is required. The configured `customers.source_fields.customer_id` takes priority.

**Stripe Metadata Set:**
- Pairs in `customers.source_fields.metadata`: each `[record_field, stripe_metadata_key]` copies from the record when present; the pair for `customer_id` receives the stamped source ID.

### subscriptions

Creates or updates Stripe Subscriptions.

**Input Fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `source_subscription_id` | string | Yes* | Source subscription ID (defaults; override via `subscriptions.source_fields.subscription_id`) |
| `subscription_id` | string | Yes* | Alternative source ID field |
| `id` | string | Yes* | Alternative source ID field |
| `customer_id` | string | Yes | Customer reference (source or Stripe ID; configurable via `subscriptions.source_fields.subscription_customer_id`) |
| `price_id` | string | Yes** | Stripe price ID |
| `plan_code` | string | Yes** | Plan code (mapped to price_id) |
| `quantity` | integer | No | Subscription quantity |
| `trial_end` | string/int | No | Trial end timestamp or "now" |
| `coupon` | string | No | Coupon code to apply |
| `cancel_at_period_end` | boolean | No | Cancel at period end flag (column name from `subscriptions.source_fields.cancel_at_period_end`) |
| `current_period_ends_at` | string/int | No | Renewal date for billing cycle preservation (`subscriptions.source_fields.billing_cycle_anchor`) |
| `current_period_started_at` | string/int | No | Period start (`subscriptions.source_fields.backdate_start`) |
| `metadata` | object | No | Additional metadata |

*One of the source ID fields is required. The configured `subscriptions.source_fields.subscription_id` takes priority.
**Either `price_id` or `plan_code` is required.

**Stripe Metadata Set:**
- Pairs in `subscriptions.source_fields.metadata` (including the `subscription_id` pair for the stamped source ID).

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
        - name: customers
          kind: object
          description: Customer stream config (source_fields + add_test_payment_methods)
        - name: subscriptions
          kind: object
          description: Subscription stream config (source_fields, plan_code_to_price_id, default_currency, …)
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
1. **Local SQLite mapping** (source_id → stripe_id) - checked first
2. **Stripe ID lookup** - Direct retrieval if mapping exists
3. **Email search** (fallback if no mapping and `customers.check_existing=true`)

**Finding existing customers strategy:**
- If record exists in local DB → retrieve directly by Stripe ID
- Otherwise, search Stripe by email address when `customers.check_existing` is true (default)
- Email search helps find customers when starting with existing Stripe account

**When a customer is found** (`customers.update_existing`, default `false`):
- Store or refresh the local mapping only; no `Customer.modify`
- Batch stats count the row as **skipped** (not updated)
- Set `customers.update_existing: true` to push record fields to Stripe (previous default upsert behavior)

**Performance optimization:** Set `customers.check_existing=false` to skip email searches and only use local mapping DB. This reduces API calls by ~50% when you know records don't exist in Stripe yet.

### Handling Re-Runs and Continuous Pipelines

The target supports safe re-runs and continuous pipeline execution with per-stream `skip_mapped_records`:

```yaml
# Recommended configuration for re-runs
target-stripe:
  config:
    customers:
      check_existing: false
      skip_mapped_records: true
    subscriptions:
      skip_mapped_records: true
```

**When `skip_mapped_records` is true for a stream:**
- Records already in local mapping DB are skipped (no API calls)
- Only new/unmigrated records are processed
- Perfect for:
  - Resuming failed migrations
  - Re-running pipelines safely
  - Continuous/incremental syncs
  - Avoiding duplicate creation

**Example output:**
```
INFO Processing batch of 50 customer records
INFO Batch complete: 50 processed, 0 created, 0 updated, 50 skipped, 0 errors
```

**Behavior on environment mismatch:**
- If local DB has a customer but Stripe ID doesn't exist → **fails with clear error**
- Helps catch configuration issues (e.g., test DB with live API key)
- Prevents silent data corruption

### Idempotency

Operations use Stripe idempotency keys with unique run identifiers:
- `source_id` strategy: `{entity}:{operation}:{source_id}:{run_id}`
- `hash` strategy: `{entity}:{operation}:{source_id}:{data_hash}:{run_id}`

**Run ID Protection:** Each pipeline run generates a unique run ID, preventing idempotency conflicts when re-running within Stripe's 24-hour idempotency window.

**Idempotency error handling:**
- If Stripe returns an idempotency conflict → returns existing record as success
- Ensures safe re-runs even with key collisions
- Logs warnings for troubleshooting

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

Then reference it in config (merged into subscription pricing):

```json
{
  "subscriptions": {
    "plan_code_mapping_file": "/path/to/plan_codes.json"
  }
}
```

Or provide mappings directly:

```json
{
  "subscriptions": {
    "plan_code_to_price_id": {
      "basic_monthly": "price_1ABC123"
    }
  }
}
```

## License

MIT License - see [LICENSE](LICENSE) for details.
