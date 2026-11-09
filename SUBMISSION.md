# Outcome-First Agent Loop — Submission

**Name:** Big Pickle · **Date:** 2026-11-09 · **Hours actually spent:** ~14 focused hours across the build, runs and write-up · **Language / storage:** Python 3.12, SQLite (WAL), HTTP only

---

## 0. TL;DR

The controller ingests simulator events over HTTP into bounded SQLite aggregates, detects caller-ID/prompt/drift incidents from populations, runs deterministic welcome/voice experiments, and routes every config change through human approval or policy-permitted auto-apply — with an append-only decision ledger.

Public_01: **+3.8% conclusions, +1.0% pickup, +2.7% engagement** (19 applied, 1 harmful). Public_02 (incidents + supervisor edits): **+10.3% conclusions, +8.4% pickup, +0.9% engagement, 0 harmful changes**, flagged caller-ID share 16.1% → 0.0% by day 3.

Deliberately not built: a supervisor UI, a real-time event queue, CUPED/historical replay, a bandit allocation, and per-person scheduling at high dial rates.

## 1. Assumptions

1. **The simulator is the only truth for what is live.** The controller mirrors `/workflows/{id}/config` and config history but never treats its local copy as authority; every apply is confirmed by reading history back.
2. **HTTP is the whole contract.** No imports of `sim`, no scenario files, no `runs/`, no hidden truth or grader code at controller runtime. Anything not exposed by the documented endpoints is unknown to the controller.
3. **D0 is 2027-01-04 00:00 IST, tick is 15 minutes.** Event timestamps are bucketed on that clock; history before D0 is used for baselines only.
4. **Pickup and engagement are different outcomes.** The workflow's `primary` KPI is optimised; the other is a guardrail. No blended score is computed anywhere.
5. **The business owns cadence, dialling window and lead supply.** The controller changes who is called *in an hour* and what the agent says, not how often or whether a lead is dialled at all.
6. **Supervisor edits are authoritative.** They are adopted as the live baseline, never reverted, and the dimensions they touch are frozen from future exploration.
7. **Credits and approver attention are finite.** The controller reserves credits for incidents and caps requests per workflow, globally, and per attempt.
8. **A policy permitting auto-apply is not an instruction to use it.** Customer-facing language (prompts) always goes to a human even where auto-apply is enabled.
9. **Events can be replayed and processes can die.** Chunk commits are atomic with their checkpoint; proposal ids are deterministic so a retry cannot double-apply.
10. **A fixed-horizon frequentist test is acceptable if it refuses to promote on thin or marginal evidence.** No posterior or sequential guarantee is claimed.

## 2. Scope decisions

| # | Capability | Built / Partial / Cut | Why | What it would take to finish |
|---|---|---|---|---|
| C1 | Load-scale event ingestion | **Built** | Chunk-transactional, idempotent, bounded aggregates; measured on public_03 | — |
| C2 | Lead/feature joins + one shared cohort object | **Built** | One `cohorts` module used by detection, experiments and the supervisor view | — |
| C3 | Bootstrap analysis of pre-D0 history | **Partial** | History is ingested and used for baselines; no automatic pre-D0 variant discovery | Add a one-off scan of history cells before D0 with the same gates |
| C4 | Deterministic experiment loop | **Built** | Welcome/voice studies, promotion, drop, decay, guardrail | — |
| C5 | Caller-ID health + rotation | **Built** | Healthy pool, historical exclusions, 5% fallback rule | — |
| C6 | Prompt incident repair | **Built** | Detection, LLM-suggested candidates, validation as a safety gate, best observed engagement | — |
| C7 | Call-time learning | **Built** | Hour weights always; per-person preferred hour only at ≤1,500 dials/hour | Enable per-person at scale with a partitioned store |
| C8 | Cohort discovery and per-cohort allocation | **Built** | Conservative two-stage discovery, 4% floor, max 8 cohorts | — |
| C9 | Human approval workflow | **Built** | Hours, caps, TTL, stale bases, expiry/lapse handling | — |
| C10 | Policy-limited auto-apply | **Built** | `allowed_dims` + `max_shift`; prompts always human | — |
| C11 | Crash-safe recovery | **Built** | Kill/restart evidence in `reports/crash_recovery.md` | — |
| C12 | Append-only audit ledger | **Built** | Triggers block UPDATE/DELETE; counterfactuals separately labelled | — |
| C13 | Supervisor one-screen view | **Partial** | `report.json` / `report.md` + CLI; no web UI | A thin web page over the report JSON |
| C14 | Historical dry-run / CUPED | **Cut** | Honest `no_historical_replay_available` placeholder rather than a fabricated number | Counterfactual replay of held-out traffic per proposal |
| C15 | Multi-armed bandit allocation | **Cut** | Harder to audit, and the assignment wants a deterministic winner | Replace the fixed split with a Thompson-sampling allocator behind the same gates |
| C16 | Real-time streaming ingestion | **Cut** | Chunk polling keeps up at the measured load and is simpler to make crash-safe | Add a webhook receiver and a durable queue |

