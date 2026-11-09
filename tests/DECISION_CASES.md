# Decision cases

These are the cases the controller is expected to get right every time. Each is
pinned by a unit test in `tests/test_controller.py` (`pytest -q tests`), and the
end-to-end behaviour is exercised by running the public scenarios and
`scripts/crash_recovery.py`.

## Statistics and promotion

| # | Case | Expected decision | Test |
|---|---|---|---|
| S1 | Candidate has a large lift but only 100 calls per arm | Refuse: `insufficient_sample` | `test_promotion_requires_sample_significance_and_practical_effect` |
| S2 | Candidate has 1,000 calls per arm but the same rate | Refuse: `candidate_not_better` | same |
| S3 | Candidate is better by less than the 5% practical floor | Refuse: `below_minimum_practical_effect` | same |
| S4 | Candidate is statistically significant and practically better, guardrail intact | `eligible` | same |
| S5 | Three candidates are tested at once | Alpha is Bonferroni-adjusted by 3 | `test_bonferroni_is_stricter_and_guardrail_needs_a_real_drop` |
| S6 | Primary lifts but pickup drops beyond the guardrail | Refuse the challenger | same |
| S7 | Guardrail difference is inside the allowed drop or based on thin data | Do not call a regression | same |
| S8 | Recent window is thin or the drop is noise | Do not call decay | `test_drop_detection_ignores_thin_or_noisy_windows` |
| S9 | Recent window has a real 10%+ drop | Report decay and roll back | same |

## Experiment design

| # | Case | Expected decision | Test |
|---|---|---|---|
| E1 | A study is created | 60% challenger mass, incumbent keeps 40% | `test_allocation_design_keeps_control_and_sums_to_one` |
| E2 | A winner is promoted | Winner 85%, 15% control spread over the field | same |
| E3 | Study closes with no winner | Keep the live split, do not churn | `test_desired_distribution_keeps_live_split_after_a_no_winner_close` |
| E4 | A supervisor set a dimension | Never create a new study on it | `test_ensure_studies_does_not_reopen_a_supervisor_set_dimension` |
| E5 | Promoted study | Keep a control arm for decay | `test_desired_distribution_after_promotion_keeps_a_control_arm` |
| E6 | Cohort plan | Unique, deterministic ids in declared order | `test_cohort_plan_builds_unique_ordered_ids` |
| E7 | Cohort matching | AND across features, OR inside values, first match wins | `test_cohort_matching_is_first_match_and_validated` |

## Governance

| # | Case | Expected decision | Test |
|---|---|---|---|
| G1 | Change is within auto-apply policy | Auto-apply, no approval request | `test_auto_apply_goes_through_the_policy_path` |
| G2 | Changed dimension is not in `allowed_dims` | Approval path; config unchanged | `test_non_allowed_dimension_uses_approval_and_does_not_change_config` |
| G3 | Allocation shift exceeds `max_shift` | Auto-apply refused | `test_auto_apply_policy_respects_dims_shift_and_enablement` |
| G4 | Auto-apply disabled | Auto-apply refused | same |
| G5 | Clock is outside approver hours | Defer and record; no request spent | `test_outside_approver_hours_defers_without_spending_a_request` |
| G6 | Prompt change, even with auto-apply enabled | Require a passing validation run | `test_prompt_change_requires_validation_even_when_auto_apply_is_enabled` |
| G7 | Same unchanged proposal rejected before | Do not resubmit beyond the attempt cap | `test_unchanged_rejected_proposal_is_not_resubmitted` |
| G8 | Approved but base version changed | Refuse to apply; mark `stale` | `test_approved_change_is_refused_when_base_version_is_stale` |
| G9 | Restart after a crashed apply | Mark the local proposal `applied`, never re-apply | `test_reconcile_history_finishes_a_crashed_apply` |

## Incidents

| # | Case | Expected decision | Test |
|---|---|---|---|
| I1 | A caller ID is flagged, clean alternatives exist | Rotate to the healthy pool | `test_did_replacement_pool_never_reuses_historically_flagged_ids` |
| I2 | Every option was flagged before | Fall back to the best last observed pickup | same |
| I3 | Every option is compromised and no alternative beats the live ID by 5% | Keep the live ID, record `incident_did_observed` | `test_did_rotation_is_skipped_when_every_alternative_is_worse` |
| I4 | Prompt near-tie on validation, one has proven engagement | Pick the proven one; validation is a safety gate | `test_prompt_fix_prefers_observed_engagement_among_safe_candidates` |

## Schedule

| # | Case | Expected decision | Test |
|---|---|---|---|
| T1 | Hour pickup varies | Bounded weights keep every hour, clip extremes | `test_bounded_hour_weights_keep_every_window_hour_and_clip_extremes` |

## Ingestion, time and audit

| # | Case | Expected decision | Test |
|---|---|---|---|
| A1 | Same chunk is delivered twice | Second delivery is skipped | `test_ingestion_is_chunk_idempotent` |
| A2 | A malformed line arrives | Counted, chunk still commits | `test_ingestion_malformed_lines_are_counted_not_fatal` |
| A3 | Event timestamp | Bucketed on the simulated IST clock (D0 = 2027-01-04) | `test_day_hour_uses_the_simulated_ist_clock` |
| A4 | Ledger update/delete | Blocked by triggers; counterfactuals labelled `observed: false` | `test_ledger_is_append_only_and_counterfactuals_are_labelled` |
| A5 | Invalid config | Rejected before an approval is spent | `test_config_validation_and_shift` |

## Pattern

For every decision the store answers: what type, why, from which source and
evidence, what alternatives were considered, what policy applied, what was
expected, what risk was accepted, who approved, whether it was applied, and what
happened next. A missing source or a counterfactual not marked hypothetical is a
hard ledger error, not a warning.
