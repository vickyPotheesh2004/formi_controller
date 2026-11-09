# Company Submission Brief

## Executive summary

Formi needs outbound voice agents that can improve over time without silently
changing regulated customer workflows. This repository provides a closed-loop
controller that measures workflow health, runs controlled experiments, proposes
changes with evidence, applies only policy-permitted changes, and explains the
result from its own audit store.

The controller is designed for the supplied simulator and communicates only
through the documented HTTP API. It is not connected to production systems.
The simulator contract is the source of truth for the implementation:
[`SIMULATOR.md`](SIMULATOR.md).

## Problem

Static agent configuration becomes stale. A caller ID can degrade, a prompt can
start hallucinating, and a voice or welcome message can work better for one
population than another. A naive optimiser could improve one number while
damaging another or make an unauthorised customer-facing change.

The key constraints are:

- pickup rate and engagement rate must remain separate;
- primary KPI optimisation must respect the guardrail KPI;
- evidence must come from populations, not anecdotes;
- an LLM may suggest, but never decide, a winner;
- supervisor and client approval decisions must be respected;
- restarts must not duplicate applies or lose pending governance state; and
- every decision must be reconstructable after the run.

## Proposed solution

The controller implements five durable stages:

1. **Ingestion:** idempotent event-chunk processing with transactional
   checkpoints and bounded aggregates.
2. **Detection:** threshold-based detection for caller-ID degradation,
   hallucination spikes, metric drops, and cohort differences.
3. **Experimentation:** controlled allocation, shared cohorts, minimum sample
   sizes, statistical tests, practical-effect floors, multiple-comparison
   correction, and guardrail checks.
4. **Governance:** policy validation, approval routing, timing/cap limits,
   stale-base protection, safe auto-apply, and reconciliation.
5. **Learning and explanation:** promotion, decay detection, rollback, incident
   verification, and append-only audit decisions.

The design details are in [`ARCHITECTURE.md`](ARCHITECTURE.md),
[`DECISION_ENGINE.md`](DECISION_ENGINE.md), and [`GOVERNANCE.md`](GOVERNANCE.md).

## Differentiation

The important design choice is that autonomy is earned through governance and
evidence. A shared cohort object prevents detection, experimentation, and the
supervisor view from using inconsistent populations. A retained control arm
makes decay measurable. LLM output is kept outside the winner-selection path.
Supervisor edits become the baseline and freeze the edited dimension, preventing
the controller from fighting a human decision.

## Evidence and impact

Evidence is checked in rather than described only in prose:

- [`reports/public_01.md`](../reports/public_01.md): development scenario;
- [`reports/public_02.md`](../reports/public_02.md): incidents, caller-ID
  remediation, supervisor edits, and prompt repair;
- [`reports/public_03.md`](../reports/public_03.md): load measurements;
- [`reports/crash_recovery.md`](../reports/crash_recovery.md): restart safety;
- [`docs/LOAD_TEST.md`](LOAD_TEST.md): detailed scale measurements; and
- [`SUBMISSION.md`](../SUBMISSION.md): scope, stakeholder judgement, and
  assignment responses.

The incident scenario produced +29.8% conclusion rate and +21.5% pickup rate
for `ks_cancel` versus its baseline, with +10.2% engagement improvement from a
validated prompt repair. The load run completed at simulated minute 7,200 with
9,737,504 events, 270 recorded decisions, and zero malformed events.

## Review checklist

Before sharing the repository, a reviewer can run:

```bash
python -m pip install -r requirements.txt
pytest -q
```

Then inspect the public evidence and run the controller using the instructions
in [`README.md`](../README.md). The expected unit-test result is 32 passing
tests with no warnings.

## Scope and limitations

The controller is intentionally a single Python process with SQLite storage.
It fits the measured 4-vCPU/8-GB target, but the single writer and evaluation
loop are the first expected bottlenecks at 10x. The next production evolution
would partition ingestion and evaluation by tenant or workflow group and add
read-only rollups. See [`TRADEOFFS.md`](TRADEOFFS.md).

## Security and data boundary

The project stores controller aggregates, decisions, and operational state. It
does not connect to Formi production, access credentials, or external customer
systems. The controller is constrained to the simulator's documented HTTP
surface and does not read hidden simulator truth.
