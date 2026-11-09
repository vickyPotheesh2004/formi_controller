# Governance

Every change to a client's agent passes through the governance boundary. The
controller has two legal routes:

1. **Approval paper trail** — `POST /approvals`, wait for a human, then
   `POST /workflows/{id}/apply` only while the approval is unexpired and the base
   version is still current.
2. **Policy-permitted auto-apply** — `POST /workflows/{id}/apply` with an
   embedded proposal, only when the workflow's `auto_apply` policy allows the
   changed dimensions and the allocation shift is within `max_shift`.

There is no third route.

## Proposal lifecycle

```
validate config
   │
   ├─ invalid ─────────────────────► proposal_validation (rejected, ledger)
   ├─ no diff ─────────────────────► proposal_no_change (no_action, ledger)
   │
   ├─ duplicate of pending/approved/applied/submitting
   │        └───────────────────────► suppressed (no second request)
   │
   ├─ duplicate of rejected/expired/lapsed/failed with attempts < 2
   │        └───────────────────────► re-propose with a new attempt id
   │
   ├─ auto path (policy allows, not a prompt change)
   │        ├─ POST apply ─────────► applied / failed (ledger lifecycle)
   │        └─ 409 duplicate proposal id
   │                 └─────────────► check history, close if already applied
   │
   └─ approval path
            ├─ outside approver hours ──► propose_deferred (ledger)
            ├─ pending request exists ──► wait
            ├─ per-workflow or global daily budget hit ──► propose_deferred
            ├─ prompt without validation_run_id ──► prompt_change_rejected
            └─ POST approvals
                     ├─ 409 stale_base_version ──► cooldown, no request
                     └─ pending ──► poll
                              ├─ approved ──► apply if base still current
                              │                └─ stale ──► status=stale, no overwrite
                              ├─ rejected / expired / lapsed ──► close, ledger
                              └─ applied (from before a crash) ──► reconcile, never re-apply
```

### Auto-apply policy

`cohorts.auto_apply_check` is the only gate:

* policy `enabled`;
* every changed dimension is in `allowed_dims`;
* total variation distance of the allocation shift `<= max_shift`;
* there is an actual change.

All three must hold. The controller additionally never auto-applies a
`prompt_version` change even if the policy would permit it: a prompt is
customer-facing language, and the assignment's trust contract is more important
than the few hours an approval costs.

### Approver attention

Approver time is a finite resource, so the controller batches and defers:

* **hours**: no request outside the workflow's `approver_hours`; it is deferred
  and recorded, not queued silently;
* **one at a time**: no new request while one is pending for the same workflow;
* **per-workflow cap**: `max_requests_per_24h` from the policy (checked against
  the controller's own approval rows);
* **global cap**: `CONTROLLER_MAX_APPROVALS_DAY = 6` across all workflows, so a
  noisy workflow cannot starve the others;
* **cooldown**: a per-workflow `next_proposal_sim_minute` after a stale base,
  lapse, or rejection;
* **TTL**: `min(policy.proposal_ttl_hours, 24)`; the previous hard 12-hour cap
  missed next-day approver windows on overnight requests, which showed up as
  avoidable expiries. The simulator still owns expiry; the controller only
  avoids setting itself up to expire;
* **no resubmission of the same unchanged proposal** beyond
  `max_proposal_attempts = 2`; attempt numbers are part of the proposal id, so a
  retry is a distinct, auditable object.

### Impact, affected leads, dry run

* `impact_score` is a **controller estimate** in `[0,1]` derived from the size of
  the change (allocation shift, schedule change, incident severity). It is
  labelled as an estimate in the ledger, never as measured impact.
* `affected_leads` is the recent uploaded-lead count for the cohort the change
  touches, from the controller's own `lead_daily` aggregates. It is a
  conservative proxy for leads in flight, not a promise.
* `dry_run` is honest about what is available. When the controller has no
  historical replay for a change it sends
  `{"method": "no_historical_replay_available", "note": ...}` rather than
  fabricating a counterfactual result. The ledger stores hypothetical
  counterfactuals separately and marks them `observed: false`.

## Supervisor edits

A supervisor edit is authoritative. The controller:

1. **Adopts it as the live baseline** — it syncs config history and never
   proposes a reversal.
2. **Records a `supervisor_edit` ledger decision** with the version, changed
   dimensions, and source endpoint.
3. **Blocks proposals touching the same dimensions for 12 simulated hours**
   (`SUPERVISOR_COOLDOWN_MIN`).
4. **Closes any running study on those dimensions** and resets the promotions.
5. **Never re-opens a study on a supervisor-set dimension.** This is the fix for
   the public_02 failure where a post-supervisor voice study diluted the
   supervisor's `V2: 1.0` back to 40% and lost engagement. Re-randomising a value
   the supervisor explicitly set is a silent override, even if it happens after
   the cooldown.

The one exception is a **prompt safety fix**: if a supervisor ships a prompt that
then trips the hallucination threshold, the incident path is allowed past the
supervisor gate (`ignore_supervisor_gate=True`) because the safety incident is
new evidence about a change that is already live. The prompt change still goes
to a human approver.

## Stale bases

If the live version changes between proposal and apply — most often because a
supervisor edited config — the controller refuses to apply:

* `base_version != p.base_version` locally: mark `stale`, ledger it, cooldown.
* simulator returns `409 stale_base_version`: mark `stale`, ledger it.
* the newer config wins, always.

## Crash safety and idempotency

* The chunk checkpoint and its aggregate counters commit in one SQLite
  transaction. A replay of a committed chunk is skipped.
* `proposal_id` is deterministic from `(workflow, base_version, config, kind,
  parent, attempt)`. A retried request reuses the same id; the simulator's
  `409 duplicate_proposal_id` is reconciled by finding the existing approval
  and linking it locally.
* If the simulator config history already contains a proposal id, local
  `submitting` proposals are marked `applied` on restart.
* An approval found in the simulator but missing locally is recorded as
  `approval_orphan`; it is not applied blind, because the proposed config may
  not be recoverable.
* The `decisions` and `counterfactuals` tables reject UPDATE and DELETE with
  triggers. Lifecycle is appended, never rewritten.

`scripts/crash_recovery.py` and `reports/crash_recovery.md` contain the
kill/restart evidence.
