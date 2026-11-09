"""Unit tests for the controller's deterministic core.

These do not start the simulator. The end-to-end loop is exercised by
``scripts/crash_recovery.py`` and by running the public scenarios; this file
pins the pieces that are easy to get subtly wrong:

* statistical winner selection and its refusal cases;
* config validation, allocation shift, and auto-apply policy;
* governance routing (auto path, approval path, prompt safety, stale base);
* chunk idempotency and aggregate counters;
* append-only ledger and counterfactual labelling;
* experiment allocation design and cohort ordering.
"""
from __future__ import annotations

import json

import pytest

from controller import cohorts, db, experiments, incidents, ingestion, schedule, stats
from controller.config import Settings
from controller.governance import Governance, _minute_from_ts
from controller.ledger import DecisionLedger, LedgerError


# --------------------------------------------------------------------------- helpers
def make_settings(tmp_path, **kw) -> Settings:
    return Settings(state_dir=str(tmp_path), db_path=str(tmp_path / "test.db"), **kw)


def make_conn(tmp_path):
    conn = db.connect(str(tmp_path / "test.db"))
    db.init_schema(conn)
    return conn


def workflow(**overrides) -> dict:
    wf = {
        "workflow_id": "wf_test",
        "tenant_id": "t1",
        "options": {"welcome_message": ["W1", "W2"],
                    "voice": ["V1", "V2"],
                    "caller_id": ["D1", "D2"],
                    "prompt_version": ["P1", "P2"]},
        "dialling_window": [9, 21],
        "dials_per_hour": 300,
        "objective": {"primary": "engagement_rate",
                      "guardrail": {"pickup_rate_max_drop": 0.02}},
        "governance": {
            "auto_apply": {"enabled": True, "allowed_dims": ["schedule", "caller_id"],
                           "max_shift": 0.5},
            "approver_hours": [10, 19],
            "proposal_ttl_hours": 24,
            "max_requests_per_24h": 3,
            "min_validation_pass_rate": 0.97,
        },
    }
    wf.update(overrides)
    return wf


def base_config(prompt="P1") -> dict:
    return {
        "cohorts": [],
        "allocations": {"default": {"welcome_message": {"W1": 1.0},
                                    "voice": {"V1": 1.0},
                                    "caller_id": {"D1": 1.0}}},
        "prompt_version": prompt,
        "schedule": {"hour_weights": {}, "lead_preferred_hour": {}},
    }


class FakeSim:
    """Minimal in-memory stand-in for the documented HTTP contract."""

    def __init__(self, config: dict):
        self.version = 1
        self.config_data = json.loads(json.dumps(config))
        self.history = [{"version": 1, "actor": "business_default", "changed_dims": [],
                         "config": self.config_data}]
        self.approvals: dict[str, dict] = {}
        self.auto_applied = []
        self.applied = []
        self.seq = 0

    def config(self, wid):
        return {"workflow_id": wid, "version": self.version,
                "config": json.loads(json.dumps(self.config_data))}

    def config_history(self, wid):
        return self.history

    def auto_apply(self, wid, body):
        self.auto_applied.append(body)
        self._commit(body["proposed_config"])
        return {"version": self.version, "changed_dims": ["schedule"]}

    def request_approval(self, body):
        self.seq += 1
        aid = f"ap_{self.seq}"
        self.approvals[aid] = {"approval_id": aid, "proposal_id": body["proposal_id"],
                               "workflow_id": body["workflow_id"], "status": "pending",
                               "base_version": body["base_version"],
                               "created_at": "2027-01-04T10:00:00+05:30",
                               "expires_at": "2027-01-05T10:00:00+05:30"}
        return self.approvals[aid]

    def get_approval(self, aid):
        return self.approvals[aid]

    def list_approvals(self, wid=None, status=None):
        return [a for a in self.approvals.values()
                if (wid is None or a["workflow_id"] == wid)]

    def apply(self, wid, aid):
        ap = self.approvals[aid]
        self._commit(json.loads(json.dumps(ap.get("_proposed") or self.config_data)))
        ap["status"] = "applied"
        ap["applied_version"] = self.version
        self.applied.append(aid)
        return {"version": self.version, "changed_dims": []}

    def _commit(self, cfg):
        self.version += 1
        self.config_data = cfg
        self.history.append({"version": self.version, "actor": "controller_auto",
                             "changed_dims": [], "config": cfg})