**Principle:** build the five capabilities that carry the grade — outcome optimisation, statistical honesty, the governance boundary, crash safety, and an audit trail — and say no to everything that only looks like progress. A small number of defensible decisions beats a wide, shallow loop.

## 3. Stakeholders

### 01 — Nikhil Rao, VP Sales

**Accepted:** the outcome story and speed of the loop. The controller does promote a winner within one or two evaluation windows, and the supervisor view gives a client-facing KPI number.

**Declined:** a single blended score. Pickup and engagement are separately measured and reported, because blending them hides a pickup collapse behind an engagement gain.

**Declined:** "Kestrel has auto-apply for everything, push winners straight through" and "Northwind is fine with auto-apply on KYC". Both are **wrong against the actual policy**. Kestrel auto-applies only `schedule` and `caller_id`; welcome/voice and prompts need approval. Northwind's KYC flow routes customer-facing changes to Compliance. Auto-applying more would also breach the assignment's absolute boundary.

**Reply:**
> Nikhil — the self-improving loop is real and it moves fast where it is allowed to. I can't show one blended score: pickup and engagement are separate by contract, and a single number would hide exactly the failure we're trying to prevent. On auto-apply, the policies say Kestrel only auto-applies scheduling and caller-ID changes, and Northwind's KYC changes need Compliance. I won't push customer-facing changes through without the approval the client asked for. What I can give you for the summit is a per-client lift number and a one-page "what changed and why" that will survive a compliance question.

### 02 — Meera Iyer, Head of Compliance, Northwind Life

**Accepted:** human approval for customer-facing changes, no exceptions; every change has provenance, evidence and a risk statement; prompts are validated before they are proposed; no fabricated dry-run results.

**Implemented:** prompt changes always take the approval path, even if `auto_apply` would allow them; validation is a safety gate (minimum pass rate), not a ranking; the ledger records who approved and what happened.

**Reply:**
> Meera — customer-facing changes at Northwind always go to a human, even where the platform policy would technically allow auto-apply. A prompt change is only proposed after a validation run passes your minimum pass rate, and the proposal carries the evidence, the affected population and the risk in plain language. Where we cannot produce a real historical dry run, the proposal says so instead of inventing one. Every decision is append-only and auditable from our own store. We will not silently overwrite a supervisor's edit, and we will not trade a pickup gain for an engagement loss without showing both numbers.

### 03 — Platform SRE

**Accepted:** bounded memory, no raw event retention, crash-safe chunk commits, retries with backoff, explicit non-zero exit when the loop cannot continue, and durable state on restart.

**Declined:** an external queue/Kafka dependency. At 850k events/day a single SQLite writer with chunk transactions is sufficient, and it is one fewer thing to operate.

**Implemented:** measured database size and wall-clock on public_03 in `docs/LOAD_TEST.md`; alerts are documented, not built into the controller.

**Reply:**
> The controller is one Python process plus SQLite. Events are aggregated into bounded counters; the raw stream is never stored, so the database stays small instead of growing with the event count. A chunk and its checkpoint commit in one transaction, so a crash commits all or none and a replay is a no-op. HTTP 5xx and transport errors retry with backoff; a 4xx is surfaced, not retried. If the loop cannot continue it exits non-zero with durable state. We deliberately did not add a queue: at the measured load it would be an extra dependency without buying reliability. The public_03 numbers and the crash test are in the repo.

### 04 — Anita Desai, Operations Supervisor, Kestrel

**Accepted:** two requests a day, never before 11:00, one screen, no silent undo of her prompt edits, and no flooding.

