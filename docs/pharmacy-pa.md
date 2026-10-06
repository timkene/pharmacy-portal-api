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
