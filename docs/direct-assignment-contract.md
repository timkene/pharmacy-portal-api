# Pharmacy direct assignment contract

Backend-only implementation. No deployment, migrations, external PA integration, or CDR coupling. All routes below have prefix `/api/orders/{orderId}`.

## State transitions

| Operation | From | To |
| --- | --- | --- |
| Assign one aggregator | `pending_review` | `direct_quote_requested` |
| Submit direct price | `direct_quote_requested` | `direct_price_review` |
| Approve / adjust and approve | `direct_price_review` | `awaiting_fulfillment` |
| Accept | `awaiting_fulfillment` | `accepted` |
| Fulfil: delivered | `accepted` | `awaiting_confirmation` |
| Fulfil: picked up | `accepted` | `completed` |
| Existing receipt confirmation | `awaiting_confirmation` | `completed` / `not_received` |
| Deny quote | `direct_price_review` | `direct_reassignment` |
| Recall direct assignment | Any of the four pre-fulfilment direct states above | `direct_reassignment` |
| Reassign | `direct_reassignment` | `direct_quote_requested` |
| Edit final price | Post-fulfilment state | Same state |
| Cancel | Post-fulfilment state | `cancelled` |
| Recall after fulfilment | Post-fulfilment state | `post_fulfilment_recalled` |

Post-fulfilment states are `awaiting_confirmation`, `completed`, `not_received`, and legacy `fulfilled`. Cancellation and reversal are terminal administrative states: no reassign, accept, fulfil, or confirmation may reactivate them. Original fulfilment, delivery fee, winner, and completion timestamps remain intact. An order awaiting receipt confirmation is already fulfilled for these controls.

Competitive flow stays `pending_review → bidding → clearline_price_review → awaiting_fulfillment → accepted → fulfilment`; existing `/approve`, `/bids`, `/close-bidding`, `/clearline-approve`, and confirmation routes remain. Review flags remain advisory and staff-only, using the existing review gate.

## Requests and authorization

All listed actions are `POST`. `expectedVersion` is the **order's `version`**, not `assignmentVersion`; send the value from the last GET. First assignment may omit it for existing client compatibility. Reassignment, all new routes, and direct acceptance/fulfilment require it. Every new lifecycle mutation increments `version`; every new assignment increments `assignmentVersion`. Competitive acceptance accepts no body or `{}`; direct acceptance requires `expectedVersion` (missing: 422; stale: 409). Competitive fulfilment remains backward compatible.

| Route | Role | JSON body |
| --- | --- | --- |
| `/assign` (changed; also reassigns) | Staff | `{ "aggregatorId": "...", "expectedVersion": 0 }` |
| `/direct-quote` (new) | Current assigned aggregator | `{ "totalPrice": 1000, "expectedVersion": 1 }` |
| `/direct-approve` (new) | Staff | `{ "expectedVersion": 2 }` |
| `/direct-approve` with adjustment | Staff | `{ "adjusted_price": 950, "reason": "Agreed discount", "expectedVersion": 2 }` |
| `/direct-deny` (new) | Staff | `{ "reason": "Price declined", "expectedVersion": 2 }` |
| `/recall` (new; pre/post fulfilment) | Staff | `{ "reason": "Recovery requested", "expectedVersion": 3 }` |
| `/accept` (changed) | Assigned/winning aggregator | `{ "expectedVersion": 3 }` |
| `/fulfill` (changed) | Assigned/winning aggregator | `{ "fulfillmentType": "delivered", "deliveryFee": 50, "expectedVersion": 4 }` |
| `/adjust-price` (new; post fulfilment) | Staff | `{ "totalPrice": 1050, "reason": "Final correction", "expectedVersion": 5 }` |
| `/cancel` (new; post fulfilment) | Staff | `{ "reason": "Administrative cancellation", "expectedVersion": 5 }` |

Direct quotes are order-level total prices; no unit-price allocation is defined or inferred. Reporting must use `directQuote.totalPrice` for the submitted quote and `winnerTotalPrice` for the current approved/final inclusive price. The approval audit event retains the approved amount before delivery fees or later adjustments. Prices are finite positive numbers, using existing numeric money conventions. Delivery fees are finite and nonnegative. Reasons are trimmed, nonblank, and limited to 2,000 characters. Changing a quote price requires a reason; approving the quoted price does not. `/fulfill` accepts `delivered` or `picked_up`. Delivery adds its fee once to `winnerTotalPrice`; `/adjust-price` replaces the **final inclusive total**, without adding that fee again. `directQuote.totalPrice` always retains the original quote.

Lifecycle success responses: `{ "success": true, "status": "...", "version": 4, "assignmentVersion": 1 }`. Refresh the detail after success. Existing competitive approval/confirmation endpoints retain their original success response; refresh after those too.