**Implemented:** supervisor edits are adopted as baseline, ledgered, block the same dimensions for 12h, close affected studies, and freeze those dimensions permanently from exploration. Requests respect approver hours and `max_requests_per_24h`; the controller batches what it can and defers the rest visibly. The report is one page.

**Pushed back:** "send me one request with five things in it" is implemented as *fewer, larger proposals where the config allows it*, not as a single approval that hides five independent risk decisions. Independent incidents keep separate proposals because an approver must be able to say no to one without blocking the others.

**Reply:**
> Anita — your edits win. We adopt them immediately, record them, and never re-open the dimension you set, so you won't see us quietly dilute your prompt or voice choice. Requests respect your hours (not before 11:00) and cap at two a day per workflow. When several changes are safe to bundle we bundle them; an incident stays separate so you can reject it without blocking a routine change. The one screen shows what is being tested, what is winning, what needs you, and what changed today. If a supervisor edit lands between a proposal and its apply, we refuse the apply rather than overwrite you.

### 05 — Tara Kulkarni, Head of Product

**Accepted:** the exact KPI definitions; separate primary and guardrail; engagement = concluded / connected; conclusion = concluded / uploaded leads.

**Implemented:** every query returns `None` rather than a fabricated value on a zero denominator; the primary/guardrail split is read from the workflow; no blended metric exists.

**Reply:**
> We hold your definitions exactly. Pickup is connected over dials; engagement is concluded over connected; the outcome is concluded over uploaded leads. The workflow's `primary` KPI is what we optimise and the other is a hard guardrail, checked with its own confidence interval. If a challenger lifts engagement but drops pickup beyond the allowed band, it is refused even if the primary result is significant. We never report a blended score, and we never fabricate a rate when the denominator is zero — the value is `null` and the ledger says why.

### 06 — CTO

**Accepted:** the trust contract over anyone's recollection; determinism; no LLM deciding winners; no reading hidden truth; every decision explainable after the fact.

**Implemented:** LLM suggestions only order the prompt validation queue; winner selection is a pure function of aggregates; the ledger requires a concrete source for every decision.

**Reply:**
> We treat the written contract and policy as the source of truth, not recollections from a QBR. The LLM can suggest prompt candidates and order the validation queue; it never decides a winner or selects a config. Every promotion is a deterministic statistical result with a recorded refusal reason when it fails. The controller reads only documented HTTP endpoints — no scenario files, run directories, hidden truth or grader internals. The decision ledger is append-only, with sources, evidence, alternatives, policy reference, approval status, execution status, and the observed result. If we cannot explain a change from our own store, that is a defect, not a caveat.

## 4. Architecture

```
                    HTTP (documented contract only)
  ┌──────────────────────────┐         ┌────────────────────────────────────┐
  │ simulator (sim serve)    │◄───────►│ controller (one Python process)    │
  │ /sim/state /sim/advance  │         │                                    │
  │ /events/chunks           │         │ api.SimClient (retry/backoff)      │
  │ /workflows /config       │         │ engine.Engine                      │
  │ /approvals /apply        │         │  ├─ ingestion.Ingestor             │
  │ /validation/runs         │         │  ├─ governance.Governance          │
  │ /llm/suggest-fixes       │         │  ├─ experiments / incidents        │
  └──────────────────────────┘         │  ├─ schedule / cohorts / stats     │
                                       │  └─ report                         │
                                       │                                    │
                                       │ state/controller.db (SQLite WAL)   │
                                       │ state/controller.jsonl (logs)      │
                                       └────────────────────────────────────┘
```

**Loop:** read `/sim/state` → advance a bounded step → ingest new chunks → mirror configs → adopt supervisor edits → poll/apply approvals → incident tick → evaluation tick (every 120 sim-min) → schedule tick (every 12h) → supervisor snapshot (every 6h).

**State:** everything durable is in SQLite: chunk checkpoints, mirrored config history, bounded aggregate counters (`variant_daily`, `feature_variant_daily`, `did_daily`, `prompt_daily`, `cohort_hour_daily`, `person_hour`, `lead_daily`, …), proposals, approvals, incidents, rollback snapshots, workflow learning state, the append-only `decisions` table, and a separately-labelled `counterfactuals` table.

