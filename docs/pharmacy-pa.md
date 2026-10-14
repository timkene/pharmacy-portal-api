# Pharmacy PA generation

The confirmed IssuePa contract uses one POST per PA group. A single service omits
`AdditionalServices` entirely. For multiple services, the primary eligible medication
occupies the top-level fields and remaining medications appear in `AdditionalServices`.
Delivery PRE11 is another additional service at the explicit delivery fee; pickup omits it.
Additional entries include ProcedureCode, DiagnosisCode, ProcedureDesc, DiagnosisDesc,
Quantity, AmountRequested and AmountGranted. AmountGranted equals the approved line
AmountRequested. Line totals are never multiplied by quantity again.

## Lookup and configuration

The backend calls `GET {MEDICLOUD_API_URL}/enrollees/lookup?enrollee_id=...` with
`X-API-Key` from `MEDICLOUD_API_KEY`. The response must identify exactly one matching
member in `results`, with `found: true`, `count: 1`, no truncation and matching legacycode.
Its nonempty string `group_legacy_id` comes from member.groupid joined to group.groupid,
then group.legacyid. Both IssuePa GroupId and DivisionID use that string. Missing LegacyID
blocks submission; numeric raw group/division identifiers are never a fallback.
The lookup returns real dbo.member columns discovered from INFORMATION_SCHEMA,
including dependantid; it never fabricates dependantnumber. Pharmacy uses
DependantNumber "0" only for an explicit principal. An explicit dependant requires
a valid positive dependantid, normalized to a decimal string. Older number aliases
are checked only for conflicts, never used instead of dependantid.

## Confirmed from existing live requests

User-provided production evidence records 191 successful submissions to
`https://clearlinehmoapp.com/intermediary/IssuePa`, using `username` and `password`
headers, without HTTP Basic. Credentials come from MEDICLOUD_LEGACY_USER /
MEDICLOUD_LEGACY_PASS (existing CLEARLINE_APP_* fallback remains). Authentication uses the configured username/password as HTTP headers only.
The IssuePa body uses UserID = "admin" and UserId = "admin", independently
of the configured authentication username.

MEDICLOUD_LEGACY_URL remains configurable. The unchanged default is the staging
candidate `https://medicloud.dev/interm`; production evidence does not verify that
staging endpoint or its authentication. No environment is changed, and staging is
never silently redirected to production.

The confirmed member source has isprincipal, dependantid, isterminated, isdeleted,
and groupid; dbo.member.dependantnumber does not exist. Twenty-eight recorded
successful dependant PAs used nonzero dependantid, including 1 and 3. GroupId and
DivisionID use the joined group legacy ID. Confirmed request structure includes
both body user fields fixed to "admin", Isbooked false, additional medications in AdditionalServices,
and AmountGranted equal to AmountRequested there, with no top-level AmountGranted.
Single-service requests continue to omit AdditionalServices.

Booking key is `Isbooked` (not `isbooked`). Referral is `"none"`. Generation uses
Africa/Lagos for `CompletionDate`, `PAStatusDate`, and `Proceduredate` (the office
sample may use different calendar days for completion vs status). Both newborn
keys stay false. Delivery diagnosis requires `PA_DELIVERY_DIAGNOSIS_CODE`.

## Ownership, outcomes and evidence

Generate PA requires signed staff authentication, a completed order, explicit validated
prices/provider and the current expectedVersion. Existing input drift, active claims,
submitting/uncertain lines and explicit rejected services block resubmission. Generated
lines are skipped. Eligible services share one submissionGroupId and submittedAt, and all
are written ahead as submitting in a single guarded write before the one upstream POST.
Each group has an entry in paGeneration.attempts, preserving included line IDs, timestamp,
reference and response evidence. Previous legacy line states are retained; they are not
silently migrated or retried. RX-20260928-EFEB must not be used as a staging fixture.

A shared PA reference is NOT proof that every service was accepted. The exact live
per-service response schema remains undocumented. The defensive parser recognizes only
explicit uniquely matched ProcedureCode entries inside Services / AdditionalServices,
with Status or PAStatus on nested service records. No top-level object can establish
per-service acceptance. These are conservative adapters, not a claim
that MediCloud guarantees those response envelopes. Conflicting statuses, duplicate
procedure codes, mismatched supplied diagnosis/quantity/amount, missing references on
accepted outcomes and unrecognized/unmapped outcomes become verification_required.
ACCEPTED/APPROVED/AUTHORIZED service evidence plus a valid shared reference can mark a
line generated. REJECTED/DENIED/FAILED evidence marks failed_rejected, which is blocked
from automatic resubmission pending manual review. One bare reference or order-level
status never marks any service successful, including a single service.