def make_gov(tmp_path, wf=None, client=None, **setting_kw):
    conn = make_conn(tmp_path)
    wf = wf or workflow()
    client = client or FakeSim(base_config())
    ledger = DecisionLedger(conn, "test", "test")
    gov = Governance(conn, client, make_settings(tmp_path, **setting_kw), ledger,
                     {wf["workflow_id"]: wf}, "test")
    return conn, gov, ledger, client, wf


# --------------------------------------------------------------------------- statistics
def test_promotion_requires_sample_significance_and_practical_effect():
    good = stats.evaluate_candidate(100, 1000, 145, 1000, min_n=300, mde_rel=0.05,
                                    alpha=0.05)
    assert good["eligible"] and good["relative_lift"] > 0.05

    equal = stats.evaluate_candidate(100, 1000, 100, 1000, min_n=300, mde_rel=0.05,
                                     alpha=0.05)
    assert not equal["eligible"] and equal["reason"] == "candidate_not_better"

    small = stats.evaluate_candidate(100, 100, 120, 100, min_n=300, mde_rel=0.05,
                                     alpha=0.05)
    assert not small["eligible"] and small["reason"] == "insufficient_sample"

    # Significant in a huge sample but below the practical floor.
    tiny = stats.evaluate_candidate(10000, 100000, 10400, 100000, min_n=300,
                                    mde_rel=0.05, alpha=0.05)
    assert not tiny["eligible"] and tiny["reason"] == "below_minimum_practical_effect"


def test_bonferroni_is_stricter_and_guardrail_needs_a_real_drop():
    assert stats.bonferroni(0.05, 4) == pytest.approx(0.0125)
    ok = stats.guardrail_regressed(300, 1000, 290, 1000, max_abs_drop=0.02, alpha=0.05,
                                   min_n=200)
    assert not ok["regressed"]
    bad = stats.guardrail_regressed(300, 1000, 150, 1000, max_abs_drop=0.02, alpha=0.05,
                                    min_n=200)
    assert bad["regressed"]


def test_drop_detection_ignores_thin_or_noisy_windows():
    thin = stats.detect_drop(1, 2, 0, 2, min_n=200, min_rel_drop=0.15, alpha=0.05)
    assert not thin["drop"] and not thin["sufficient"]
    real = stats.detect_drop(300, 1000, 150, 1000, min_n=200, min_rel_drop=0.15, alpha=0.05)
    assert real["drop"]


# --------------------------------------------------------------------------- cohorts
def test_config_validation_and_shift():
    wf = workflow()
    cfg, problems = cohorts.normalise_config(wf, base_config())
    assert not problems
    assert cohorts.changed_dims(cfg, cfg) == []
    assert cohorts.max_shift(cfg, cfg) == pytest.approx(0.0)

    proposed = json.loads(json.dumps(cfg))
    proposed["allocations"]["default"]["voice"] = {"V1": 0.5, "V2": 0.5}
    assert cohorts.changed_dims(cfg, proposed) == ["voice"]
    assert cohorts.max_shift(cfg, proposed) == pytest.approx(0.5)

    bad = json.loads(json.dumps(cfg))
    bad["allocations"]["default"]["voice"] = {"VX": 1.0}
    _, problems = cohorts.normalise_config(wf, bad)
    assert any("not allowed" in p for p in problems)

    bad_hour = json.loads(json.dumps(cfg))
    bad_hour["schedule"]["hour_weights"] = {"default": {"3": 1.0}}
    _, problems = cohorts.normalise_config(wf, bad_hour)
    assert any("outside" in p for p in problems)