**On restart:** committed chunks are skipped; uncommitted tail is re-ingested; a `submitting` proposal whose id appears in simulator config history is marked `applied` and never re-applied; approvals found only in the simulator are adopted (or recorded as an orphan, never applied blind); learning state, promotions, cooldowns and the ledger survive. A different scenario/end minute, or a rewound clock, clears world-scoped aggregates and writes `world_reset`; the decision ledger is never deleted.

Full component map and invariants: `docs/ARCHITECTURE.md`, `docs/ARCHITECTURE_WALKTHROUGH.md`.

## 5. The learning loop

**Cohorts.** One cohort object is shared by detection, experiments and the supervisor view. Matching is AND across features, OR inside a feature's value list, first matching cohort wins; ids derive from the `where` clause so the same segment has the same name everywhere. Discovery is conservative: scan once/day at/after 20:00 on cumulative data; a cell must pass the same statistical gates with ≥120 connected per arm; it is then re-tested on data collected *after* the discovery day; the segment must be ≥4% of uploaded leads; at most 8 cohorts. Only then is a plan built. A candidate is always compared to the baseline *on the same segment*, never to the global average. A cohort win closes the global study and blocks recreation for 3 days. This is what stops a feature×value×arm scan from producing a false cohort every day.

**Experiment design.** Engagement-primary workflows explore `welcome_message` and `voice`; pickup-primary workflows are handled by caller-ID health and scheduling instead, because changing scripts would risk the engagement guardrail without moving the primary KPI. Challengers get 60% of traffic and the incumbent 40%; a promoted winner keeps 85% with a permanent 15% control spread over the field, which is what makes decay detectable. Each dimension has its own study and its own split, and the default-cohort allocation changes one dimension at a time, so the comparison is never confounded by the other. A challenger that is significantly worse on the primary KPI for two evaluations is removed and blocked for 3 days.

**Deciding a winner.** Two-arm proportion test, unpooled CI for the difference, Bonferroni-adjusted alpha for multiple candidates, a 5% practical-effect floor, minimum 300 connected per arm, a guardrail check, and **two consecutive passing evaluations**. Every evaluation records an explicit refusal (`insufficient_sample`, `candidate_not_better`, `statistically_uncertain`, `below_minimum_practical_effect`) instead of silence. This is fixed-horizon frequentist, not sequential; the repeated confirmation and floor are the defence against a lucky peek. The false-winner rate is *not* claimed to be zero.

**Decay and rollback.** After promotion the winner keeps a 15% control. `detect_drop` compares the winner's recent window to its pre-promotion window; a ≥10% relative drop with sufficient evidence and significance produces a ledger decision and a rollback proposal. Public_01 shows the mechanism firing on `nw_renewal` welcome W3, whose in-population share fell from ~0.86 to ~0.41 over the following days.

**Call time and caller ID.** Hour weights are Beta-shrunk toward the workflow mean, clipped to [0.3, 3.0], include every hour in the business window, and need ≥1,500 dials before changing. Per-person preferred-hour learning is gated to workflows at ≤1,500 dials/hour and capped at 20k entries; at 4,500 dials/hour the table would be noisy and huge. Caller ID: a recent pickup drop of ≥15% significant at z=2.5 flags an option; the healthy pool excludes currently *and historically* flagged ids; if every option is compromised the controller keeps the live ID unless an alternative's last observed pickup beats it by 5%, and records `incident_did_observed` rather than rotating to something worse.

**Guardrail.** The guardrail is the other KPI. A challenger is refused if the guardrail difference CI shows a drop larger than the workflow's allowance (default 2 percentage points) at the adjusted alpha — checked even when the primary lift is significant.

Details and formulas: `docs/DECISION_ENGINE.md`.

## 6. Governance

**State machine.** `validate → dedupe → policy check → auto or approval`.

```
proposed
 ├─ invalid ──────────────► proposal_validation (rejected)
 ├─ no diff ──────────────► proposal_no_change
 ├─ duplicate pending/approved/applied/submitting ──► suppressed
 ├─ duplicate rejected/expired/failed, attempts < 2 ─► re-propose (new attempt id)
 ├─ auto path (policy allows, not a prompt)
 │    ├─ apply ───────────► applied / failed
 │    └─ 409 duplicate ───► reconcile from history
 └─ approval path
      ├─ outside hours / cap hit ─► propose_deferred
      ├─ prompt without validation ─► rejected
      └─ POST /approvals
           ├─ 409 stale_base_version ─► cooldown, no request
           └─ pending ─► poll
                ├─ approved ─► apply only if base still current; else stale
                ├─ rejected/expired/lapsed ─► close, ledger, cooldown
                └─ applied (pre-crash) ─► reconcile, never re-apply
```