All transport errors, timeouts, malformed JSON and HTTP errors conservatively move every
included service to verification_required. No automatic retry occurs. A valid reference in
an error body may be retained without accepting any service. HTTP status, a SHA256 body
fingerprint and up to 8192 characters of credential-redacted raw response are stored in the
private attempt history. This evidence may contain upstream personal data and is not
included in public PA state, order history events or application logs. Request payloads and
credentials are not persisted. Audit events retain staff identity and independent line state.

Recovery requires a signed staff user in the comma-separated PA_RECOVERY_STAFF_IDS
allowlist. POST /api/orders/{id}/pa-lines/{lineId}/verify records per-service authoritative
verification; it never issues a PA. existing_pa requires a verified reference and evidence.
A reference may be shared only among lines with the same submissionGroupId; a conflicting
known group reference is rejected. confirmed_no_pa requires evidence, confirmNoPaCreated,
verificationMethod, checkedWith and timezone-aware verifiedAt. It moves only an uncertain
line to failed_retryable, and requires additional authoritative absence attestation for every potentially transmitted
attempt, irrespective of status or transport outcome (see review corrections below).

Expired submitting claims require the allowlisted interruption endpoint to mark all
submitting services uncertain. Ownership/version guards remain in place. Manual verified
outcomes and later generation are separate, explicitly authorized operations. A standalone retry consisting only of delivery PRE11 is blocked; resolve it within the
medication PA. Rejected
services currently have no automatic retry/recovery action; they require manual review.

## Validation boundary

Tests use mocked HTTP and forbid live sockets. Actual staging per-service response samples
must still be reviewed before relying on automatic outcome mapping. Unknown response
schemas remain verification_required rather than fabricated success. No deployment,
configuration mutation or MediCloud request is part of this implementation change.


## Independent review corrections

The normalized member must have an authoritative positive memberid, matching legacycode,
nonempty string group_legacy_id and explicitly false isterminated/isdeleted. Missing or
ambiguous flags are ineligible. dependantid is authoritative; inconsistent
legacy aliases, invalid IDs and contradictory principal/dependant values block submission.
Only a confirmed principal may use 0 when the number is missing. No numeric group fallback.
Material member identity and order identity are stored with attempts and compared on retry.
Legacy attempts without that snapshot require manual reconciliation before resubmission.

Only nested explicit per-service entries may establish acceptance. Top-level Status,
PAStatus and request echoes NEVER approve a service, even for single-line groups.
Hypothetical Services/AdditionalServices adapters remain provisional, not a verified schema.
Reference keys are limited to PANumber, PaNumber, panumber and pa_number; generic ID,
AdmissionCode and unconfirmed PAReference are not trusted. AmountGranted, when supplied,
must normalize to exactly the requested Decimal amount; ambiguous/different values require
verification. groupPaNumber retains a shared reference privately; only generated services
expose paNumber as approved. Verification responses omit internal group ownership fields.

The existing allowlisted verify endpoint accepts failed_rejected as well as uncertain lines.
existing_pa records authoritative individual acceptance, including PRE11, and may share a
reference in the same submission group. confirmed_rejected records an audited rejection
without enabling retry. confirmed_no_pa still requires all existing evidence fields. For
every potentially transmitted attempt it additionally requires
confirmAuthoritativeAbsence=true, reconciliationReference (an authoritative investigation
record/source reference), and verifiedAt at or after the original attempt. Missing submittedAt blocks automated
absence resolution and resubmission, including legacy/interrupted attempts. Parser nonrecognition
is not sufficient evidence of absence. A rejected service may enter failed_retryable only
through this explicit authoritative absence path. All remaining uncertain/rejected lines
still block group submission. PRE11-only resubmission intentionally remains manual: use
existing_pa/confirmed_rejected reconciliation within the medication PA, not a new delivery PA.

Cross-order manual attachment checks lines and attempt references before updating. Same
reference in another order is rejected. Atomic pa_reference_owners reservations keyed by
reference additionally prevent simultaneous manual attachment across different orders or
submission groups. The collection uses built-in _id uniqueness; no additional index is
required. If the order version changes after reservation, that reservation is intentionally
retained and may require manual investigation before reuse, rather than silently released.

