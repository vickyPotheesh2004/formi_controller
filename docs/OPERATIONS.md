# Operations

## Running

```bash
pip install -r requirements.txt

# 1. Start a simulator (one run per run dir).
python -m sim serve --scenario scenarios/public_01_dev.yaml --run-dir runs/mine01 --port 8700

# 2. Start the controller. It runs until the scenario finishes.
./run.sh http://127.0.0.1:8700

# 3. Grade.
python -m sim grade --run runs/mine01 --baseline runs/base01 --out reports/public_01.md
```

On Windows without a bash shell, the equivalent is:

```powershell
python -m controller run --url http://127.0.0.1:8700 --state state/mine01
```

`run.sh` only calls `python -m controller run --url "$SIM_URL" --state "${CONTROLLER_STATE_DIR:-state}"`.
It has no build step and no manual follow-up.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `CONTROLLER_STATE_DIR` | `state` | Directory for `controller.db` and `controller.jsonl`. |
| `CONTROLLER_DB` | `<state>/controller.db` | Explicit SQLite path. |
| `CONTROLLER_LOG` | `<state>/controller.jsonl` | Explicit JSONL log path. |
| `CONTROLLER_STEP_MINUTES` | `60` | Simulated minutes per loop tick. |
| `CONTROLLER_MAX_RETRIES` | `6` | HTTP retry budget. |
| `CONTROLLER_TIMEOUT_S` | `180` | Per-request timeout. |
| `CONTROLLER_CREDIT_RESERVE` | `20` | Credits the incident path will not spend. |
| `CONTROLLER_SUPERVISOR_COOLDOWN` | `720` | Minutes a supervisor-set dimension is blocked. |
| `CONTROLLER_EVAL_INTERVAL` | `120` | Minutes between experiment evaluations. |
| `CONTROLLER_SCHEDULE_INTERVAL` | `720` | Minutes between schedule-learning runs. |
| `CONTROLLER_MIN_ARM_CONNECTED` | `300` | Minimum connected calls per arm. |
| `CONTROLLER_MIN_COHORT_ARM_CONNECTED` | `120` | Minimum connected calls per cohort arm. |
| `CONTROLLER_PROMOTION_CONFIRMATIONS` | `2` | Consecutive passing evaluations. |
| `CONTROLLER_EXPLORATION_SHARE` | `0.60` | Challenger traffic mass. |
| `CONTROLLER_CONTROL_SHARE` | `0.15` | Permanent control after promotion. |

All thresholds are documented in `docs/DECISION_ENGINE.md`.

## CLI

```bash
# Full audit trail for a workflow, newest decisions last.
python -m controller decisions --state state/mine01 --workflow ks_cancel --limit 20

# One decision, with its evidence, sources, policy and lifecycle.
python -m controller decisions --state state/mine01 --type proposal_submission --json

# Consolidated supervisor view (JSON, or Markdown to stdout).
python -m controller report --state state/mine01 --json
python -m controller report --state state/mine01 --out state/mine01

# KPIs, ingestion counters, credits, per-workflow totals.
python -m controller metrics --state state/mine01

# Fast invariants without a simulator.
python -m controller selftest
```

Every loop log line is JSON, so it can be filtered directly:

```bash
jq 'select(.level=="ERROR")' state/mine01/controller.jsonl
jq 'select(.workflow_id=="ks_cancel") | {ts,message}' state/mine01/controller.jsonl
```

## Supervisor view

`python -m controller report --out <dir>` writes `report.json` and `report.md`.
The view is built from the controller's own store, not the simulator logs. It
contains per-workflow KPIs, active studies, pending approvals, incidents,
recent decisions, credit spend, and the decision count. It is one page, not
fifty alerts.

## Alerts

Page on:

* **ingestion stalled** — no new chunk committed for more than two loop ticks
  while the simulator clock is moving; decisions are now stale.
* **approval backlog** — a proposal still `approval_pending` within two hours of
  its `expires_at`; a human needs to look before it lapses.
* **credit reserve breached** — `credits_remaining < CONTROLLER_CREDIT_RESERVE`;
  the next incident may be unfixable.
* **repeated apply conflict** — more than two `stale_base_version` outcomes for
  the same workflow in a day; the controller and a human are racing.
* **cloud of errors** — the same `5xx` from the simulator retried to exhaustion;
  the loop is degraded.

Do **not** page on:

* a single proposal rejection or expiry (normal approver behaviour, already
  ledgered);
* a single candidate refusing promotion for insufficient sample (expected early
  in a study);
* a closed study with no winner (the live split is unchanged; this is the design
  working);
* a dropped loser (the controller removed a statistically worse variant on
  purpose).

## Recovery

Kill the process at any point and restart it with the same `--state`:

```bash
python -m controller run --url http://127.0.0.1:8700 --state state/mine01
```

What happens:

* committed chunks are not replayed; only the uncommitted tail is ingested;
* `submitting` proposals are closed from simulator config history if the apply
  already landed;
* pending approvals are re-polled; an approval seen in the simulator but missing
  locally is recorded as an orphan, not re-requested;
* learning state, promotions, cooldowns and the ledger survive because they live
  in SQLite.

If the simulator world changed (`scenario` or `end_minute` differs, or the clock
went backwards), world-scoped aggregates are cleared and a `world_reset` ledger
entry is written. The decision ledger itself is never deleted.

## Load and sizing

The store is counter-based, not event-based. At the public_03 rate (12
workflows, ~650k dials/day, ~850k events/day, 5 days) the controller completed
the run within the 8 GB envelope. The measured database was 411 MiB and peak
RSS was approximately 630 MB; raw events are not stored, but aggregate indexes
are material at this scale. Measured numbers are in `docs/LOAD_TEST.md` and
`reports/public_03.md`.

For a 10x increase, partition ingestion by workflow or tenant, run one
evaluation loop per partition, and keep the decision store per tenant. The
statistical and governance logic does not need to change.