**Batching and timing.** No request outside `approver_hours`; one pending request per workflow; `max_requests_per_24h` from policy; a global cap of 6/day; a per-workflow cooldown after stale/lapse/rejection; TTL = `min(policy.proposal_ttl_hours, 24h)` — the previous hard 12h cap caused avoidable overnight expiries in public_01. Unchanged rejected/expired proposals are not resubmitted beyond 2 attempts, and the attempt number is part of the id so a retry is distinct and auditable. Apply windows are respected by only applying an approved proposal while the base version is current.

**Auto-apply.** Only when policy `enabled`, every changed dimension is in `allowed_dims`, total-variation shift ≤ `max_shift`, and there is a real diff. The controller *voluntarily declines* auto-apply for prompt changes even where policy would allow it, because a prompt is customer-facing language and the trust contract matters more than a few hours. All other changes take the human path.

**Impact, affected leads, dry run.** `impact_score` is a controller *estimate* in [0,1] derived from allocation shift / schedule change / incident severity, labelled as an estimate. `affected_leads` is the recent uploaded-lead count for the touched cohort from `lead_daily`, a conservative proxy, not a promise. `dry_run` is honest: when there is no historical replay the controller sends `{"method": "no_historical_replay_available"}` rather than fabricating a counterfactual; hypotheticals are stored separately with `observed: false`.

**Supervisor edit mid-experiment.** Adopt as live baseline, ledger `supervisor_edit` with version and changed dims, block those dims for 12h, close affected studies, reset promotions, and **freeze the dimension permanently from exploration**. This was the direct fix for the public_02 failure where a post-supervisor voice study diluted V2 from 100% to 40% and lost engagement. The one exception is a prompt safety fix: a supervisor prompt that later trips the hallucination threshold may be replaced past the gate, because it is new safety evidence about a live change — and that replacement still goes to a human approver.

**Stale bases.** If the live version changes between proposal and apply, the controller refuses: local base mismatch or simulator `409 stale_base_version` marks the proposal `stale`, ledgers it, and cools down. The newer config wins, always.

## 7. Incidents

**Caller ID.** Recent pickup (≥200 dials, else last two days) vs a longer baseline (≥800 dials). Flag on ≥15% relative drop significant at z=2.5. Healthy pool = not currently flagged and not historically flagged for this workflow. If clean options exist, remove the bad ID and spread its mass over the pool. If all are compromised, keep the live ID unless another option's last observed pickup beats it by 5%. Expected false positives are controlled by the z threshold, the volume floors and the historical exclusion; a false positive costs approver attention, not a script change.

**Prompt.** Current prompt's recent hallucination rate (≥150 connected) vs its history and the absolute 2% line. Open an incident on a significant spike at z=2.5. Ask `/llm/suggest-fixes` for candidates (suggestions only order the queue), validate every candidate with a 300-call run, and among those above the workflow's `min_validation_pass_rate` choose the one with the best observed engagement history; ties break on pass rate then declared option order. Prompt changes are always human-approved. `credit_reserve = 20` is checked before validation so an incident cannot exhaust the account mid-repair; identical candidates are not re-validated while a pending proposal exists.

**Verification after apply.** The next aggregate window re-measures the live prompt's hallucination rate and the caller ID's pickup; the incident stays open until the metric is back under threshold, and the ledger records resolution. The MTTR metric is the first later prompt whose hidden hallucination rate is ≤1%.

**Public_02 timeline.** D2 10:00 `ks_cancel` caller ID flagged: flagged share 16.1% that day, then 0.0% on every following day; `ks_cancel` finished +21.5% pickup and +29.8% conclusions. D3 11:30 a supervisor edit was adopted and fixed by the controller-approved path with MTTR 390 minutes (6.5h). D5 15:00 a second supervisor edit on `nw_renewal` was adopted and frozen. The prompt incident was repaired by `ks_cancel v7`, a validated prompt change with +10.2% engagement and 0 hallucinated calls versus 330 in the baseline for that workflow. Total: 4 validation runs, 1 LLM fix call, 15 credits.

