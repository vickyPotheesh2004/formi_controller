# Trade-offs and rejected alternatives

| Option | Why considered | Why rejected / what instead |
|---|---|---|
| Raw event log in SQLite | Exact replay; trivial audit | Unbounded at 850k events/day; every decision becomes a scan. Kept bounded aggregate counters with a chunk checkpoint. |
| Kafka / external queue | The "production" answer to ingestion scale | A dependency the SRE memo is right to challenge, and unnecessary at 850k/day. SQLite chunk transactions are enough and crash-safe. |
| Bayesian posterior / Thompson sampling | Natural fit for explore/exploit; no fixed horizon | Harder to audit deterministically and to explain to an approver. Used a two-proportion test with repeated confirmation and a practical-effect floor. |
| Sequential test (always-valid) | Avoids peeking bias | More machinery than the time budget allowed; mitigated peeking with two consecutive confirmations instead. Documented as a limitation. |
| One global allocation over both dimensions | Simpler config | Confounds welcome and voice. Each dimension gets its own study and is changed one at a time. |
| Fully random exploration | Maximises information | Wastes traffic on obviously bad options. Drops statistically worse challengers after two evaluations and blocks them for 3 days. |
| Re-open exploration after a supervisor edit | More learning | It silently dilutes the supervisor's explicit value. The controller now freezes supervisor-set dimensions. |
| Auto-apply everything the policy allows | Faster loop | Prompt changes are customer-facing language; always human-approved even when policy would allow it. |
| Auto-apply welcome/voice even when blocked | More lift | Breaks the absolute governance boundary and confuses attribution. Only policy-permitted dimensions use auto-apply. |
| Rotate to any other caller ID when one is flagged | Simple rule | Can rotate to an equally bad or worse ID. Healthy pool excludes historically flagged IDs; if none is better by 5%, keep the live ID and record the compromise. |
| Blended pickup+engagement score | One number is easy to report | The assignment forbids blending them. Pickup and engagement stay separate; the guardrail is checked explicitly. |
| Lift huge and ignore guardrail | Better primary number | Rejected any challenger whose guardrail CI shows a material drop. |
| Optimise call time for every person | Personalisation | At 4,500 dials/hour the per-person table is noise and huge. Gated to `dials_per_hour <= 1500`, capped at 20k entries. |
| Change cadence / dialling window | More control | Out of scope by contract; the business owns cadence and capacity. |
| Web UI for supervisors | Nice demo | The report + API answer the audit question without a front-end to defend. |
| LLM as judge of winner | Less code | Violates the "LLM may suggest, never decide" rule. LLM only orders the prompt validation queue. |
| Re-propose the same rejected change forever | Persistence | Spends approver attention on a settled question. Capped at 2 attempts, then recorded and left alone. |
| Shorter approval TTL | Encourages urgency | Caused overnight expiries against 10:00–19:00 approver hours. Now uses the policy TTL capped at 24h. |
| Store the simulator's config as truth | Simple | The controller must be auditable from its own store. Config history is mirrored and the ledger is authoritative for decisions. |

## Principle

Build the five capabilities that carry the most weight — outcome optimisation,
statistical honesty, the governance boundary, crash safety, and an audit trail —
and say no to everything that only looks like progress. A smaller number of
defensible decisions beats a wide, shallow loop.
