# NetHub M-Pesa Gateway Worker

**Status:** Architecture draft / implementation baseline  
**Last updated:** 2026-08-22  
**Runtime:** Cloudflare Python Workers (Pyodide)  
**Entrypoint:** `src/entry.py`  
**Core logic:** `src/worker.py`

---

## 1. Purpose

This Worker is the **edge ingestion layer** for M-Pesa (Daraja) callbacks.

It is deliberately thin. Its only jobs are:

1. Accept callbacks from Safaricom on well-defined routes
2. Identify the registered integration via an opaque ID
3. Wrap the original payload in a normalized event envelope
4. Queue the envelope for asynchronous processing
5. Return the correct HTTP response expected by Safaricom

**It does not:**
- Perform payment business logic
- Validate the raw M-Pesa payload
- Enforce financial idempotency (that belongs to FastAPI + PostgreSQL)
- Make accept/reject decisions beyond the current safe default for C2B Validation

---

## 2. High-Level Flow

```
M-Pesa / Daraja
      │
      ▼
gateway.nethub.co.ke
      │
      ▼
Python Cloudflare Worker
  ├── Route + basic checks
  ├── Build normalized envelope
  ├── Validate envelope (our schema only)
  └── Queue  ──────────────────►  Cloudflare Queue (mpesa-callbacks)
                                      │
                                      ▼
                               (future) Consumer → FastAPI → PostgreSQL
```

---

## 3. Supported Routes

All routes follow the pattern:

```
POST /mpesa/cb/{integration_id}/{event_type}
```

| Path segment     | Canonical `event_type` | Immediate response? | Notes |
|------------------|------------------------|---------------------|-------|
| `validation`     | `c2b_validation`       | **Yes**             | Must return Safaricom-shaped JSON. Currently always accepts. |
| `confirmation`   | `c2b_confirmation`     | No (202 + queue)    | C2B payment completed |
| `stk`            | `stk_callback`         | No (202 + queue)    | STK Push / Lipa Na M-Pesa Online result |
| `b2c-result`     | `b2c_result`           | No (202 + queue)    | B2C payout result |
| `b2c-timeout`    | `b2c_timeout`          | No (202 + queue)    | B2C queue timeout |

### Integration ID rules
- Must be present
- Must start with `gw_` (soft validation)
- Example: `gw_7xK92mPq`

### Example full URLs
```
https://gateway.nethub.co.ke/mpesa/cb/gw_7xK92mPq/validation
https://gateway.nethub.co.ke/mpesa/cb/gw_7xK92mPq/confirmation
https://gateway.nethub.co.ke/mpesa/cb/gw_7xK92mPq/stk
https://gateway.nethub.co.ke/mpesa/cb/gw_7xK92mPq/b2c-result
https://gateway.nethub.co.ke/mpesa/cb/gw_7xK92mPq/b2c-timeout
```

---

## 4. Response Behaviour

### C2B Validation (special case)
Safaricom expects an **immediate synchronous decision**.

Current behaviour (safe default):
```json
HTTP 200
{
  "ResultCode": "0",
  "ResultDesc": "Accepted"
}
```

A copy of the event is still written to the queue for audit / later processing.  
A queue failure does **not** prevent the 200 response from being returned.

### All other routes
```
HTTP 202
Queued successfully ({event_type}) for {integration_id}
```

The normalized envelope is sent to the Cloudflare Queue.

### Error responses
| Condition                        | Status | Body |
|----------------------------------|--------|------|
| Non-POST method                  | 405    | Method Not Allowed |
| Unknown path / event type        | 404    | Not Found |
| Integration ID missing or bad    | 400    | Invalid integration identifier |
| Envelope construction failure    | 500    | Internal envelope error: … |
| Queue send failure (non-validation) | 500 | Failed to queue callback |

---

## 5. Normalized Event Envelope

Every queued message has this shape:

```json
{
  "event_id": "evt_a1b2c3d4e5f6...",
  "provider": "mpesa",
  "event_type": "c2b_confirmation",
  "integration": {
    "id": "gw_7xK92mPq",
    "type": "unknown"
  },
  "received_at": "2026-08-22T14:30:00.123456Z",
  "request": {
    "method": "POST",
    "path": "/mpesa/cb/gw_7xK92mPq/confirmation"
  },
  "payload": { ... original Safaricom body ... }
}
```

### Field notes
- `event_id` – gateway-generated unique ID for this delivery
- `provider` – always `"mpesa"`
- `event_type` – one of the canonical values listed above
- `integration.id` – opaque routing ID from the URL
- `integration.type` – placeholder (`"unknown"`). Will be filled from the registry later
- `received_at` – UTC ISO-8601 timestamp
- `request` – method + path as seen by the Worker
- `payload` – **exact** original body. If the body was valid JSON it is stored as an object; otherwise as a string. We never mutate or validate it.

---

## 6. Design Principles (enforced)

| Principle | How it is applied |
|-----------|-------------------|
| Edge is an ingestion boundary | No payment business logic, no ledger writes, no financial decisions |
| Provider payload is preserved | Original body is nested under `payload` unchanged |
| At-least-once is expected | Backend must tolerate duplicate deliveries |
| Database is the idempotency authority | Worker does not attempt to deduplicate |
| Validation is layered | Worker only checks route + envelope shape |
| One Worker, multiple routes | All callback types share the same Worker |

---

## 7. File Structure

```
mpesaedge/
├── src/
│   ├── entry.py          # Thin Cloudflare entrypoint (Default class)
│   ├── worker.py         # CallbackRouter, EnvelopeBuilder, schema, validation
│   └── submodule.py      # Existing file – leave untouched
├── wrangler.jsonc
├── pyproject.toml
└── ...
```

