# Testing and Evidence

## Test strategy

The test suite focuses on deterministic, safety-critical behaviour that can be
verified without depending on a particular simulator seed. The simulator tests
cover the documented contract; controller tests cover the decision core.

```bash
pytest -q
```

Current result: **32 passed, 0 warnings**.

## Unit-test coverage

The suite verifies:

- winner significance, practical effect, minimum samples, and refusal reasons;
- Bonferroni correction and independent guardrail regression;
- rejection of thin or noisy detection windows;
- config normalisation, allowed dimensions, allocation shifts, and schedule
  boundaries;
- deterministic experiment splits, promotion allocation, and cohort ordering;
- approval versus auto-apply routing, prompt safety, stale-base handling,
  approval expiry, deduplication, and policy caps;
- chunk idempotency, aggregate counters, malformed-event accounting, and
  restart-safe checkpoints;
- append-only ledger invariants and counterfactual labelling; and
- simulator advance, approval, validation, auto-apply, and hard-rule behaviour.

## Test constraints

Unit tests do not prove hidden-scenario lift, production-scale latency, or the
behaviour of a real client approver. Those are evaluated with completed
scenario runs and operational evidence. The controller must also obey these
constraints during those runs:

1. It may only use the documented simulator HTTP API.
2. It must never inspect scenario files, simulator run directories, or hidden
   ground truth.
3. It must preserve separate pickup and engagement KPIs.
4. It must not act on a single call.
5. It must not let the LLM select a winner.
6. It must not bypass governance or silently overwrite supervisor edits.
7. It must survive termination and restart without duplicate applies.

## End-to-end evidence

| Evidence | Purpose |
|---|---|
| `reports/public_01.md` | No-incident development scenario |
| `reports/public_02.md` | Caller-ID, prompt, supervisor-edit, and governance incidents |
| `reports/public_03.md` | Full public load measurements |
| `reports/crash_recovery.md` | Kill/restart correctness |
| `docs/LOAD_TEST.md` | Load-test methodology and measured resource use |

## Reproducibility note

Scenario outcomes depend on the simulator run and its generated state. The
checked-in reports are the evidence from the completed runs used for this
submission. Re-running a scenario is expected to produce a fresh run directory
and may produce different stochastic call outcomes while preserving the same
controller constraints and decision rules.
