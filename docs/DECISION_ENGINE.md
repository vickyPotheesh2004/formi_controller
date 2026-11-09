# Decision engine

The controller makes four kinds of decisions: **detect**, **experiment**,
**promote/rollback**, and **incident repair**. All four are deterministic. The
LLM can suggest prompts; it never decides a winner.

## 1. Statistics

Winner selection uses a two-arm proportion test on the workflow's primary KPI.

* **Test**: unpooled normal approximation for `p2 - p1`, two-sided p-value.
* **Interval**: unpooled CI for the difference.
* **Multiple comparisons**: `alpha = promotion_alpha / number_of_candidates`
  (Bonferroni).
* **Promotion needs all four**:
  1. both arms have at least `min_arm_connected = 300` connected calls;
  2. the lower CI bound is above the practical floor
     `baseline_rate * mde_rel` (`mde_rel` is 5% for engagement and pickup);
  3. the p-value is below the adjusted alpha;
  4. the point estimate is positive.
* **Repeated confirmation**: a candidate must pass on two consecutive
  evaluations before promotion. This is the guard against a fixed-horizon test
  that happened to peek at a lucky window.
* **Explicit refusal**: every evaluation records one of `insufficient_sample`,
  `candidate_not_better`, `statistically_uncertain`,
  `below_minimum_practical_effect`, or `eligible`. Refusals are ledger
  decisions, not silence.

The test is frequentist and fixed-horizon, not sequential. The controller does
not claim a posterior. Repeated confirmation, a practical-effect floor, and a
minimum sample are what keep the false-winner rate down; no claim is made that
it is zero.

### Guardrail

The guardrail is the *other* KPI. If the primary is engagement, the guardrail is
pickup, and vice versa. A challenger is rejected if the guardrail difference CI
shows a drop larger than the workflow's `pickup_rate_max_drop` /
`engagement_rate_max_drop` (default 2 percentage points) at the adjusted alpha.
The guardrail is checked even when the primary lift is significant.

### Decay

After promotion, the winner keeps `control_share = 15%` of traffic, spread over
the non-winner options. `detect_drop` compares the winner's recent window to
its pre-promotion window; a relative drop of at least 10% with
`min_arm_connected` evidence and significance produces a ledger decision and a
rollback proposal. The control arm is what makes the comparison possible after
promotion.

## 2. Experiment design

* **Dimensions**: `welcome_message`, `voice` (only for engagement-primary
  workflows). Pickup-primary workflows are handled by caller-ID health and
  scheduling; changing welcome/voice would risk the engagement guardrail without
  moving the primary KPI.
* **Allocation**: challengers get 60% total, the incumbent keeps 40%
  (`exploration_share = 0.60`, `control = 0.0` during exploration). After a
  promotion, the winner gets 85% and a permanent 15% control is spread over the
  other options.
* **Two dimensions**: each dimension has its own study and its own split. The
  default-cohort allocation changes one dimension at a time, so a comparison is
  never confounded by a simultaneous change in the other dimension.
* **Dropping losers**: a challenger that is significantly worse on the primary
  KPI for two evaluations is removed from the split and blocked for three days.
  Traffic is not spent on a known loser.
* **Closed studies**: a study with no winner after `MAX_STUDY_DAYS = 5` keeps the
  live split. Reverting to 100% baseline only to re-randomise next tick is churn
  and can throw away a real lift.
* **Supervisor-set dimensions are never re-opened** (see GOVERNANCE.md).

## 3. Cohorts

One cohort object is shared by detection, experiments, and the supervisor view.
Matching is AND across features, OR inside a feature's value list, first
matching cohort wins. Cohort IDs are derived from the `where` clause, so the
same segment has the same name everywhere.

Discovery is deliberately conservative:

* scan at most once per day, at or after 20:00, on cumulative study data;
* a cell must pass the same statistical gates with
  `min_cohort_arm_connected = 120`;
* it is then re-tested on data collected **after** the discovery day;
* the segment must be at least 4% of uploaded leads
  (`min_cohort_share = 0.04`) and the system keeps at most 8 cohorts;
* only after independent confirmation is a cohort plan built;
* a cohort win closes the global study, and no new study is created for three
  days after the win.

This is what stops a 3-feature × 4-value × 2-arm scan from producing a
false cohort every day. The controller always compares a candidate to the
baseline on the *same* segment; it never compares a cohort result to the global
average.

## 4. Call time

`schedule.py` learns hour weights from observed pickup by hour:

* Beta-shrink each hour's pickup toward the workflow mean, so a thin hour does
  not become an extreme weight;
* clip weights to `[0.3, 3.0]`;
* include every hour in the business dialling window, so an hour can never be
  accidentally dropped;
* require at least `schedule_min_dials = 1500` dials before changing weights.

Per-person preferred-hour learning (`lead_preferred_hour`) is gated to workflows
with `dials_per_hour <= 1500`, because at the load-scenario rate the table would
be noisy and huge. When enabled it is capped at 20,000 entries. The business owns
cadence and dialling capacity; the controller only decides who is called in a
given hour.

## 5. Incidents

### Caller ID

For each option, compare recent pickup (at least 200 dials, else the last two
days) to the longer baseline (at least 800 dials):

* flag when the relative drop is at least 15% and the difference is
  significant at `caller_id_z = 2.5`;
* build a healthy pool of caller IDs that are neither currently flagged nor
  historically flagged for that workflow;
* if a clean pool exists, remove the bad ID and spread its weight over the pool;
* if every option is compromised, keep the live ID unless another option's last
  observed pickup beats it by at least 5%, and record
  `incident_did_observed` instead of spending approver attention on a rotation
  to a worse ID.

### Hallucination / prompt

* Compare the current prompt's recent hallucination rate (at least 150 connected)
  to its history and to the absolute 2% line;
* an incident is opened when the spike is significant at `hallucination_z = 2.5`
  and either materially above history or above 2%;
* ask `POST /llm/suggest-fixes` for candidates (the suggestion only orders the
  validation queue and is recorded as a suggestion);
* validate every candidate with `POST /validation/runs` (300 calls each);
* validation is a **safety gate**, not an engagement ranking:
  * among candidates above the workflow's `min_validation_pass_rate`, prefer the
    one with the best observed engagement history from `prompt_daily`;
  * with no usable history, prefer the highest pass rate, then the workflow's
    declared option order;
* prompt changes are **always sent to a human approver**, even where auto-apply
  is policy-enabled.

Credits are reserved (`credit_reserve = 20`) so an incident cannot exhaust the
account mid-repair. Validation runs are the only spend; the controller does not
re-validate identical candidates when a pending proposal already exists.

## 6. What the controller will not do

* It will not call a winner from one call or one small sample.
* It will not treat an LLM suggestion as evidence.
* It will not blend pickup and engagement into one score.
* It will not override a supervisor edit to re-open exploration.
* It will not submit a prompt change without a passing validation run.
* It will not rotate a caller ID when every alternative is known to be worse.