### Responsibilities
- `entry.py` – orchestration only. Calls the router, builds the envelope, decides whether to return the Validation JSON or queue + 202.
- `worker.py` – all pure logic (routing, envelope construction, schema validation).

---

## 8. Current Limitations / Explicit Non-Goals

- No lookup of the integration registry yet (`integration.type` stays `"unknown"`)
- C2B Validation always accepts (no real-time business rules)
- No extraction of `provider_transaction_id` into a top-level field yet
- No retry / dead-letter handling inside the Worker
- No request signature / IP verification yet
- No rate limiting beyond what Cloudflare provides by default

These will be addressed in later incremental steps.

---

## 9. Testing

### Happy-path curls

```bash
# C2B Validation – must return Safaricom JSON
curl -i -X POST https://gateway.nethub.co.ke/mpesa/cb/gw_7xK92mPq/validation \
  -H "Content-Type: application/json" \
  -d '{"TransID":"TESTVAL1","TransAmount":"100.00"}'

# C2B Confirmation
curl -i -X POST https://gateway.nethub.co.ke/mpesa/cb/gw_7xK92mPq/confirmation \
  -H "Content-Type: application/json" \
  -d '{"TransID":"TESTCONF1","TransAmount":"150.00"}'

# STK Push callback
curl -i -X POST https://gateway.nethub.co.ke/mpesa/cb/gw_7xK92mPq/stk \
  -H "Content-Type: application/json" \
  -d '{"Body":{"stkCallback":{"CheckoutRequestID":"ws_CO_123","ResultCode":0}}}'

# B2C Result
curl -i -X POST https://gateway.nethub.co.ke/mpesa/cb/gw_7xK92mPq/b2c-result \
  -H "Content-Type: application/json" \
  -d '{"Result":{"ResultCode":0,"ResultDesc":"Success"}}'

# B2C Timeout
curl -i -X POST https://gateway.nethub.co.ke/mpesa/cb/gw_7xK92mPq/b2c-timeout \
  -H "Content-Type: application/json" \
  -d '{"Result":{"ResultCode":1,"ResultDesc":"Timeout"}}'
```

### Negative cases
```bash
# Wrong method
curl -i -X GET https://gateway.nethub.co.ke/mpesa/cb/gw_7xK92mPq/confirmation

# Unknown event type
curl -i -X POST https://gateway.nethub.co.ke/mpesa/cb/gw_7xK92mPq/unknown \
  -H "Content-Type: application/json" -d '{}'

# Bad integration ID
curl -i -X POST https://gateway.nethub.co.ke/mpesa/cb/badid123/confirmation \
  -H "Content-Type: application/json" -d '{}'
```

---

## 10. Next Planned Steps

1. Observe real messages on the Cloudflare Queue
2. Build the Queue consumer
3. Introduce FastAPI gateway + integration registry
4. Persist events/payments with database-enforced idempotency
5. Add production security (source verification, request limits, etc.)
6. Make C2B Validation decision data-driven (instead of always Accept)

---

## 11. Related Documents

- `Nethub_Mpesa_Gateway_Architecture_Spec.pdf` – full architecture specification
- `progress.md` – living implementation tracker

---

## 12. NetPay secure integration

Safaricom never talks to NetPay directly. Flow:

1. Daraja → **mpesa-edge** (`/mpesa/cb/{gw_*}/…`)
2. Edge builds envelope → **Cloudflare Queue** `mpesa-callbacks`
3. Same Worker **queue** handler → `POST {NETPAY_BASE_URL}/internal/events`
4. Header: `X-Internal-Api-Key: {NETPAY_INTERNAL_API_KEY}`
5. NetPay durable ingest (`inbound_events`) + payment state / ledger

### Required secrets (Cloudflare)

```bash
npx wrangler secret put NETPAY_BASE_URL
# value example: https://api.your-netpay-host.example  (no trailing slash)

npx wrangler secret put NETPAY_INTERNAL_API_KEY
# MUST equal NetPay env INTERNAL_API_KEY
```

Create the DLQ once (if not exists):

```bash
npx wrangler queues create mpesa-callbacks
npx wrangler queues create mpesa-callbacks-dlq
```

### NetPay side

- Set a long random `INTERNAL_API_KEY` (secrets manager / k8s secret).
- Do not expose `/internal/*` without that key.
- Prefer network isolation so only Cloudflare (or your worker egress) can reach ingest.
- Integration `public_id` values must match URL `{gw_*}` ids registered with Daraja.

### Local dev

Use `.dev.vars` (gitignored):

```
NETPAY_BASE_URL=http://127.0.0.1:8000
NETPAY_INTERNAL_API_KEY=unit-test-internal-api-key
```

### Failure behaviour

| Outcome | Behaviour |
|---------|-----------|
| NetPay 2xx | Message acknowledged |
| NetPay 5xx / 429 | Queue retries (up to `max_retries`) then **mpesa-callbacks-dlq** |
| NetPay 401 | Retry until key fixed (check secrets) |
| Missing secrets | Consumer errors until configured |

Financial idempotency remains on NetPay (`event_id` + payment state machine + ledger).

### Live test (edge → NetPay)

After secrets are set:

```bash
# Manual ping (header must match EDGE_ADMIN_KEY or NETPAY_INTERNAL_API_KEY)
curl -sS -X POST "https://<your-worker>/__netpay/ping" \
  -H "X-Edge-Admin-Key: $NETPAY_INTERNAL_API_KEY"
```

Cron runs **every 5 minutes** (`edge.heartbeat` → NetPay). In NetPay **System status**, check **Last heartbeat**.
