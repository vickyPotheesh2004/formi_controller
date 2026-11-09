# Interview guide

A 60-minute review aid: the choices worth defending, the questions likely to
come up, and where the code answers them.

## The one-minute version

The controller is an HTTP client plus a SQLite decision store. It ingests
simulator events into bounded aggregates, detects caller-ID/prompt/drift
incidents from populations, runs deterministic experiments on welcome and voice,
routes every config change through approval or policy-permitted auto-apply, and
writes every decision to an append-only ledger. It never reads scenario files or
simulator internals.

## Likely questions

**"Why SQLite aggregates instead of an event store?"**
At 850k events/day, raw events make every decision query a scan and grow without
bound. The controller only needs counters for its decisions and its audit. A
chunk commits its counters and checkpoint in one transaction, so crash recovery
is exactly-once. See `controller/ingestion.py`, `controller/db.py`.

**"Is the statistics sound?"**
Two-proportion test, unpooled CI, Bonferroni for multiple candidates, a 5%
practical-effect floor, a 300-connected minimum, a guardrail check, and two
consecutive confirmations. It is fixed-horizon frequentist, not sequential; that
limitation is stated in `docs/DECISION_ENGINE.md`. The repeated confirmation and
floor are the defence against a lucky peek.

**"How do you avoid false cohort discovery?"**
Discover once/day on cumulative data, then re-test on data collected after the
discovery day, require 4% of leads and 120 connected per arm, cap at 8 cohorts,
and close the global study on a win. See `experiments._feature_scan`.

**"What stops the controller overwriting a supervisor?"**
Supervisor edits are adopted as baseline immediately, ledgered, block the same
dimensions for 12h, close affected studies, and freeze the dimension from future
exploration. Base-version checks refuse a stale approved apply. See
`engine._sync_supervisor_edits`, `governance._apply_approved`.

**"Why did public_02 have a harmful voice change before?"**
A supervisor set voice V2. The controller closed the study and then re-opened
exploration after the cooldown, diluting V2 to 40% and losing engagement. The fix
is the supervisor-dimension freeze. `tests/test_controller.py::test_ensure_studies_does_not_reopen_a_supervisor_set_dimension`.

**"How do you choose a prompt fix?"**
The LLM only orders the validation queue. Validation is a safety gate. Among
safe candidates the controller prefers proven engagement history, then pass rate,
then declared option order. Prompt changes always go to a human. See
`incidents.choose_replacement_prompt`.

**"What happens if every caller ID is bad?"**
The healthy pool excludes historically flagged IDs. If it is empty, the
controller keeps the live ID unless an alternative's last observed pickup beats
it by 5%; otherwise it records the compromise instead of rotating to a worse ID.
`incidents._healthy_pool`, `_better_alternative`.

**"How is approver attention protected?"**
Approver hours, one pending per workflow, `max_requests_per_24h`, a global daily
cap of 6, cooldowns, a policy TTL capped at 24h (not a hard 12h), and a
two-attempt limit on unchanged resubmissions. `governance._request_approval`.

**"What breaks at 10x?"**
Ingestion is the first bottleneck: one SQLite writer and one Python process.
Partition by tenant/workflow, run one evaluation loop per partition, keep a
per-tenant decision store. The statistical and governance modules are
partition-safe. See `docs/LOAD_TEST.md`.

**"What did you cut?"**
A UI, real-time streaming (chunk polling is enough), full CUPED/historical
dry-run replay, multi-armed bandit allocation, and per-person scheduling at high
dial rates. Honestly labelled in `docs/TRADEOFFS.md` and the submission.

**"Show me why workflow X changed at time T."**
`python -m controller decisions --state <state> --workflow X` prints every
decision with evidence, sources, policy, alternatives, approval, execution and
lifecycle. The ledger rejects updates and deletes by trigger.

## Where to look

| Question | File |
|---|---|
| Loop and wiring | `controller/engine.py` |
| HTTP contract | `controller/api.py` |
| Ingestion and idempotency | `controller/ingestion.py` |
| Schema and triggers | `controller/db.py` |
| Metrics | `controller/analytics.py` |
| Stats | `controller/stats.py` |
| Cohorts and config rules | `controller/cohorts.py` |
| Experiments and decay | `controller/experiments.py` |
| Incidents | `controller/incidents.py` |
| Schedule | `controller/schedule.py` |
| Governance | `controller/governance.py` |
| Ledger | `controller/ledger.py` |
| Supervisor view | `controller/report.py` |
| CLI | `controller/main.py` |

## What I would improve next

1. A sequential test (always-valid CI) to replace fixed-horizon peeking.
2. Per-tenant ingestion partitions for 10x.
3. A proper historical dry-run replay for impact scores instead of the honest
   "no replay available" placeholder.
4. A small supervisor UI over the existing report API.
5. More end-to-end scenarios for prompt incidents and cohort wins.