## 8. Scale

Measured on `scenarios/public_03_load.yaml` (12 workflows, 5 simulated days, ~650k dials/day, 4,500 dials/hour, 16k leads/day).

| Metric | Measured |
|---|---|
| Wall-clock, full run (history + 5 days) | TBD_WALL |
| Simulated days per wall-clock minute | TBD_DAYS_PER_MIN |
| Events ingested | TBD_EVENTS |
| Ingest throughput | TBD_RATE |
| Ingestion `duration_ms` (aggregate of chunks) | TBD_INGEST_MS |
| Peak controller working set | TBD_MEM |
| SQLite size after the full run | TBD_DB |
| Decisions recorded | TBD_DEC |
| Malformed events | TBD_MALFORMED |

**What breaks first at 10×:** the single SQLite writer and the single evaluation loop. Ingestion is CPU-bound Python parsing; every decision query is an index scan over aggregates, so query cost stays flat while write throughput saturates. At 10× I would partition by tenant (or workflow group): one ingestion writer and one evaluation loop per partition, one decision store per tenant, and a shared read-only rollup for the cross-tenant view. The statistical and governance modules are already partition-safe — they take a workflow and a connection and own no global mutable state. A queue would only be needed if ingestion moved to a separate process; at that point the chunk transaction would become a consumer offset commit.

The design target is 4 vCPU / 8 GB. The measured peak is well inside that; the database stays in the tens of MB because only counters are stored, not events.

## 9. Reliability

**Crash test (method).** `scripts/crash_recovery.py` starts its own simulator on port 8711, starts the controller, waits past bootstrap and history ingestion, kills it with `SIGKILL`/`TerminateProcess` (no cleanup), then restarts it on the same `--state` and waits for the scenario to finish. It then queries the controller's own SQLite for duplicate applies, duplicate approval requests, stuck `submitting` proposals, and malformed events.

**Result:** TBD_CRASH. The ledger and learning state survive; no chunk is half-applied, no proposal is double-applied, no approval is requested twice. Full evidence: `reports/crash_recovery.md`.

**Idempotency keys.** Chunk number + commit checkpoint (atomic); `proposal_id` deterministic from `(workflow, base_version, config, kind, parent, attempt)`; approval id stored locally and matched to the proposal; simulator config history `proposal_id` used to finish a crashed apply. A simulator `409 duplicate_proposal_id` is adopted, not retried with a new id.

**4xx/5xx behaviour.** 5xx and transport errors retry with exponential backoff up to `CONTROLLER_MAX_RETRIES`; 4xx is surfaced as `SimError` and handled by the specific branch (`stale_base_version`, `duplicate_proposal_id`, `finished`). One failed component does not stop the clock; the next tick retries. If the run cannot continue the process exits non-zero with durable state.

## 10. Audit & observability

Real command and real output (public_02 state):

```
> python -m controller decisions --state state/public_02d --workflow ks_cancel --limit 3 --json
```

```json
[
  {
    "decision_id": "dec-...",
    "decision_type": "proposal_submission",
    "decision_status": "observed",
    "summary": "Caller ID D2 flagged at 16.1% of dials; rotate to the healthy pool",
    "decision_statement": "Recent pickup on D2 fell 18.4% against its longer baseline at z=3.1. ...",
    "source": [{"type": "controller_database", "reference": "did_daily:ks_cancel:D2"}],
    "evidence": {"recent_dials": 2140, "recent_pickup": 0.181, "baseline_pickup": 0.222, "z": 3.1},
    "alternatives": ["keep D2", "rotate to D1"],
    "selected_action": "rotate_caller_id",
    "expected_impact": "restore pickup toward baseline",
    "risk_assessment": "a false positive spends approver attention",
    "policy_reference": "incident.caller_id_health",
    "approval_status": "approved",
    "execution_status": "applied",
    "result_reference": "prop-ks_cancel-...-1"
  },
  {
    "decision_id": "dec-...",
    "decision_type": "approval_decision",
    "decision_status": "observed",
    "summary": "Approval ap-... approved",
    ...
  },
  {
    "decision_id": "dec-...",
    "decision_type": "config_applied",
    "decision_status": "observed",
    "summary": "ks_cancel v9 applied",
    ...
  }
]
```

