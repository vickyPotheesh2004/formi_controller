# Architecture walkthrough

A guided path through one real decision: how an event becomes a config change.
Use it to onboard onto the code, or to answer "show me why X changed".

## 0. Start

```bash
./run.sh http://127.0.0.1:8700
```

`run.sh` → `controller.main.cmd_run` → `Engine.run()`.

## 1. Bootstrap (`engine.bootstrap`)

1. Wait for `/health`.
2. `GET /sim/state` — scenario name, end minute, current minute, credits.
3. `_check_world` — if the scenario or end minute changed, or the clock went
   backwards, clear world aggregates and write `world_reset`; keep the ledger.
4. `GET /workflows` — the public contract for every workflow. Store it in
   `workflows` and `workflows.public_json`.
5. Build `Governance` and `Ingestor`.
6. Per workflow: `reconcile_history` (finish a crashed apply if the simulator
   config history has the proposal id), then `sync_config` (mirror current
   config and version).
7. `reconcile_approvals` — adopt approvals that exist but are not local.
8. `ingest_pending` — read history up to D0.
9. `_sync_supervisor_edits` — adopt any supervisor versions already present.

## 2. One loop tick (`engine.run` → `_after_advance`)

```
GET /sim/state
POST /sim/advance(step)          # default 60 simulated minutes
  ├─ ingestor.ingest_pending()   # /events/chunks, /events/chunks/{n}
  ├─ _sync_configs()             # /workflows/{id}/config, /config/history
  ├─ _sync_supervisor_edits()    # detect actor=='supervisor' versions
  ├─ governance.poll_and_apply() # /approvals/{aid}, /workflows/{id}/apply
  ├─ _incident_tick()            # caller ID, then prompt
  ├─ _evaluation_tick()          # every 120 min: studies, cohorts, drift
  ├─ _schedule_tick()            # every 12 h
  └─ _snapshot()                 # every 6 h: supervisor report
```

## 3. Ingestion (`ingestion.Ingestor`)

* `GET /events/chunks` returns new chunk numbers.
* Each chunk is fetched once and parsed into `Aggregator` counters keyed by
  workflow, day, hour, cohort, variant, caller ID, prompt, feature, person.
* One SQLite transaction writes the counters **and** the `chunks` checkpoint.
* Replaying a committed chunk is a no-op.
* Malformed lines increment `ingestion_metrics.malformed`; the chunk still
  commits.

## 4. Detection (`engine._incident_tick`)

### Caller ID (`incidents.detect_did_incidents`)

1. For each `caller_id` option, read `did_daily` for today (or the last two days
   if today has fewer than 200 dials).
2. Compare to the longer baseline (at least 800 dials).
3. Flag on a 15% relative drop with a significant difference (`z = 2.5`).
4. Build the healthy pool: not currently flagged, not historically flagged for
   this workflow.
5. If a clean option exists, remove the bad ID from the allocation and spread
   its weight over the pool.
6. If every option is compromised, keep the live ID unless the best alternative
   beats its last observed pickup by 5%; otherwise record
   `incident_did_observed` and do nothing.

A proposed rotation goes to `_propose(..., kind="incident_did_rotate")`.

### Prompt (`engine._prompt_tick`)

1. `prompt_daily` recent vs history for the live prompt, plus the 2% absolute
   line; at least 150 connected calls.
2. Open an incident on a significant spike.
3. `POST /llm/suggest-fixes` — suggestions only order the queue.
4. `POST /validation/runs` for each candidate (300 calls, credits reserved).
5. Among candidates above `min_validation_pass_rate`, pick the best observed
   engagement history; ties break on pass rate then declared option order.
6. Propose with `allow_auto=False` and past the supervisor gate (safety fix).

## 5. Experiment evaluation (`engine._evaluation_tick`)

1. `experiments.ensure_studies` — one study per `welcome_message` / `voice` for
   engagement-primary workflows; never a supervisor-set dimension.
2. `mark_started` — when the live default allocation matches the study split.
3. `evaluate`:
   * arm stats from `variant_daily` since the study started;
   * `stats.evaluate_candidate` with Bonferroni alpha and the practical floor;
   * guardrail check on the other KPI;
   * two consecutive passes → promotion; two significant negative evaluations →
     drop the loser and block it for 3 days;
   * feature scan once/day after 20:00, discovery then independent confirmation;
   * decay check for promoted winners.
4. `_apply_cohort_plan` — retried every tick so an approver-hours deferral does
   not lose a confirmed plan.
5. `_sync_study_allocation` — propose the declared split so the live traffic
   matches the design.

## 6. Governance (`governance.Governance.propose`)

```
validate → dedupe → policy check → auto path or approval path
```

* `normalise_config` mirrors the documented hard rules.
* `changed_dims` / `max_shift` / `auto_apply_check` decide auto vs approval.
* Auto path: `POST /workflows/{id}/apply` with the proposal body; on
  `409 duplicate`, reconcile from history.
* Approval path: approver hours, pending, caps, cooldown, prompt evidence.
  `POST /approvals`. On `409 duplicate_proposal_id` adopt the existing approval;
  on `409 stale_base_version` set a cooldown.
* Every branch writes a ledger decision or lifecycle entry.

## 7. Apply and observe

`poll_and_apply`:

* `approved` → if base version changed, `stale` and stop; else
  `POST /workflows/{id}/apply`, mirror the version, snapshot the old config.
* `applied` (crash case) → reconcile, never re-apply.
* `rejected` / `expired` / `lapsed` → close, ledger the reason, cooldown.

After apply, the next ticks keep measuring the arm. A promoted winner that
decays triggers a rollback proposal; a dropped loser is blocked.

## 8. Audit

Every decision above is a row in `decisions` with a concrete source list,
evidence, alternatives, selected action, expected impact, risk, approval status,
execution status, and result reference. Lifecycle stages are appended with
`correlation_id` pointing at the original decision, so no row is ever rewritten.

```bash
python -m controller decisions --state state/mine --workflow ks_cancel --limit 20
python -m controller decisions --state state/mine --type supervisor_edit --json
```

Read it as: what happened, why, from where, what else was possible, who approved,
did it apply, and what happened next.
