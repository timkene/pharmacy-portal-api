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
