# Public load-test evidence

Run on 2026-11-09 on the development Windows host from the repository root.
The simulator generated `public_03_load` in 90.5 seconds; the controller then
completed the five simulated days in approximately 2 minutes 27 seconds. The
controller process reached approximately 630 MB RSS during the run.

| Metric | Result |
|---|---:|
| Simulated duration | 5 days / 7,200 minutes |
| Events ingested | 9,737,504 |
| Calls represented | 7,433,295 |
| Leads represented | 2,304,000 |
| Aggregate ingestion time | 297,190 ms |
| Aggregate ingestion throughput | 32,762 events/s |
| Decisions recorded | 270 |
| Malformed events | 0 |
| SQLite size | 430,772,224 bytes (411 MiB) |
| Peak controller RSS | approximately 630 MB |
| Final state | finished at minute 7,200 |

The SQLite result is materially larger than the earlier design estimate because
the load run retains more aggregate dimensions and indexes than the small public
scenarios. It is still within the stated 8 GB envelope. At 10x, the single
SQLite writer and evaluation loop are the first scaling boundary; partitioning
by tenant/workflow and rolling up read-only views is the next step.

The run's controller report is in `state/public_03_new/report.md` and the
machine-readable report is in `state/public_03_new/report.json`.
