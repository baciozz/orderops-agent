# Durable CREATE input ownership

A fresh accepted receipt on an unbound task supersedes its older CREATE candidates in
the same SQLite `BEGIN IMMEDIATE` transaction. The task epoch becomes one greater
than the maximum task/prior-plan revision, all noncommitted task-local CREATE plans
are invalidated, and their ACTIVE approvals are revoked. The original records remain
available as evidence. No order, stock, reservation or operation is written at receipt.
Receipt-key replay precedes the fences and does not advance the epoch. Initial
unbound receipt counts as epoch 1; a bootstrap question also consumes one epoch.

CREATE planning consumes the durable task epoch and the exact latest owned input,
never a revision supplied by the parser. First CREATE outcome establishes a fresh
business chain, independent of the conversation/thread and any inferred existing-order
chain. Subsequent validated, clarification and shortage outcomes preserve that chain.
Persisted CREATE plan events plus their task-owned input receipts prove chain ownership;
no schema change or migration is needed. A supported conversion through an inferred existing-order scope and back to CREATE
establishes a fresh business chain rather than inheriting the existing-order chain.

Approval and commit each independently check current task state, plan state, epoch,
and the kernel plan event's exact input provenance. Comparing only the old plan's hash
or its own revision is insufficient. A queued, invalid or clarifying replacement still
retires older candidates immediately. A fresh successful candidate requires its own
explicit approval and explicit commit. Successful operation/idempotency replay remains
before the new checks, including recovery after acknowledgement loss.

## Compatibility and transaction order

- Existing selected-order and late-bound CHANGE_ORDER chronology remains unchanged.
  The original explicit `input.submit` selection guard still rejects selected-order
  NEW_ORDER output. An originally unbound task with an inferred/partial binding can
  still become NEW_ORDER; its current answer receipt may reference the inferred order.
  The CREATE plan event itself has no order binding. Planning preserves the receipt's
  current task epoch and establishes a fresh CREATE chain for that scope conversion.
- Direct kernel and non-durable dispatcher calls have no input receipts. They retain
  their independent new-chain/revision-1 initialization and existing replay behavior.
- Two new tasks in the same thread have separate business chains. Supersession is
  task-scoped and does not revoke another task's CREATE approval.
- Receipt, approval and commit serialize on the existing write transaction. If commit
  wins first, the terminal task rejects the new answer. If receipt wins, the older
  candidate cannot subsequently be approved or committed, even after replanning.
- Existing lease/current-input fences remain authoritative. Receipt rollback restores
  old approvals and epochs; committed receipt replay and worker retries cannot advance
  an epoch twice. No automatic approval or business submission was added.

## Legacy data

Current legacy CREATE plans remain usable when task state, revision and server-owned
input/plan-event provenance all agree. Older plans with the same legacy revision are
rejected by input identity. Missing or inconsistent durable provenance fails closed.
For consecutive unbound CREATE corrections, planning preserves the current task chain
only when an existing task-local CREATE origin proves it. Otherwise it rejects rather
than guessing. No historical chain registry is introduced. A task with no prior CREATE receives a fresh chain on first outcome,
including pre-fix tasks whose temporary chain was a thread ID. No old approval is
reactivated and no counter is reset. Such rejected legacy tasks require review and a
fresh task, not retrospective authorization repair.

Tests use synthetic SQLite databases and controlled strict LIVE outputs without a
provider. They verify infrastructure only, not natural-language interpretation accuracy.
The separate spreadsheet normalization, shortage resumption and contextual quantity
correction work is not enabled by this fix.