def test_auto_apply_policy_respects_dims_shift_and_enablement():
    wf = workflow()
    cfg, _ = cohorts.normalise_config(wf, base_config())

    sched = json.loads(json.dumps(cfg))
    sched["schedule"]["hour_weights"] = {"default": {"9": 1.0, "10": 2.0}}
    check = cohorts.auto_apply_check(wf, cfg, sched)
    assert check["allowed"] and check["dims"] == ["schedule"]

    alloc = json.loads(json.dumps(cfg))
    alloc["allocations"]["default"]["caller_id"] = {"D1": 0.5, "D2": 0.5}
    assert cohorts.auto_apply_check(wf, cfg, alloc)["allowed"]

    voice = json.loads(json.dumps(cfg))
    voice["allocations"]["default"]["voice"] = {"V1": 0.5, "V2": 0.5}
    check = cohorts.auto_apply_check(wf, cfg, voice)
    assert not check["allowed"] and any("not permitted" in r for r in check["reasons"])

    disabled = workflow(governance={**wf["governance"],
                                    "auto_apply": {"enabled": False, "allowed_dims": [],
                                                   "max_shift": 0}})
    assert not cohorts.auto_apply_check(disabled, cfg, sched)["allowed"]


def test_allocation_design_keeps_control_and_sums_to_one():
    split = cohorts.split_allocations(["A", "B", "C"], "A", exploration=0.6, control=0.0)
    assert split["A"] == pytest.approx(0.4)
    assert split["B"] == split["C"] == pytest.approx(0.3)
    assert sum(split.values()) == pytest.approx(1.0)

    promoted = cohorts.winner_allocation(["A", "B", "C"], "B", control=0.15)
    assert promoted["B"] == pytest.approx(0.85)
    assert promoted["A"] == promoted["C"] == pytest.approx(0.075)
    assert sum(promoted.values()) == pytest.approx(1.0)


def test_ensure_studies_does_not_reopen_a_supervisor_set_dimension(tmp_path):
    state = {"studies": {}, "promotions": {}, "cohort_wins": {}, "blocked": {},
             "supervisor_dims": {"voice": 100}}
    created = experiments.ensure_studies(
        make_conn(tmp_path), workflow(), base_config(), state,
        100 + 5 * 1440, make_settings(tmp_path))
    dims = {c["dim"] for c in created}
    assert "voice" not in dims
    assert "welcome_message" in dims


def test_cohort_matching_is_first_match_and_validated():
    cfg = {"cohorts": [{"id": "seniors_hi", "where": {"age_band": ["60+"],
                                                      "lang": ["hi"]}},
                       {"id": "hi", "where": {"lang": ["hi"]}}]}
    assert cohorts.cohort_of(cfg, {"lang": "hi", "age_band": "60+"}) == "seniors_hi"
    assert cohorts.cohort_of(cfg, {"lang": "hi", "age_band": "18-30"}) == "hi"
    assert cohorts.cohort_of(cfg, {"lang": "en"}) == "default"
    assert cohorts.validate_cohort_set(cfg["cohorts"]) == []


# --------------------------------------------------------------------------- governance
def test_auto_apply_goes_through_the_policy_path(tmp_path):
    conn, gov, ledger, client, wf = make_gov(tmp_path)
    _, current = gov.current_config("wf_test")
    proposed = json.loads(json.dumps(current))
    proposed["schedule"]["hour_weights"] = {"default": {"9": 1.0, "10": 2.0}}

    res = gov.propose(wid="wf_test", proposed_config=proposed, change_summary="schedule",
                      rationale="Observed pickup differs by hour.", evidence={"h": 1},
                      affected_leads=10, dry_run={}, impact_score=0.3, kind="schedule",
                      sim_minute=11 * 60)
    assert res["ok"] and res["status"] == "applied"
    assert len(client.auto_applied) == 1 and client.applied == []
    assert client.auto_applied[0]["base_version"] == 1
    counts = {r["decision_type"] for r in conn.execute("SELECT decision_type FROM decisions")}
    assert "auto_apply_attempt" in counts


def test_non_allowed_dimension_uses_approval_and_does_not_change_config(tmp_path):
    conn, gov, ledger, client, wf = make_gov(tmp_path)
    _, current = gov.current_config("wf_test")
    proposed = json.loads(json.dumps(current))
    proposed["allocations"]["default"]["voice"] = {"V1": 0.5, "V2": 0.5}

    res = gov.propose(wid="wf_test", proposed_config=proposed, change_summary="voice test",
                      rationale="Challenger experiment.", evidence={"e": 1},
                      affected_leads=100, dry_run={}, impact_score=0.4, kind="experiment",
                      sim_minute=11 * 60)
    assert res["ok"] and res["status"] == "pending"
    assert client.auto_applied == [] and client.version == 1
    row = conn.execute("SELECT status,mode FROM proposals").fetchone()
    assert row["status"] == "approval_pending" and row["mode"] == "approval"


