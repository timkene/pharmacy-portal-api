# Pharmacy PA generation

New orders have server-created medication `lineId` values and require a procedure code, diagnosis code,
and positive quantity for every medication. Direct quotes and competitive bids submit one **line total**
per medication: the total price for all units in that medication line. `AmountRequested` is exactly
that line total; PA generation does not multiply by quantity. The API validates every positive
two-decimal price (maximum ₦100,000,000 per line) and calculates
the medication subtotal. Staff approvals and later adjustments retain explicit line prices;
the delivery fee remains separate. Legacy orders without line prices remain readable and can
complete, but they cannot generate a PA.

`POST /api/orders/{id}/generate-pa` requires a signed staff session, JSON body
`{"expectedVersion": <current order version>}`, and a completed order. The version is checked
in the atomic claim; a stale page receives 409 before any IssuePa POST. Staff must review the
current version and all line totals in the confirmation dialog for every attempt, including retries.
It uses the winning aggregator record's `providerId`. Each medication causes one legacy
IssuePa POST. Delivery causes a separate PRE11 POST at the stored delivery fee. The client
uses the working CDR field names and leaves `AdditionalServices` empty.
All three PA date fields use the calendar date in `Africa/Lagos` when PA generation runs.
`MEDICLOUD_LEGACY_URL` and credentials must be explicitly configured; there is no production
URL default. `GET /member` uses HTTP Basic authentication. This follows the latest explicit
committed repository contract in commit `063f1f8804522b4e566fc063dae15d754d2b59b4`.
Repository evidence contains no staging or sandbox integration endpoint, so no live verification
was performed. Controlled integration verification remains desirable before broad rollout; the
implementation follows the strongest committed contract evidence currently available. The delivery
PRE11 diagnosis requires an explicit `PA_DELIVERY_DIAGNOSIS_CODE`.
No authoritative delivery diagnosis convention was found in repository contracts.

PA lines move from `pending` to `submitting` to `generated`, `failed_retryable`, or
`verification_required`. A failed connection establishment is retryable. A timeout, HTTP
error, invalid response, or interrupted submission is uncertain. Ordinary generation skips
generated lines and stops when any line requires verification. An atomic order claim prevents
concurrent submissions. The order audit records safe line and staff details, never the full
request or upstream response.
The existing float storage is retained for compatibility with historical orders; validated
two-decimal Decimal input is checked for cent preservation before conversion. Existing historical
delivered orders with a zero fee remain readable. New delivery fulfilments require a positive fee.

For an uncertain line, an authorized staff member must verify the outcome in the
intermediary PA register. The signed staff recovery endpoint is
`POST /api/orders/{id}/pa-lines/{lineId}/verify` with `resolution` set to `existing_pa`
(and a conservatively formatted `paNumber`) or `confirmed_no_pa`, plus an evidence note. For
`confirmed_no_pa`, the request must also include `confirmNoPaCreated: true`,
`verificationMethod`, `checkedWith`, and timezone-aware `verifiedAt`. Only staff IDs in the
server-side `PA_RECOVERY_STAFF_IDS` comma-separated allowlist can use it. It records the
verification and **does not submit** a PA. `confirmed_no_pa` makes only that line safe to retry.
For an expired submitting lease, staff see an interrupted state. An authorized recovery staff
member calls `POST /api/orders/{id}/pa-interruption/mark-verification-required` using the UI.
This rotates claim ownership, audits the transition, and never submits a PA. The operator then
uses the verification form above.

## Controlled Test pharmacy 1 provider mapping

The script `scripts/set_test_pharmacy_provider.py` requires a verified aggregator ObjectId,
checks `companyName: "Test pharmacy 1"`, refuses conflicting provider IDs, and maps it to `4304`.
It defaults to a read-only dry run. A deployment operator must verify `DB_NAME` and
`MONGO_URI` in their approved environment, review the dry-run record ID, and separately
authorize an `--apply` run. This implementation does not run the script against production.

```bash
python scripts/set_test_pharmacy_provider.py --db-name <verified-db-name> --aggregator-id <verified-object-id>
python scripts/set_test_pharmacy_provider.py --db-name <verified-db-name> --aggregator-id <verified-object-id> --apply
```
