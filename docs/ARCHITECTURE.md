# Architecture

The controller is a single Python process. It speaks only HTTP to the simulator,
keeps all of its own state in SQLite, and never imports `sim` or reads scenario
files, run directories, hidden truth, or grader internals.

```
                 HTTP (documented contract only)
  ┌────────────────────────────┐        ┌──────────────────────────────────┐
  │ simulator (sim serve)      │◄──────►│ controller                       │
  │ /sim/state /sim/advance    │        │                                  │
  │ /events/chunks /workflows  │        │  api.SimClient  (retry/backoff)  │
  │ /approvals /validation/runs│        │                                  │
  └────────────────────────────┘        │  engine.Engine                   │
                                       │   ├─ ingestion.Ingestor           │
                                       │   ├─ governance.Governance        │
                                       │   ├─ experiments                  │
                                       │   ├─ incidents                    │
                                       │   ├─ schedule                     │
                                       │   └─ report                       │
                                       │                                  │
                                       │  state/controller.db (SQLite)     │
                                       │  state/controller.jsonl (logs)    │
                                       └──────────────────────────────────┘
```

## Loop

`Engine.run()` bootstraps once, then repeatedly:

1. `GET /sim/state`, compute a step (default 60 simulated minutes, rounded down
   to the simulator tick), `POST /sim/advance`.
2. Ingest every new `/events/chunks` chunk.
3. Sync each workflow's current config and config history.
4. Adopt any supervisor edits.
5. Poll and apply pending approvals.
6. Run the incident tick (caller ID, prompts, engagement drift).
7. Every 120 simulated minutes: the experiment evaluation tick, including
   cohort discovery and the prompt incident path.
8. Every 12 simulated hours: the schedule-learning tick.
9. Every 6 simulated hours: write a supervisor snapshot.

Every action that can change a client's agent goes through `Governance`.
Every observation that supports a decision is appended to the ledger.

## Components

| Module | Responsibility |
|---|---|
| `api.py` | The only HTTP client. Retries 5xx/transport errors with backoff, honours the documented endpoints, surfaces 4xx as `SimError`. |
| `ingestion.py` | Chunk-transactional, idempotent aggregation. The chunk checkpoint commits in the same transaction as its counters, so a crash commits all or none and replay is skipped. |
| `db.py` | SQLite schema, aggregate tables, append-only triggers on `decisions`, transactions, bounded analytics reset. |
| `analytics.py` | All metric queries. Rates return `None` when the denominator is zero; values are never fabricated. |
| `cohorts.py` | The single source of truth for the config shape: validation, changed-dimension diff, max shift, cohort matching, allocation design, auto-apply policy check. |
| `stats.py` | Two-proportion tests, unpooled difference CIs, Bonferroni alpha, minimum sample and practical-effect gates, guardrail regression, smoothed rates. |
| `experiments.py` | Study design, deterministic evaluation, promotion/demotion, decay detection, feature-cohort discovery, cohort plans. |
| `incidents.py` | Caller-ID collapse detection and healthy-pool rotation, prompt hallucination detection/validation/selection, engagement drift. |
| `schedule.py` | Bounded hour weights and (gated) per-person preferred-hour learning. |
| `governance.py` | Proposal lifecycle, approval polling, auto-apply, stale-base handling, crash reconciliation, rollback snapshots. |
| `ledger.py` | Append-only decision ledger and provenance helpers. Counterfactuals are a separate labelled table. |
| `report.py` | The consolidated supervisor JSON/Markdown view, built from the controller's own store. |
| `engine.py` | Wiring and the loop. Owns no statistics and no governance policy. |
| `main.py` | CLI: `run`, `decisions`, `report`, `metrics`, `selftest`. |

## State

Everything durable is in `state/controller.db` (override with
`CONTROLLER_STATE_DIR`, `CONTROLLER_DB`, or `--db`):

* `chunks` — chunk checkpoint (idempotency).
* `config_versions` — mirrored config history, including the simulator's
  `proposal_id` / `approval_id` when present.
* Aggregate counters: `variant_daily`, `feature_variant_daily`, `hour_feature_daily`,
  `did_daily`, `prompt_daily`, `cohort_hour_daily`, `concluded_daily`,
  `person_hour`, `lead_daily`, `campaigns`, `lead_features`.
* `proposals`, `approvals`, `credit_events`, `incidents`, `rollback_snapshots`,
  `workflow_state`.
* `experiments` / `cohorts` (learning state is stored in `workflow_state.notes_json`).
* `decisions` and `counterfactuals` — append-only; UPDATE and DELETE are blocked
  by triggers.

`state/controller.jsonl` holds one JSON object per loop log line.

### Restart

On restart the process reconnects, reads `/sim/state`, and:

* replays only chunks it has not committed;
* marks any local `submitting` proposal as `applied` if the simulator config
  history already contains that proposal id (a crash after the HTTP apply but
  before the local commit);
* adopts an approval found in the simulator but missing locally instead of
  creating a duplicate request;
* keeps the learning state, promotions, cooldowns, and ledger.

The world is identified by `scenario` + `end_minute`. A different world (or a
rewound clock) clears world-scoped aggregates and resets supervisor-edit
bookkeeping, but the decision ledger is never deleted.

## Why SQLite aggregates

Raw events at 850k/day would grow without bound and make every decision query a
scan. The controller keeps only the counters needed for detection, experiments,
scheduling, the supervisor view, and the ledger. A chunk is one transaction, so
the store stays consistent under crash and restart, and the database stays small
enough to run on the 4 vCPU / 8 GB target.
