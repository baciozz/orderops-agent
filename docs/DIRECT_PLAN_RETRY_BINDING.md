# Direct plan retry identity

`POST /plans` now binds an event key to the normalized change request inside the
same SQLite `BEGIN IMMEDIATE` transaction that creates its plan. This hardens
request-to-plan association; it is not a finding of approval bypass. Approval and
commit authorization, plan business hashes, schema, inbox receipt replay and the
durable worker's internal event IDs are unchanged.

## Identity and replay

The fingerprint includes binding version, direct-change kind, order ID, requested
base and intent revisions, cleaned changes, source references, and bump-intent mode.
HTTP Pydantic validation happens first: accepted numeric strings normalize to
numbers, omitted optional fields normalize to their defaults, and object key order
is irrelevant. Change and source-reference list order is significant; no trimming,
SKU aliasing, list sorting, or equivalence of different change modes is inferred.
All source-reference fields count, including excerpts and inferred flags. Unknown
API fields remain rejected by the existing schema. Transport request/correlation
IDs, clock time, and the spelling/presence of the key are not body identity.

The key selection is unchanged: nonempty explicit `client_event_key`, otherwise
`plan-{order_id}-{intent_revision}` (including explicit empty string). Explicitly
providing that derived key is the same key, not a new namespace. A different body
using it returns `409 PLAN_REQUEST_CONFLICT`, before stale-version checks and
without changing any table, invalidating approval, or rewriting a plan.

A matching bound request returns the original plan/task/hash/revisions even after
approval, later intent, or commit. Existing replay status semantics remain: current
COMMITTED/INVALIDATED state is returned, not a frozen VALIDATED receipt. Existing
error replay details are not expanded: shortage replay uses the existing stored
error code/message behavior. New keys continue normal version validation and intent
invalidation; the binding is not a substitute for checking current plan state.

## Compatibility and failure boundary

Only the direct route opts in (`plan_change(bind_request=True)`). Internal callers
remain unbound by default and preserve historical behavior for unbound keys. They
cannot consume a newly direct-bound key, even through a different kernel plan kind;
that collision returns 409. This is intentional isolation, not a new HTTP CREATE API.

Bindings live as versioned metadata in `Plan.normalized_changes`, outside business
hash inputs. Existing historical records have no trustworthy original-request
fingerprint and are not backfilled from the resulting plan. Direct reuse fails
closed with 409, including old CHANGE/CREATE plans, missing or unknown binding
versions, and events whose payload reference is absent or is not a Plan.

No-event early validation failures do not reserve a key. Persisted shortage and
clarification plans receive bindings and do not produce duplicate writes on retry.
Diagnostic-only failures have an Event and Task but no Plan: the first response and
diagnostic writes retain their original behavior (typically 422). Subsequent direct
calls with that key, identical or changed, return 409 without writes because identity
cannot be verified. They do NOT promise exact replay. No schema migration was added
to make these diagnostic-only events bindable.

On a historical/diagnostic key conflict, inspect the original task/plan first. If a
new planning event is intended, use an explicit new event key and current versions.
Do not blindly change a commit idempotency key after a commit timeout.

## Tests and limits

Focused tests call the real route with validated request objects and isolated request
state. They cover all-table snapshots, approval preservation, commit replay identity,
current status, derived keys, normalization, source evidence, legacy/cross-kind
collisions, diagnostic failures, and simultaneous exact/conflicting requests against
synthetic file-backed SQLite databases. No HTTP server/browser/live integration is
required. Full suite and HTTP transport integration remain separate gates.