def test_outside_approver_hours_defers_without_spending_a_request(tmp_path):
    _, gov, _, client, _ = make_gov(tmp_path)
    _, current = gov.current_config("wf_test")
    proposed = json.loads(json.dumps(current))
    proposed["allocations"]["default"]["voice"] = {"V1": 0.5, "V2": 0.5}
    res = gov.propose(wid="wf_test", proposed_config=proposed, change_summary="voice",
                      rationale="Experiment", evidence={}, affected_leads=0, dry_run={},
                      impact_score=0.2, kind="experiment", sim_minute=2 * 60)
    assert not res["ok"] and res["reason"] == "outside_approver_hours"
    assert client.approvals == {}


def test_prompt_change_requires_validation_even_when_auto_apply_is_enabled(tmp_path):
    wf = workflow(governance={"auto_apply": {"enabled": True,
                                             "allowed_dims": ["schedule", "caller_id",
                                                              "welcome_message", "voice",
                                                              "prompt_version"],
                                             "max_shift": 1.0},
                              "approver_hours": [0, 24], "proposal_ttl_hours": 24,
                              "max_requests_per_24h": 4,
                              "min_validation_pass_rate": 0.97})
    _, gov, _, client, _ = make_gov(tmp_path, wf=wf)
    _, current = gov.current_config("wf_test")
    proposed = json.loads(json.dumps(current))
    proposed["prompt_version"] = "P2"

    blocked = gov.propose(wid="wf_test", proposed_config=proposed, change_summary="prompt",
                          rationale="Safety fix", evidence={}, affected_leads=0, dry_run={},
                          impact_score=0.8, kind="prompt", sim_minute=11 * 60)
    assert not blocked["ok"] and blocked["reason"] == "missing_validation_evidence"
    assert client.auto_applied == [] and client.approvals == {}

    ok = gov.propose(wid="wf_test", proposed_config=proposed, change_summary="prompt",
                     rationale="Safety fix", evidence={"validation_run_id": "vr_1"},
                     affected_leads=0, dry_run={}, impact_score=0.8, kind="prompt",
                     sim_minute=11 * 60)
    assert ok["ok"] and ok["status"] == "pending"
    assert client.auto_applied == [] and len(client.approvals) == 1


def test_unchanged_rejected_proposal_is_not_resubmitted(tmp_path):
    wf = workflow()
    conn, gov, ledger, client, _ = make_gov(tmp_path, wf=wf, max_proposal_attempts=1)
    _, current = gov.current_config("wf_test")
    proposed = json.loads(json.dumps(current))
    proposed["allocations"]["default"]["voice"] = {"V1": 0.5, "V2": 0.5}
    args = dict(wid="wf_test", proposed_config=proposed, change_summary="voice",
                rationale="Experiment", evidence={}, affected_leads=0, dry_run={},
                impact_score=0.2, kind="experiment")
    first = gov.propose(sim_minute=11 * 60, **args)
    assert first["ok"]
    ap = client.approvals[first["approval_id"]]
    ap["status"] = "rejected"
    gov.poll_and_apply(12 * 60)  # closes as rejected and sets a cooldown

    # Ignore the cooldown to exercise the "no repeat without new evidence" gate.
    conn.execute("UPDATE workflow_state SET next_proposal_sim_minute=0")
    second = gov.propose(sim_minute=24 * 60, **args)
    assert not second["ok"] and second["reason"] == "resubmission_suppressed"


def test_reconcile_history_finishes_a_crashed_apply(tmp_path):
    conn, gov, _, client, _ = make_gov(tmp_path)
    conn.execute(
        "INSERT INTO proposals(proposal_id,workflow_id,kind,base_version,proposed_config_json,"
        "change_summary,diff_json,dry_run_json,affected_leads,impact_score,rationale,"
        "evidence_json,mode,status,created_sim_minute,attempts,dedupe_key) "
        "VALUES('ctl-auto-wf_test-x-a1','wf_test','schedule',1,'{}','s','{}','{}',0,0,'r',"
        "'{}','auto','submitting',0,1,'d')")
    conn.commit()
    client.history.append({"version": 2, "actor": "controller_auto", "changed_dims": ["schedule"],
                           "proposal_id": "ctl-auto-wf_test-x-a1",
                           "approval_id": None, "config": base_config()})
    gov.reconcile_history("wf_test", 60)
    row = conn.execute("SELECT status,applied_version FROM proposals "
                       "WHERE proposal_id='ctl-auto-wf_test-x-a1'").fetchone()
    assert row["status"] == "applied" and row["applied_version"] == 2