General enrollee lookup stays backward compatible and may return NIN/phone/email. Pharmacy
keeps only normalized identity and flags and never persists/logs the raw lookup row. A narrow
PA-specific response is recommended as a follow-up to reduce transmitted data; no unrelated
lookup redesign is included here.


## Remaining read-only staging validation

isprincipal must explicitly establish principal/dependant identity; a missing/null/unknown
flag blocks PA even when dependantnumber is 0. Only a confirmed principal can use 0;
a confirmed dependant requires a valid positive number. Conflicting aliases block.
NULL isdeleted remains ineligible/ambiguous for Pharmacy; SQL ISNULL patterns elsewhere
are not sufficient to establish PA eligibility semantics. Validate against actual staging data.
Terminated/deleted checks do not establish current coverage. Confirm whether Pharmacy
must enforce coverage or IssuePa authoritatively enforces it before operational validation.
A separately authorized read-only lookup of the known staging member must establish the
actual member column keys, including dependantid, and principal/dependant semantics.
No such lookup was performed during this correction pass.


## Still unconfirmed: successful response contract

No raw successful response JSON has been saved. The old sender gated success on
HTTP 200 and case-insensitive status=success, which supports group-level evidence
only. This change does not approve services based on that gate. Its extraction
candidates (response, PANumber, PaNumber) do not establish which property actually
produced the recorded references; arbitrary response content is never a PA number.
Exact successful response JSON, actual PA-reference property, response Content-Type,
and service-level / partial-outcome structure still require controlled capture.
Services / AdditionalServices response adapters remain hypothetical, conservative
adapters rather than confirmed MediCloud semantics. Unknown or group-only success
remains verification_required. Existing authoritative-absence reconciliation and
no-automatic-retry protections are unchanged.

## Phase 1: internal preparation persistence

Phase 1 adds internal services only; no preparation, catalog, mapping-import,
export or frontend routes are exposed. `core.pa_preparation` reads orders and
appends preparation evidence. `core.pa_mapping` accepts internally attested
procedure extracts and human-reviewed mapping inputs. Neither module sends
IssuePa. Existing `orders.paGeneration` and `pa_reference_owners` remain the
only transmission/reference authority and are never written by these services.

The core collections are `pa_preparation_batches`, `pa_preparation_lines`,
`pa_mapping_versions`, `pa_mapping_heads`, `pa_procedure_catalog_snapshots`,
`pa_procedure_catalog_entries`, and `pa_preparation_events`. Review adds
`pa_catalog_validation_evidence` and `pa_preparation_contexts`. Index creation is
additive/idempotent at database startup. Phase 1 publication requires a
transaction-capable MongoDB replica set or sharded cluster; there is no unsafe
nontransactional fallback. No database migration or reference-data population
is executed by this implementation task.

Internal `Actor` permissions must come from authenticated server authorization;
never construct them from uploaded rows. Preparation, proposal, catalog ingestion,
catalog validation, catalog trust and mapping review are explicit capabilities.
Mapping review alone does not grant catalog validation/trust authority. `MemberEvidence` must be constructed
by trusted lookup-validation code through the minimized adapter, not accepted
from clients. Its validity interval and source evidence reference are required.
Member evidence validity is capped by `MEMBER_EVIDENCE_MAX_AGE` (one hour).
Material member identity/eligibility enters the preparation fingerprint; retrieval
IDs and timestamps do not. Fresh equivalent evidence reuses the preparation
revision, while its evidence ID/validity times are persisted in a separate audit
event. Historical preparation evidence is not overwritten.
An enrollee fingerprint is restricted integrity evidence, not anonymization.
No member name, contact, DOB, address or raw enrollee identifier is duplicated.

Procedure extracts use only MediCloud `dbo.proceduredata` canonical codes and
descriptions. The internal attestation includes extraction time, source evidence
reference, record count and content hash. `catalog_hash` hashes sorted exact
code/description pairs as compact UTF-8 JSON; extraction tools must use this
contract. Any normalized duplicate (including identical duplicates), missing
real description, code-as-description fallback, count/hash mismatch or invalid
extraction metadata produces an INVALID snapshot with no usable entries.
Matching a caller-supplied hash/count establishes structural validity only.

Catalog ingestion publishes an INGESTING header, then commits bounded chunks of
at most 1,000 entries in separate transactions. Only complete ingestion becomes
VALIDATED. Failed/partial ingestion stays unusable; no giant insertion transaction
is required. Prior TRUSTED snapshots and mapping evidence remain intact.

