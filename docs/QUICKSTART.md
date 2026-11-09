# Reviewer Quickstart

## Prerequisites

- Python 3.10 or newer
- `pip`
- a shell capable of running the commands below

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

## Run a complete local loop

Terminal 1 — start the simulator:

```bash
python -m sim serve \
  --scenario scenarios/public_01_dev.yaml \
  --run-dir runs/reviewer_public_01
```

Terminal 2 — start the controller:

```bash
./run.sh http://127.0.0.1:8700
```

On Windows PowerShell, use:

```powershell
python -m controller run --url http://127.0.0.1:8700 --state state/reviewer_public_01
```

The controller exits when the scenario finishes. Its durable state and report
are written under the selected state directory.

## Run the checks

```bash
pytest -q
python scripts/crash_recovery.py --port 8711 --kill-after 20
```

The expected test result is 32 passed tests. Crash evidence is written to
[`reports/crash_recovery.md`](../reports/crash_recovery.md).

## Inspect decisions

The controller exposes its own decision store through the CLI. For example:

```bash
python -m controller decisions \
  --state state/reviewer_public_01 \
  --limit 20 \
  --json
```

This is the source for answering why a workflow changed, what evidence was
used, whether approval was required, and what happened after apply. The audit
design is described in [`GOVERNANCE.md`](GOVERNANCE.md).

## Configuration and state

Useful environment variables:

- `CONTROLLER_STATE_DIR`: durable state directory; default `state`;
- `CONTROLLER_STEP_MINUTES`: simulator time advanced per loop tick;
- `CONTROLLER_DB`: explicit SQLite path; and
- `CONTROLLER_LOG`: explicit JSONL log path.

Do not delete a state directory during a live run. To intentionally start a new
scenario, use a new state directory or stop the old run first.

## Troubleshooting

- **Port already in use:** choose another simulator port and pass the matching
  URL to the controller.
- **Controller resumes an old world:** use a new state directory for a new
  scenario, or allow the controller's world-reset logic to reconcile it.
- **Report encoding on Windows:** set `PYTHONUTF8=1` before running simulator
  grading commands.
- **Approval remains pending:** inspect the simulator's approver hours and the
  controller decision ledger; the controller deliberately does not bypass
  approval timing.