def test_approved_change_is_refused_when_base_version_is_stale(tmp_path):
    wf = workflow()
    conn, gov, ledger, client, _ = make_gov(tmp_path, wf=wf)
    _, current = gov.current_config("wf_test")
    proposed = json.loads(json.dumps(current))
    proposed["allocations"]["default"]["voice"] = {"V1": 0.5, "V2": 0.5}
    res = gov.propose(wid="wf_test", proposed_config=proposed, change_summary="voice",
                      rationale="Experiment", evidence={}, affected_leads=0, dry_run={},
                      impact_score=0.2, kind="experiment", sim_minute=11 * 60)
    approval_id = res["approval_id"]
    client.approvals[approval_id]["status"] = "approved"

    # A supervisor moved the workflow on before the controller applied.
    client._commit(base_config(prompt="P2"))
    out = gov.poll_and_apply(12 * 60)
    assert out and out[0]["reason"] == "stale"
    assert client.applied == []
    assert conn.execute("SELECT status FROM proposals").fetchone()["status"] == "stale"


# --------------------------------------------------------------------------- ingestion
class FakeEvents:
    def __init__(self, events):
        self.events = events
        self.calls = 0

    def list_chunks(self, kind="events", after=-1):
        # History is empty; the single events chunk is returned once.
        if kind == "history" or after >= 0:
            return []
        return [{"chunk": 0, "kind": "events", "first_event_id": 1,
                 "last_event_id": len(self.events)}]

    def iter_chunk(self, n):
        yield from self.events


def _call(lead_id, person_id, connected, engaged, hour_ts, variant=None):
    return {"event_id": None, "type": "call", "workflow_id": "wf_test",
            "lead_id": lead_id, "person_id": person_id, "connected": connected,
            "engaged": engaged, "disposition":
                "concluded_interested" if engaged and connected else "no_answer",
            "ts": hour_ts, "config_version": 1, "cohort_id": "default",
            "variant": variant or {"welcome_message": "W1", "voice": "V1",
                                   "caller_id": "D1", "prompt_version": "P1"},
            "qa": {"hallucination_flag": False, "flow_adherence": 0.9}}


def test_ingestion_is_chunk_idempotent(tmp_path):
    conn = make_conn(tmp_path)
    settings = make_settings(tmp_path)
    ts = "2027-01-04T10:15:00+05:30"
    events = [
        {"event_id": 1, "type": "lead", "workflow_id": "wf_test", "lead_id": "l1",
         "person_id": "p1", "campaign_id": "c1", "ts": ts,
         "features": {"lang": "hi", "city_tier": "2", "age_band": "31-45",
                      "value_band": "mid"}},
        _call("l1", "p1", True, True, ts),
        _call("l1", "p1", True, False, ts),
    ]
    client = FakeEvents(events)
    ing = ingestion.Ingestor(conn, client, settings, "test",
                             {"wf_test": {"workflow_id": "wf_test", "dials_per_hour": 300}})
    first = ing.ingest_pending()
    second = ing.ingest_pending()
    assert first["events"] == 3 and first["calls"] == 2 and first["leads"] == 1
    assert second["events"] == 0 and second["chunks"] == 0

    row = conn.execute("SELECT dials,connected,engaged FROM variant_daily").fetchone()
    assert (row["dials"], row["connected"], row["engaged"]) == (2, 2, 1)
    assert conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"] == 1
    # The person-hour table is written for low-volume workflows only.
    ph = conn.execute("SELECT dials FROM person_hour WHERE person_id='p1'").fetchone()
    assert ph["dials"] == 2