TRUSTED promotion is a separate privileged action, requiring registered
`CatalogValidationEvidence` from a validator context with the
SERVER_CONTROLLED_MEDICLOUD_EXTRACTION method, source/extraction metadata,
content hash, record count, schema version, validator identity/time and evidence
ID. Promotion checks persisted completeness/hash and binds the validation record.
A generic mapping reviewer or uploader cannot perform this action. Phase 1 has
no actual extraction validator or route: Phase 4 must establish server-controlled
provenance before issuing this evidence and define snapshot freshness. Tests use
an explicitly privileged fixture. Separate capabilities do not mandate different
people; no two-person approval policy is introduced.

Mapping versions are append-only. The unique namespace/type/key head records
both the latest revision and active revision. ACTIVE cannot become REJECTED. Only never-activated PROPOSED/VALIDATED
candidates can be rejected. An explicit replacement proposal clears prior
canonical validation fields and preserves the active head until a valid
replacement activation; RETIRED releases it. Returned revision state explicitly
reports `activeVersion` and `isActive`.
Activation requires trusted canonical evidence and explicit matching human
reviewer evidence. AI/fuzzy confidence alone cannot activate. Canonical target
description comes from the catalog. Description identities use conservative
trim/casefold comparison; source code identities use trim/uppercase. No fuzzy
identity equivalence is inferred.

Preparation retains approved/final line totals as exact two-decimal strings,
without multiplying by quantity or replacing prices. Legacy aggregate quotes
are not approved line-price evidence. Source namespace is explicit: an existing
order without one requires review rather than guessing code ownership.
Referring provider, aggregator and configured IssuePa provider remain distinct.
Provider validation here establishes configured identity only; tariff eligibility
and tariff price comparison are explicitly NOT_IMPLEMENTED.

Reserved delivery PRE11 is prepared separately using the existing delivery fee
and `PA_DELIVERY_DIAGNOSIS_CODE`. It requires a trusted canonical PRE11 entry
from an explicitly supplied snapshot or the single common medication mapping
snapshot. Medication mappings cannot make PRE11 eligible as a medication.

Preparation states are READY, REVIEW_REQUIRED and BLOCKED. `submissionEligible`
is advisory as-of calculation, requires a completed order and all services ready,
and does not grant IssuePa or retry permission. Any existing attempt/transmitted
line blocks preparation eligibility for controlled submission/recovery handling.
Actual transmission integration is deferred to Phase 6; the existing PA API is
unchanged and is not gated by Phase 1 preparation yet.

An order has one active preparation context across OPEN/AUDITING batches.
`pa_preparation_contexts._id` is the canonical order ID and supplies MongoDB
uniqueness; its batch lookup is indexed. Claiming ownership and publishing lines
commit together. `close_batch` atomically closes the batch and releases ownership
without changing historical line evidence. Later batches can then claim it.
All batch closing must use this domain operation rather than raw status updates.

Batch version compare-and-set serializes recalculation. Revisions retain immutable
evidence; only `isCurrent`/`staleReason` publication metadata changes. Identical
inputs reuse a revision. Mapping activation/retirement invalidates current
preparations. Order/member/provider changes and released batch ownership are detected by `current_lines`;
future team APIs must use that accessor rather than trusting raw `isCurrent`
flags or last-calculated batch counts. Counts are as-of recalculation. Future
submission must independently revalidate source versions, member freshness and
all existing ledger/reconciliation safeguards immediately before claiming.

Audit events are separate append-oriented documents, using Mongo `_id` as event
identity and bounded allowlisted references. Publication, version heads and
related audit events commit together. The mocked transaction tests verify CAS,
rollback and unique-index contracts. Duplicate-key, write-conflict and Mongo
transient/unknown-commit labels become `DomainConflict` without automatic retry.
For an unknown commit result, inspect persisted state before repeating an action.
No external side effects are retried. Required index creation fails closed with
collection/index context if a safety index fails. Real replica-set transaction
validation remains a production prerequisite; mocks do not prove server behavior.

## Phase 2: staff preparation APIs

The application registers seven operations under `/api/pa-preparation`:

- `POST /batches` (`businessDate`)
- `GET /batches`
- `GET /batches/{batch_id}`
- `GET /batches/{batch_id}/lines`
- `POST /batches/{batch_id}/orders/{order_id}/prepare`
- `POST /batches/{batch_id}/orders/{order_id}/recalculate`
- `POST /batches/{batch_id}/close`