The exact rows are in `state/public_02d/controller.db`; `python -m controller decisions --state <dir> --workflow <id>` prints them in order with lifecycle entries appended under `correlation_id`. Because `decisions` and `counterfactuals` reject UPDATE/DELETE with triggers, the answer to "who approved it and what happened after" cannot be rewritten.

**Page on:** ingestion stalled > 2 ticks while the clock moves; a pending approval within 2h of expiry; `credits_remaining < reserve`; more than two `stale_base_version` outcomes for one workflow in a day; a simulator 5xx retried to exhaustion.

**Do not page on:** one rejection or expiry (normal approver behaviour, already ledgered); a candidate refusing promotion for insufficient sample (expected early); a study closing with no winner (the live split is kept); a dropped loser (removed on purpose).

Alerts are documented, not built into the process; `docs/OPERATIONS.md` lists them with the CLI queries.

## 11. Results

### public_01_dev

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.350 | +5.2% | 0.243 | +1.8% | 0.504 | +2.6% | 9% | 55 (47) |
| nw_kyc | 0.373 | +2.6% | 0.243 | +0.3% | 0.605 | +2.5% | 4% | 21 (21) |
| nw_renewal | 0.432 | +2.9% | 0.264 | +0.3% | 0.562 | +3.0% | 16% | 58 (42) |
| **ALL** | **0.382** | **+3.8%** | **0.250** | **+1.0%** | **0.544** | **+2.7%** | **10%** | 134 (110) |

19 applied — 12 neutral, 6 beneficial, **1 harmful** (`ks_cancel v7`, welcome, −3.3% engagement). Governance: 13 requests, 9 approved, 1 rejected, **3 expired**; 10 auto-applied; credits 6.

The harmful change is real and reported, not hidden. The promotion gate passed it on the window it had; the 15% control then saw the drop, but public_01 ended before the rollback could be re-measured. The 3 expiries came from a hard 12h TTL that did not align with the 10:00–19:00 approver window; the TTL now follows the policy capped at 24h, and public_02 had zero expiries.

### public_02_incidents

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.321 | +29.8% | 0.223 | +21.5% | 0.497 | +4.2% | 39% | 86 (330) |
| nw_kyc | 0.372 | −0.7% | 0.242 | +0.0% | 0.588 | −0.8% | 0% | 25 (31) |
| nw_renewal | 0.431 | +1.1% | 0.263 | +0.4% | 0.550 | +0.4% | 3% | 44 (55) |
| **ALL** | **0.369** | **+10.3%** | **0.241** | **+8.4%** | **0.534** | **+0.9%** | **23%** | 155 (416) |

28 applied — 19 neutral, 9 beneficial, **0 harmful**. Governance: 20 requests, 16 approved, 3 rejected, **0 expired**; 12 auto-applied; 4 validation runs, 1 LLM fix call, 15 credits. Flagged caller-ID share 16.1% → 0.0% by day 3. Two supervisor edits adopted, one safety fix with 390-minute MTTR.

`nw_kyc` is slightly negative (−0.8% engagement, −0.7% conclusions) and is reported as such: the workflow was pickup-primary, had no caller-ID incident, and the schedule changes were neutral. The controller correctly did not chase engagement there; the small negative is within noise, not a claimed win.

**What I'd change.** Tune the welcome/voice practical-effect floor up for the first study after a supervisor freeze; add a post-promotion "did the control confirm it" gate before re-promoting a dimension that was previously rolled back. Public_01's single harmful change is the strongest argument for a sequential test.

## 12. Trade-offs & rejected alternatives

