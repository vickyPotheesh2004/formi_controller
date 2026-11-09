# Failure scenarios

Each entry: what can fail, what the controller does, and what a human sees.

## 1. Simulator returns 5xx or times out

`api.SimClient` retries transport errors and 5xx with exponential backoff up to
`CONTROLLER_MAX_RETRIES`. A 4xx is not retried; it is surfaced as `SimError` and
the loop logs it. Ingestion and approval polling are wrapped so one failed
component does not stop the clock; the next tick retries. If the run cannot
continue, the process exits 2 and state stays durable.

## 2. Controller is killed mid-chunk

The chunk counters and the checkpoint share one SQLite transaction. On restart
the chunk is either fully present (and skipped) or absent (and re-ingested).
There is no half-applied aggregate.

## 3. Controller is killed between HTTP apply and local commit

On restart, `reconcile_history` sees the proposal id in the simulator's config
history and marks the local proposal `applied`. It does not apply again.

## 4. Controller is killed between `POST /approvals` and local insert

On restart, `reconcile_approvals` finds the approval in the simulator. If it can
be matched to a local proposal it is linked; if not, it is recorded as
`approval_orphan` and not applied blind. A duplicate `409 duplicate_proposal_id`
is adopted rather than retried with a new id.

## 5. Approval expires or lapses

The controller records the outcome with the simulator's reasons, closes the
proposal, and does not resubmit the same unchanged change more than
`max_proposal_attempts` times. The counterfactual (what would have happened) is
stored as hypothetical, `observed: false`.

## 6. Supervisor edits config mid-experiment

The edit is adopted as the live baseline, ledgered, and the affected study is
closed. Proposals touching those dimensions are blocked for 12h, and the
dimension is never re-opened for exploration. If the edit arrives between
proposal and apply, the base version check refuses the apply (`stale`).

## 7. Supervisor ships a bad prompt

The prompt incident path runs on the current version, detects the hallucination
spike from the next day's aggregate, gets candidates from `/llm/suggest-fixes`,
validates them all, and proposes the safest one with the best observed
engagement. The change still needs a human approver. The safety fix is allowed
past the supervisor cooldown because it is new evidence about a live change.

## 8. Caller ID collapses

Recent pickup is compared to the longer baseline. If a healthy pool exists the
bad ID is removed and its mass spread over the pool. If every option is
compromised, the controller keeps the live ID unless a replacement beats it by
5%; it records `incident_did_observed` and spends no approver attention on a
rotation to something worse.

## 9. Every caller ID is historically flagged

The healthy pool is empty. The controller re-admits the option with the best
last observed pickup, compares it to the current ID with the 5% margin, and
falls back to keeping the live ID. This was the direct fix for the public_02
harmful rotation.

## 10. A challenger is a false winner

The promotion gate needs minimum sample, a practical-effect floor, significance
at Bonferroni alpha, a guardrail check, and two consecutive confirmations. If a
false winner still gets promoted, the 15% control arm lets `detect_drop` see the
decay and roll back.

## 11. Experiment never reaches a winner

After 5 days the study closes with `no_winner` and the live split is left
unchanged. The controller does not churn back to 100% baseline only to
re-randomise. A new study is created only if the dimension is not
supervisor-set and has not just been targeted by a cohort win.

## 12. Cohort discovery is a false positive

A feature cell must first pass on cumulative data, then be re-tested on data
after the discovery day, and must be at least 4% of leads. Only then is a cohort
plan built. Plans are retried every evaluation tick so an approver-hours deferral
does not lose a real win.

## 13. Credits run low mid-incident

`credit_reserve = 20` is checked before validation runs. The controller will
not start a repair it cannot finish; it records the reservation and leaves the
incident open for a human. Validation results are reused where possible.

## 14. Two changes race

A pending approval for a workflow stops a second request for the same workflow.
Base-version checks stop an approved change from overwriting a newer edit. The
apply result is read back and mirrored.

## 15. Clock jumps or world changes

If `scenario` or `end_minute` differs, or the clock went backwards,
world-scoped aggregates are cleared and a `world_reset` decision is written.
The ledger is append-only and survives. This prevents one scenario's aggregates
poisoning another's decisions.

## 16. Malformed event

A malformed line increments `ingestion_metrics.malformed` and is skipped. It
does not kill the chunk. The count is visible in `python -m controller metrics`
and `docs/LOAD_TEST.md` records the public_03 value.

## 17. Disk full / SQLite locked

The process logs the error and exits with a non-zero code rather than silently
dropping a decision. State is durable; a restart resumes. SQLite is configured
with a busy timeout so a short lock does not fail a whole tick.