Mutation requests except creation require the current batch `expectedVersion`.
There is no IssuePa, recovery, export, import, catalog or mapping-activation
operation in this router. Existing transmission routes remain unchanged.

Every operation requires a valid signed staff session and an existing staff
record. Authorization reads `staff_users.permissions` on each request:
`PA_PREPARATION_READ` permits reads; `PA_PREPARATION_WRITE` permits mutations.
Neither capability is inferred from staff status, request JSON or signed cookie
permission claims. Absent permissions deny access. Assigning these permissions
requires separately authorized administration; this implementation does not
modify staff records or expose a permission-management route. A writer without
READ cannot receive another batch's ownership identifier in conflict responses.
No catalog/mapping privilege is granted to the domain Actor by these routes.

The existing `get_member_info` interface returns only three normalized identity
fields, not the complete Phase 1 MemberEvidence contract. Phase 2 therefore
fails closed with `MEMBER_VALIDATION_UNAVAILABLE` and BLOCKED preparation until
a trusted server-side full-evidence adapter is integrated through the
`member_validation_source` dependency. The default adapter performs no network
call. Client JSON cannot provide member evidence. The Mac is not a runtime
dependency. Tests override this dependency with minimized authoritative fixtures.
Current freshness timestamps come only from the evidence used for the current
request/recalculation, never from an old preparation row's receipt.

Missing source namespaces remain unmapped/review-required without a global
fallback. Other blocking validations take precedence in the overall status.
Malformed legacy medication codes/names and assignment identifiers fail closed
without repairing source orders. Line totals remain snapshots of existing
approved/final values, with no quantity multiplication or tariff comparison.

Line responses are allowlisted and contain IDs/revisions, provider/aggregator
context, medication/price snapshots, mapping lifecycle and validation states,
reason codes, stale status and advisory eligibility. They omit member identities,
PHI, hashes, raw upstream evidence, credentials and audit payloads. Transmission
projection reads only line status and a confirmed generated reference from the
canonical `orders.paGeneration`; it never changes transmission or retry state.
A stale row cannot be presented as READY or submission eligible. Source snapshot
values retain `calculatedAt`; current validation is evaluated independently.

Batch lists use `_id` cursors, with limits 1–100. Line pages use UUID `_id`
cursors and limits 1–50. A request scans at most 100 stored candidates; filters
are evaluated against safe current projections. An empty filtered page can have
a next cursor and must be followed. Historical superseded revisions are skipped;
latest stale/invalidated rows remain visible. Supported filters are `status`,
`mappingStatus`, `aggregatorId`, `providerId` (IssuePa provider), and `stale`.
A batch/id index supports scans. Within a page, each order is evaluated once.

Batch counts are explicitly LAST_CALCULATION with `countsAsOf`; they are not
live eligibility evidence. Submitted, verification and stale totals are null
when not calculated, rather than invented. `pageCounts` provides exact counts
for returned rows without an unbounded batch aggregation. Future frontend code
must distinguish page counts from batch totals.

Storage errors produce sanitized 503 responses, domain/ownership/version
conflicts produce 409, absent resources 404, and authentication/authorization
failures 401/403. Invalid request bodies/parameters return sanitized 422 errors
without echoing unknown evidence or credential fields. Responses use no-store
caching. No real MongoDB/MediCloud validation or deployment occurred in Phase 2.


Phase 2 review clarifications: WRITE permission permits a mutation and its minimized result response; it does not grant access to standalone GET/list endpoints, which require READ. Mapping status and target fields reflect the current line's effective validation, not the existence of an active mapping head. REVIEW_REQUIRED is a preparation status, not a mapping filter. Invalid or inconsistent PA ledgers project UNKNOWN with verification required and no reference, without repair. Invalid stored preparation rows are isolated as BLOCKED/PREPARATION_ROW_INVALID with minimized identifiers, preserving other page rows.

Deferred before production volume/frontend work: sequential per-row query performance and random-ID pagination/date ordering. Catalog ingestion cleanup and single-order ownership release remain deferred.

### Phase 3A — mapping review transfer (backend only)

The normal backend stores bounded review manifests and proposed versions in MongoDB. No Mac or live MediCloud connection is needed. This phase does not validate procedure existence, promote catalog trust, activate mappings, export member information, change pricing, or submit/retry PAs. Suggested descriptions remain suggestions; canonical descriptions can only come from the separately trusted catalog in a later authorized review.

Staff permissions are independent, default-deny, and read from the authoritative staff record:

- `PA_MAPPING_EXPORT`: create/download mapping review exports.
- `PA_MAPPING_IMPORT`: import proposals/read import status.

Existing preparation permissions and its seven routes are unchanged. New routes are:

- `POST /api/pa-mapping-review/batches/{batch_id}/exports`: JSON `expectedVersion`, optional UUID `after`, and `limit` (1–100, default 50).
- `GET /api/pa-mapping-review/exports/{export_id}/download`: authenticated download, no public link.
- `POST /api/pa-mapping-review/exports/{export_id}/imports`: raw UTF-8 `text/csv` body, not multipart. Streaming input is capped before parsing; no upload spool files.
- `GET /api/pa-mapping-review/exports/{export_id}/import-status`.

The format is UTF-8 CSV with an exact, ordered header. Server exports use CRLF and standard CSV quoting. UTF-8 BOM on import is supported. Keep every protected cell and every exported row unchanged; row ordering may change. Source spreadsheet-formula prefixes are escaped with an apostrophe, which is itself protected. Embedded control characters, malformed UTF-8, unexpected headers/extra cells, duplicate/missing/added rows, oversized files, and malformed proposals are rejected. File size is capped at 512 KiB, exports/imports at 100 rows, and creation scans at most 200 preparation candidates per transaction. The response's `nextCursor` advances over scanned preparation IDs; export scope is immutable, not a chronological pagination guarantee. Exports expire after 24 hours. They and their audit evidence are retained, not TTL-deleted.

Exact columns, in order:

```text
schema_version,export_id,export_row_id,expires_at,batch_id,preparation_line_id,preparation_revision,order_id,order_line_id,referring_provider_id,aggregator_id,issue_pa_provider_id,source_namespace_id,source_identity_type,source_identity_key,source_code,source_description,existing_mapping_id,existing_mapping_version,existing_mapping_status,proposed_medicloud_code,proposed_medicloud_description,mapping_notes
```

Only `proposed_medicloud_code`, `proposed_medicloud_description`, and `mapping_notes` are editable. They convey no authorization. Codes preserve case and must already be trimmed, match the conservative ASCII code syntax, and cannot target reserved PRE11. Descriptions may be absent; they are never fabricated or substituted with the code. Blank codes remain unresolved; a description without a code is rejected. Notes/descriptions are bounded to 1,000 characters, codes to 200. Do not put member data or secrets in these fields.

Exports include only current, source-bound rows needing mapping review. Missing namespace, invalid price/provider/medication identity, and PA-ledger cases are excluded. An unavailable/stale member receipt can coexist with an independently actionable mapping review; exporting such a row neither asserts eligibility nor makes it ready for submission. The stored member receipt is used only to reproduce preparation at its original calculation time and is never exported. Source drift requires recalculation before export/import.

Server-held manifests bind opaque export/row IDs, expiry, protected cells, scope, preparation IDs/revisions, material input fingerprints and mapping head/latest-version evidence. They retain SHA-256 content/protected/scope digests. A client manifest is never accepted. Downloads and first imports recheck current ownership, sources, revisions and mapping evidence. The batch transaction guard serializes with recalculation/closure; source reads are snapshot-consistent as of validation. Later mapping activation/submission must independently revalidate current source state; a proposal receipt is not a freshness or eligibility certificate.

All proposals in an import, the receipt, manifest state change and audit events commit together through the mandatory Mongo transaction primitive. No nontransactional fallback or automatic retry is used. Driver write conflicts become domain conflicts. Real replica-set transaction/concurrency validation remains a production prerequisite; in-memory tests do not establish real Mongo isolation behavior.

Imports append immutable `PROPOSED` versions with CURSOR source and `CURSOR_PROPOSAL` provenance, uploader/time/export/row references and suggested text. Existing heads, including ACTIVE heads, are left entirely unchanged. A previously unmapped identity gets a nonactive head. Pending versions are not current/active authority. Version allocation uses the highest immutable version, allowing existing explicit lifecycle operations to coexist with pending proposals. A later review workflow must explicitly select/review a pending proposal; import never selects it for activation. Namespace-identical rows with identical suggestions coalesce; inconsistent suggestions/source identities reject the entire import. Latest-version binding prevents conflicting imports from separate outstanding exports from silently racing.

An identical repeat for the same unexpired, intact export returns the original receipt without new versions or audit writes. Different repeat content conflicts. Blank proposals produce unresolved counts. Append-oriented events record export creation/download, imported proposal versions and import completion with bounded export/row references; raw files/member/PA evidence are not audit payloads. Server receipts expose counts/status only.