def test_ingestion_malformed_lines_are_counted_not_fatal(tmp_path):
    conn = make_conn(tmp_path)
    settings = make_settings(tmp_path)
    events = [{"__malformed__": True, "error": "bad json", "raw_head": "{"},
              {"event_id": 2, "type": "lead", "workflow_id": "wf_test",
               "lead_id": "l1", "person_id": "p1", "campaign_id": "c1",
               "ts": "2027-01-04T10:00:00+05:30",
               "features": {"lang": "hi", "city_tier": "1", "age_band": "31-45",
                            "value_band": "mid"}}]
    ing = ingestion.Ingestor(conn, FakeEvents(events), settings, "test",
                             {"wf_test": {"workflow_id": "wf_test"}})
    out = ing.ingest_pending()
    assert out["malformed"] == 1 and out["leads"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM ingestion_errors").fetchone()["n"] == 1


def test_day_hour_uses_the_simulated_ist_clock():
    assert ingestion.day_hour("2027-01-04T00:00:00+05:30") == (0, 0)
    assert ingestion.day_hour("2027-01-05T13:45:00+05:30") == (1, 13)


# --------------------------------------------------------------------------- ledger
def test_ledger_is_append_only_and_counterfactuals_are_labelled(tmp_path):
    conn = make_conn(tmp_path)
    ledger = DecisionLedger(conn, "test", "test")
    did = ledger.record(
        decision_type="test", decision_status="observed", summary="s",
        decision_statement="d", source=[{"type": "unit", "reference": "x"}],
        evidence={"ok": True}, sim_minute=0,
        counterfactual={"observed": False, "statement": "would have applied",
                        "conditions": {"status": "approved"}})
    with pytest.raises(Exception):
        conn.execute("UPDATE decisions SET summary='x' WHERE decision_id=?", (did,))
    with pytest.raises(Exception):
        conn.execute("DELETE FROM decisions WHERE decision_id=?", (did,))
    assert ledger.get(did)["counterfactual"]["observed"] is False

    with pytest.raises(LedgerError):
        ledger.record(decision_type="bad", decision_status="observed", summary="s",
                      decision_statement="d", source=[], evidence={})
    with pytest.raises(LedgerError):
        ledger.record(decision_type="bad", decision_status="observed", summary="s",
                      decision_statement="d",
                      source=[{"type": "unit", "reference": "x"}], evidence={},
                      counterfactual={"observed": True, "statement": "claimed"})

    assert _minute_from_ts("2027-01-04T10:00:00+05:30") == 600


# --------------------------------------------------------------------------- experiments
def test_desired_distribution_keeps_live_split_after_a_no_winner_close():
    wf = workflow()
    cfg = base_config()
    cfg["allocations"]["default"]["voice"] = {"V1": 0.4, "V2": 0.6}
    state = {"studies": {"voice": {"status": "closed", "close_reason": "no_winner",
                                   "baseline": "V1", "allocation": {"V1": 0.4, "V2": 0.6}}},
             "promotions": {}}
    dist = experiments.desired_distribution(wf, cfg, state, "voice", make_obj_settings())
    assert dist == {"V1": 0.4, "V2": 0.6}


def test_desired_distribution_after_promotion_keeps_a_control_arm():
    wf = workflow()
    cfg = base_config()
    state = {"studies": {"voice": {"status": "closed", "close_reason": "promote",
                                   "baseline": "V1", "allocation": {}}},
             "promotions": {"voice": {"value": "V2", "baseline": "V1"}}}
    dist = experiments.desired_distribution(wf, cfg, state, "voice",
                                            make_obj_settings(control_share=0.15))
    assert dist["V2"] == pytest.approx(0.85)
    assert dist["V1"] == pytest.approx(0.15)


def test_did_replacement_pool_never_reuses_historically_flagged_ids(tmp_path):
    conn = make_conn(tmp_path)
    conn.execute("INSERT INTO incidents(incident_id,workflow_id,kind,status,detected_day) "
                 "VALUES('did:wf_test:D2','wf_test','did_low_pickup','resolved',2)")
    conn.execute("INSERT INTO incidents(incident_id,workflow_id,kind,status,detected_day) "
                 "VALUES('did:wf_test:D1','wf_test','did_low_pickup','open',5)")
    conn.commit()

    # D1 is currently flagged, D2 flagged before: only never-flagged D3 qualifies.
    assert incidents._healthy_pool(conn, "wf_test", ["D1", "D2", "D3"], {"D1"}) == ["D3"]
    # If every option has a history, fall back to not-currently-flagged IDs.
    assert incidents._healthy_pool(conn, "wf_test", ["D1", "D2"], {"D1"}) == ["D2"]
    # Another workflow's history must not leak in.
    assert incidents._healthy_pool(conn, "wf_other", ["D1", "D2"], set()) == ["D1", "D2"]

    # If every option is historically bad, re-admit the best last observed
    # pickup rather than diluting traffic across a known-dead caller ID.
    conn.executemany(
        "INSERT INTO did_daily(workflow_id,day,caller_id,dials,connected) "
        "VALUES('wf_test',6,?,1000,?)",
        [("D1", 300), ("D2", 90)])
    conn.commit()
    assert incidents._healthy_pool(conn, "wf_test", ["D1", "D2", "D3"],
                                   {"D3"}) == ["D1"]


class FakePromptSim:
    def __init__(self, pass_rates):
        self.pass_rates = pass_rates
        self.validated = []

    def suggest_fixes(self, wid, text):
        return {"suggestions": [{"prompt_version": "P2"}]}

    def state(self):
        return {"credits_remaining": 100}

    def run_validation(self, wid, pv, n):
        self.validated.append(pv)
        return {"prompt_version": pv, "pass_rate": self.pass_rates[pv],
                "validation_run_id": f"vr_{pv}", "credits_remaining": 100}


def test_prompt_fix_prefers_observed_engagement_among_safe_candidates(tmp_path):
    conn = make_conn(tmp_path)
    # P2 validates marginally higher but P1 is the proven incumbent.
    conn.executemany(
        "INSERT INTO prompt_daily(workflow_id,prompt_version,day,dials,connected,engaged,halluc) "
        "VALUES('wf_test',?,0,1000,1000,?,0)",
        [("P1", 490), ("P2", 400)])
    conn.commit()
    client = FakePromptSim({"P1": 0.99, "P2": 0.995})
    ledger = DecisionLedger(conn, "test", "test")
    wf = workflow(options={**workflow()["options"], "prompt_version": ["P1", "P2", "P3"]})
    winner = incidents.choose_replacement_prompt(
        conn, client, ledger, wf, base_config(prompt="P3"), "inc", 600,
        make_settings(tmp_path))
    assert winner["prompt_version"] == "P1"
    # The LLM hint only ordered validation; both candidates were still tested.
    assert set(client.validated) == {"P1", "P2"}


def test_did_rotation_is_skipped_when_every_alternative_is_worse(tmp_path):
    conn = make_conn(tmp_path)
    conn.executemany(
        "INSERT INTO did_daily(workflow_id,day,caller_id,dials,connected) "
        "VALUES('wf_test',7,?,1000,?)",
        [("D1", 210), ("D2", 90)])
    conn.commit()
    # D1 is only worth rotating to if it beats the live ID's recent pickup.
    assert incidents._better_alternative(conn, "wf_test", "D1", 0.22) is False
    assert incidents._better_alternative(conn, "wf_test", "D1", 0.18) is True
    assert incidents._better_alternative(conn, "wf_test", "D2", 0.18) is False


def test_cohort_plan_builds_unique_ordered_ids():
    state = {"cohort_wins": {
        "voice": {"where": {"lang": ["hi"]}, "value": "V2", "baseline": "V1",
                  "options": ["V1", "V2"]},
        "welcome_message": {"where": {"age_band": ["60+"]}, "value": "W2",
                            "baseline": "W1", "options": ["W1", "W2"]},
    }}
    plan = experiments.cohort_plan(workflow(), state, make_obj_settings())
    ids = [p["id"] for p in plan]
    assert len(ids) == len(set(ids))
    assert any("lang_hi" in i for i in ids) and any("age_band_60plus" in i for i in ids)


# --------------------------------------------------------------------------- schedule
def test_bounded_hour_weights_keep_every_window_hour_and_clip_extremes():
    rates = {9: (10, 100), 10: (90, 100), 11: (50, 100)}
    weights = cohorts.bounded_hour_weights(rates, [9, 12], min_weight=0.3, max_weight=3.0)
    assert set(weights) == {"9", "10", "11"}
    assert all(0.3 <= w <= 3.0 for w in weights.values())
    assert weights["10"] > weights["9"]


def make_obj_settings(**kw):
    return Settings(**kw)