Errors: `400` invalid state/workflow, `401` unauthenticated/wrong session role, `403` assignment unavailable to this aggregator, `404` unknown order/aggregator, `409` stale version or concurrent change, `422` invalid/missing fields. On `409`, refetch and ask staff/aggregator to review current state before retrying; never automatically replay with a fresh version. `DELETE /api/orders/{id}` now returns `405` for existing orders, protecting history from deletion.

Repeated cancel or post-fulfilment recall returns `409` with an already-terminal/action-unavailable message, without another mutation or audit event.

## Reads, audit, and frontend controls

`GET /api/orders/{id}` and staff list items expose `assignmentType`, `status`, `version` (legacy default `0`), `assignmentVersion` (legacy default `0`), `directQuote`, `winnerTotalPrice`, `priceApprovedAt`, `acceptedAt`, `fulfilledAt`, `completedAt`, `cancelledAt`, and `recalledAt`. `directQuote` contains `totalPrice`, `submittedAt`, `aggregatorId`, and `assignmentVersion`; it is null until quoted. Winner total remains null until approved. Staff detail exposes `winnerId` and `history`; aggregator detail returns empty history and no staff review flags.

Direct denial persists the trimmed reason as `denialComment`, `deniedBy: {userId, name}`, and UTC `deniedAt`, matching staff rejection semantics. Staff detail exposes all three; list items expose `denialComment`. Assignment/reassignment resets all three to null for the new attempt while preserving the denial audit event.

`history` is append-only, embedded in the order, and committed in the same Mongo update as the state/price change. Each event includes `eventType`, UTC `timestamp`, `actorId`, `actorName`, `actorRole`, `reason`, `oldValues`, `newValues`, `aggregatorId`, `aggregatorName`, and `assignmentVersion`. Snapshots include only changed workflow fields, not enrollee/medical details. Denial/recall preserves the original quote in history; reassigning resets current quote/approval fields without removing earlier events. Adjustment plus approval emits both `price_adjusted` and `direct_quote_approved` atomically. Assign/reassign, direct-approve, direct-deny, recall, adjust-price, and cancel require authenticated staff sessions; the legacy service key does not authorize them. Existing machine integration routes retain service-key access with constant-time comparisons. Staff actions use staff role; receipt callbacks use system role.

Event types: `direct_assigned`, `direct_quote_submitted`, `direct_quote_approved`, `direct_quote_denied`, `direct_recalled`, `direct_reassigned`, `aggregator_accepted`, `fulfilled`, `price_adjusted`, `cancelled`, `post_fulfilment_recalled`; also `price_approved`, `receipt_confirmed`, `receipt_disputed` for existing flow actions. History is not a separate collection: it uses Mongo's single-document atomicity and shares its document-size limit. No endpoint truncates or deletes it.

### CIL-FRONTEND

- In `pending_review`, use existing staff aggregator selector (`GET /api/aggregators`) and `/assign` to send to exactly one aggregator.
- Show “Waiting for aggregator quote” in `direct_quote_requested` and allow Recall with a reason.
- In `direct_price_review`, show the original submitted quote, Approve Price, Adjust Price + Approve, Deny Price (required reason), and Recall.
- In `awaiting_fulfillment` / `accepted`, direct orders retain Recall until fulfilled.
- In `direct_reassignment`, display the denial/recall from staff history and Reassign via `/assign`; no direct accept/fulfil controls.
- In post-fulfilment states, offer Edit Price, Cancel, and Recall/Reversal; require reasons and clearly describe that fulfilment remains recorded. Display terminal reversal/cancellation status alongside original fulfilment details.
- Display staff `history` chronologically, including previous/new prices, assignment attempts, reasons, and actors.
- Put Generate PA with post-fulfilment administrative actions, driven by `paGeneration: { "available": false, "status": "not_configured" }`. Render disabled/unconfigured. There is intentionally no generation endpoint, credential, or CDR call.

### PHARMACY-PORTAL

- Both `GET /api/aggregator/orders` (`active`) and `/api/aggregator/dashboard` (`wonOrders`) include assigned direct quote/review states. Use `assignmentType === "direct"` to distinguish these from open bidding.
- `direct_quote_requested`: show total-price input and submit via `/direct-quote`; no bid submission or Accept.
- `direct_price_review`: show the submitted quote and “Waiting for Clearline price approval”; no Accept or second quote submission.
- `awaiting_fulfillment`: show approved `winnerTotalPrice` and enable Accept only after approval; `accepted`: use existing fulfilment controls with `expectedVersion`.
- Denied/recalled assignments disappear from the old aggregator's active list. Direct detail and stream return `403` once access is revoked. Clear cached prescription data, remove controls, and show a generic “Assignment no longer available” message; do not expose staff reasons or other aggregators' quotes.
- Reversed/cancelled fulfilled orders remain in `fulfilled` / `completedOrders` with their explicit terminal status. No fulfilment controls remain available.