Export creation JSON bodies are capped at 4 KiB, including streamed requests. A bounded scan with no eligible rows but more candidates returns `EMPTY_SCAN`, no export ID/file, and a `nextCursor`; callers continue scanning with the same batch version. A fully exhausted scope with no eligible valid rows returns `EMPTY`, no export ID/file, and zero exported rows. Both empty and nonempty results report `skippedRows` and bounded, machine-readable `skipReasons` counts.


Phase 3A review fixes: manifest creation/expiry timestamps are normalized to UTC millisecond precision before expiry calculation or hashing. Digest verification uses the same exact canonical UTC representation and rejects non-millisecond timestamps rather than rounding altered values. The 24-hour expiry and integrity checks remain strict across BSON datetime storage.

Export candidates are isolated individually: stale/malformed rows, invalid mapping identities, unrepresentable CSV cells and rows exceeding the remaining file budget are skipped without modifying preparation history. Valid rows alone enter the immutable manifest. Skip diagnostics contain static reason/count pairs, never row contents or exception text; counts are retained in the manifest and append-oriented export/empty-scan events. Mongo driver failures still abort the transaction. Import/download scope validation remains strict for every actually exported row.

### Phase 3B foundation — signed offline procedure catalog

This foundation adds only catalog extraction, signed-provenance verification and independent catalog validation/trust. It adds no mapping approval or activation endpoint. Existing priced orders, quantities, PA ledgers and PA reference ownership are untouched.

An authorized offline workstation extracts only `procedurecode` and `proceduredesc` from `dbo.proceduredata`. `benefitcode_procedure` is not the catalog authority. The workstation is not a normal backend/team runtime dependency. Server requests use immutable Mongo catalog entries and signed evidence; they never connect to SQL or the Mac.

The extractor is `python -m scripts.extract_pa_catalog`, with required `--secrets-file`, `--private-key-file`, and `--output-file` paths. It reads only the existing TOML `credentials.server/database/username/password/port` in memory, uses ODBC Driver 18 with `Encrypt=yes;TrustServerCertificate=no`, and never emits credentials/connection strings. It does not generate keys. The existing Ed25519 private key must be outside the primary repository and its linked worktrees. Encrypted PEM keys may use the transient `PA_CATALOG_SIGNING_KEY_PASSPHRASE` environment variable. The private key and passphrase are never placed in the artifact or logs. The output is created exclusively (no overwrite) and contains only procedure data and public provenance.

ODBC Driver 18 and pyodbc are offline workstation requirements; `scripts/requirements-pa-catalog-extractor.txt` lists the Python dependency. Nothing was installed and no extraction was run as part of implementation. The backend requires `cryptography` but does not import pyodbc. Provisioning signing keys, TLS/ODBC readiness and read-only SQL access are separate authorized operational steps.

Server/workstation authority configuration is explicit and has no production defaults:

- `PA_CATALOG_PUBLIC_KEY_PATH`: provisioned Ed25519 public PEM verification key.
- `PA_CATALOG_AUTHORITY_ID`: approved source authority identifier.
- `PA_CATALOG_SQL_SERVER_IDENTITY`: exact expected SQL `SERVERPROPERTY('ServerName')`.
- `PA_CATALOG_SQL_DATABASE_IDENTITY`: exact expected SQL `DB_NAME()`.

The source identity is established by TLS-authenticated SQL connectivity plus those exact metadata checks and existence of `dbo.proceduredata`; a Mac hostname or filename establishes nothing. Missing/mismatched identity fails closed. The private key must match the configured public authority. The source read uses an autocommit-disabled transaction: count, complete two-column SELECT to EOF, and final count under `HOLDLOCK,TABLOCK` shared table locks. These locks preserve a complete, consistent table read and can temporarily block catalog writers; run only in an authorized extraction window. The transaction is rolled back to release locks; all executed SQL statements are SELECTs. Missing rows, duplicates, invalid descriptions and unknown source identity produce no authorized artifact.

Artifact schema v1 is deterministic UTF-8 JSON with sorted exact code/description entries. The catalog digest retains Phase 1's SHA-256 wire hash of sorted code/description pairs. The Ed25519 signature covers canonical compact, sorted-key JSON of the entire manifest: source authority/server/database/table/columns, millisecond UTC extraction time, schema version, digest, record count, expected/final SQL counts, completed flag, consistency method and signing-key fingerprint. Private key material and client-provided verification keys are not artifact fields. The server verifies against its own configured key; changing configuration does not silently accept old signatures.