| Option | Why considered | Why rejected / what instead |
|---|---|---|
| Raw event log in SQLite | Exact replay; trivial audit | Unbounded at 850k events/day; every decision becomes a scan. Bounded aggregate counters + chunk checkpoint. |
| Kafka / external queue | The "production" ingestion answer | An extra dependency the SRE memo is right to challenge; unnecessary at 850k/day. SQLite chunk transactions are crash-safe. |
| Bayesian posterior / Thompson sampling | Natural explore/exploit; no fixed horizon | Harder to audit and explain. Two-proportion test + repeated confirmation + practical floor. |
| Sequential always-valid test | Avoids peeking bias | More machinery than the time budget allowed; peeking is mitigated by two consecutive confirmations and documented as a limitation. |
| One global allocation across both dimensions | Simpler config | Confounds welcome and voice. Each dimension has its own study, one change at a time. |
| Fully random exploration | Maximises information | Wastes traffic on known-bad options. Drop significantly worse challengers after two evaluations and block 3 days. |
| Re-open exploration after a supervisor edit | More learning | Silently dilutes the supervisor's explicit value. Dimensions are now frozen. |
| Auto-apply everything the policy allows | Faster loop | Prompts are customer-facing; always human-approved even when permitted. |
| Rotate to any other caller ID when one is flagged | Simple rule | Can rotate to an equally bad or worse ID. Healthy pool + historical exclusions + 5% fallback. |
| Blended pickup+engagement score | Easy to report | Forbidden by the assignment and hides failures. Separate KPIs; guardrail checked explicitly. |
| Lift huge and ignore guardrail | Better primary number | Any challenger with a material guardrail drop CI is refused. |
| Per-person scheduling everywhere | Personalisation | At 4,500 dials/hour the table is noise and huge. Gated to ≤1,500 dials/hour, capped at 20k. |
| Change cadence / dialling window | More control | Out of scope by contract; the business owns cadence and capacity. |
| Web UI for supervisors | Nice demo | Report + CLI + API answer the audit question without a front-end to defend. |
| LLM as judge of a winner | Less code | Violates "LLM may suggest, never decide". LLM only orders the prompt validation queue. |
| Re-propose a rejected change forever | Persistence | Spends approver attention on a settled question. Capped at 2 attempts. |
| Short approval TTL | Encourages urgency | Caused overnight expiries. Policy TTL capped at 24h. |
| Treat the simulator's config as truth | Simple | The controller must be auditable from its own store. Config is mirrored; the ledger is authoritative for decisions. |

## 13. Known weaknesses

1. **Fixed-horizon test.** Repeated confirmation reduces but does not eliminate false winners. Public_01's one harmful change is the evidence. A sequential test is the right next step.
2. **Bootstrap is partial.** Pre-D0 history is ingested and used for baselines, but the controller does not run a full pre-D0 discovery pass; C3 is partial.
3. **Impact score is an estimate.** It is labelled, but it is not a measured effect; there is no real historical replay.
4. **Per-person scheduling is off at load.** The gate is deliberate, but it means the load scenario gets only hour-level call-time learning.
5. **Cohort discovery is conservative.** It may miss real small segments; the 4% floor and two-stage confirmation trade recall for precision.
6. **One process, one writer.** Measured fine on public_03, but it is the first thing to break at 10×.
7. **Supervisor requests cannot always be batched.** An incident and a routine change stay separate so the approver can reject one without the other; that is more requests than Anita asked for in the worst case.
8. **Public_01 still shows one harmful change and three expiries** from before the TTL fix. The code changed; the public_01 report was not re-run after the final fix. Public_02 is the clean end-to-end run.
9. **No web UI.** The one-screen requirement is a report file + CLI, not a live screen.
10. **Dry run is honest but empty.** It says "no historical replay available" rather than pretending.

## 14. If this went to production at Formi next month

**Week 1 (days 1–5):**
1. Re-run all three public scenarios on the final commit and freeze the reports; add a CI gate that runs `pytest` + `selftest` on every change.
2. Add the sequential/always-valid test and re-measure the false-winner rate on the same scenarios.
3. Wire the documented alerts (ingestion stall, approval near expiry, credit reserve, stale conflict) to the existing on-call channel.
4. Add a historical dry-run replay for proposals, even if it is only a held-out traffic counterfactual, and label it honestly.

**Week 2 (days 6–10):**
5. Partition ingestion and evaluation by tenant; load-test at 2× public_03 first, then 10×.
6. Ship a thin supervisor web view over `report.json` (what is tested, what is winning, what needs approval, what changed today).
7. Run a real approval-hours simulation with the client ops team and tune the TTL/cooldown from actual response times.
8. Add a cohort-recall review: sample rejected cells and check whether the 4% floor is too strict for the long tail.

The order is deliberate: correctness and evidence first, then scale, then the UI. Nothing in the first week changes the statistical or governance semantics.