Both surfaces: subscribe to `order_changed` on existing `/stream` and refetch authorized detail/list. Staff retain existing SSE events too. Aggregator initial `bid_update` contains only their own bids; subsequent shared notifications become `order_changed: { "refresh": true }`, preventing competitor data leakage. An existing direct subscription emits `access_revoked` and closes on recall/denial/reassignment (checked on events or within the 15-second heartbeat). Stop reconnecting and clear the detail on that event. SSE is process-local; refresh on focus/reconnect or poll periodically for multi-worker reliability.

## Authenticated staff identity

`GET /api/auth/staff/me` authenticates the existing `staff_session` cookie using the backend signed-session decoder and explicitly requires role `staff`. Success is `200` with exactly these string fields (no response envelope):

```json
{ "userId": "...", "name": "...", "email": "..." }
```

Identity comes entirely from the authenticated signed login payload; no database or network lookup occurs. This is the identity captured at login. The endpoint does not refresh/rotate the session or set cookies, and returns `Cache-Control: no-store`. It never returns the token, signature, secret, role/expiry metadata, or password/hash.

Missing cookies return `401 {"detail":"Staff authentication required"}`. Malformed, unsigned, tampered, expired, wrong-role, or invalid identity sessions return `401 {"detail":"Invalid staff session"}`. An aggregator token cannot authenticate even when placed in `staff_session`. Service keys do not authorize this endpoint.

PHARMACY-PORTAL migration: **OLD:** frontend decodes `staff_session`. **NEW:** frontend calls the authenticated identity endpoint on dashboard initialization and uses its `userId`, `name`, and `email`. Clients must treat all session tokens as opaque: never decode or verify them, and never receive or configure `SESSION_SECRET` in the frontend.

Use the existing backend API base URL and cookie-based login, with `credentials: "include"` on both login and identity browser requests:

```js
const response = await fetch(`${API_BASE_URL}/api/auth/staff/me`, {
  credentials: "include",
  cache: "no-store",
});
if (response.status === 401) {
  // Clear cached staff identity and return to staff sign-in.
} else if (response.ok) {
  const { userId, name, email } = await response.json();
  // Populate the staff dashboard identity.
} else {
  // Show a request error; do not fall back to decoding the cookie.
}
```

Server-side frontend requests must forward the incoming `staff_session` cookie to the trusted backend; `credentials: "include"` alone does not forward browser cookies on the server. Preserve the existing login cookie transport. CORS retains its explicit allowed origins and credentials policy, including `https://pharmacy-portal-delta.vercel.app`. Cookies remain HttpOnly, Secure/SameSite=None in production and SameSite=Lax without Secure in development, with no added domain override. No CORS or cookie security changes are needed.

## Compatibility and release prerequisites

No migration is required. Historical documents missing lifecycle fields remain readable, and post-fulfilment administrative controls work with version `0`. Legacy direct orders awaiting acceptance without explicit approval must be recalled/reassigned through quoting before acceptance. Already-accepted legacy direct orders may finish the existing fulfilment flow (send `expectedVersion: 0` when no version exists); no fictional historical approval is created.

The previous unsigned base64 session cookies allowed role forgery. Sessions now use HMAC signatures, role binding, and a 24-hour expiry. A stable `SESSION_SECRET` of at least 32 random characters must be provisioned before release; `.env.example` contains only an empty placeholder. All old sessions must log in again using the existing staff/aggregator login endpoints. Clients must treat returned session tokens as opaque and use the auth response for display names. Before deployment, an operator must set a stable, securely generated `SESSION_SECRET` of at least 32 characters on the Render service, shared by all instances. `render.yaml` declares it with `sync: false`; no secret is supplied or generated here. Startup validates it before database initialization or serving traffic and fails if missing/short. Render uses `/health`, which returns 503 if authentication configuration becomes invalid. Preserve the value across restarts; changing it invalidates signed sessions. Existing sessions require a fresh staff/aggregator login after rollout; unsigned sessions are never accepted. The existing Pharmacy service-key mechanism remains available only on legacy integration routes. No secrets or deployed configuration were changed by this task.

Tests use synthetic records, mocked integrations, a conditional-update Mongo fake, and a network-denying fixture. They do not constitute a real Mongo deployment/integration test. External PA generation remains intentionally unimplemented.

## Local backend tests

Use Python 3.10 or newer. Install with `python -m pip install -r requirements-dev.txt`, then run the complete suite with `python -m pytest -q`. Production installs only `requirements.txt`. Tests supply synthetic session secrets and mock database/network integrations; no live services are required.