Artifact inputs are bounded to 16 MiB and 100,000 entries. Strict schema/UTF-8/duplicate-JSON-key checks, unique normalized codes, genuine canonical descriptions (no code fallback), exact row counts and full catalog digest are enforced. Canonical codes/descriptions are preserved rather than fabricated. Invalid new imports do not replace previous trusted evidence. Ingestion uses the existing 1,000-entry transaction chunks; partial `INGESTING` snapshots are never trust evidence. No new collections or indexes are needed.

Maximum evidence age is **24 hours**, measured from signed extraction time. Future timestamps and age at or beyond 24 hours fail closed at ingest, independent validation, trust promotion and canonical lookup used for new mapping authorization. Existing ACTIVE mapping resolution preserves integrity checks without requiring fresh evidence. Expired snapshots/evidence remain stored for audit. This is code/description authority only: provider tariff eligibility and price applicability remain explicitly `NOT_VERIFIED`.

Three authenticated routes are registered:

- `POST /api/pa-catalog/snapshots`: raw JSON artifact, requires `PA_CATALOG_INGEST`.
- `POST /api/pa-catalog/snapshots/{snapshot_id}/validate`: empty body, requires `PA_CATALOG_VALIDATE`.
- `POST /api/pa-catalog/snapshots/{snapshot_id}/trust`: empty body, requires `PA_CATALOG_TRUST`.

Permissions and actor identity come from authoritative staff records. Clients cannot nominate a public key, validator, trust actor, validation receipt or TRUSTED status. An uploaded artifact receives structural `VALIDATED` status only, not authority to activate mappings. Independent validation re-verifies signature/completeness from server-held entries and writes one immutable validation receipt bound to manifest digest/key fingerprint. Trust promotion re-verifies again and requires a trust actor distinct from both the uploader and the persisted validator. Uploader, validator and truster must be three distinct authorized staff identities, enforced in the domain and rechecked during canonical lookup. Roles cannot collapse into a single self-certifying request. Absent authority configuration produces safe 503 responses; invalid evidence/conflicts fail closed without payload/credential details.

Signed manifest/signature/digest/fingerprint are retained immutably alongside the existing snapshot; validation bindings extend the existing evidence model. Append-only snapshot/validation/trust events record authenticated identities and references. Mandatory Mongo transactions cover each publication stage and rollback; no unsafe fallback or external-side-effect retry exists. Legacy unsigned snapshots cannot serve as new activation evidence merely because they contain the old extraction-method label. Real replica-set and operational extraction validation remain prerequisites before production use. Tests use ephemeral test-only keys and fake SQL/Mongo, never production credentials.


### Phase 3B review safeguards

Catalog uploader, evidence validator and catalog truster must be three distinct
persisted staff identities. Domain validation, trust promotion and canonical
procedure resolution enforce this rule; Cursor output supplies no authority.

All three catalog mutations require strict `application/json` (optionally UTF-8).
Validate/trust accept only `{}`. Cookie-authenticated browser requests must carry
an exact allowed frontend Origin or the API's same Origin. Non-browser clients
without Origin must explicitly send `X-Pharmacy-CSRF: 1`; cross-site Fetch
Metadata is rejected on that path. Missing/null/form Content-Type and null,
foreign or duplicate Origin cannot authorize a mutation. Authentication and
server-record permissions remain mandatory; CORS alone supplies no protection.

The 24-hour limit is exclusive: evidence aged exactly 24 hours cannot authorize
new validation, trust, mapping validation or activation/replacement. Existing
ACTIVE mappings may resolve against their previously trusted immutable snapshot
after expiry. This exception requires the current active head/version and exact
snapshot/code association, and still checks signature, source identity, complete
content integrity, trust evidence, distinct identities and revocation. Expired
snapshots remain available for audit and do not establish provider eligibility
or tariff price applicability.

Full bounded catalog verification is reused only within a request/calculation or
Mongo transaction, keyed by database/session, complete snapshot and validation
metadata, and the current configured public-key fingerprint/source authority.
Each medication then uses the indexed `(snapshotId, normalizedCode)` lookup and
compares its exact code/description to the verified signed entry binding. Trust,
revocation, evidence/header changes and key rotation are rechecked each use.
Entry changes cannot escape the signed binding; subsequent operations verify the
whole catalog again. There is no persistent cross-request cache. Real Mongo
replica-set transaction validation remains a production prerequisite.
