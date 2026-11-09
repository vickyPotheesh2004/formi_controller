"""Controller orchestration: one loop, every decision recorded.

The engine owns no statistics and no governance policy.  It wires the HTTP
client, the event store, the governance client and the decision modules
together, then advances the simulator in bounded steps.  Every action that can
change a client's agent goes through ``Governance``; every observation that
supports a decision goes through the append-only ledger.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from . import analytics, cohorts, db, experiments, incidents, schedule
from .api import SimClient, SimError
from .config import Settings, default_settings
from .governance import Governance
from .ingestion import Ingestor
from .ledger import DecisionLedger, source_ref
from .report import write_report

log = logging.getLogger("controller.engine")

SUPERVISOR_COOLDOWN_MIN = int(os.environ.get("CONTROLLER_SUPERVISOR_COOLDOWN", str(12 * 60)))
SCHEDULE_INTERVAL_MIN = int(os.environ.get("CONTROLLER_SCHEDULE_INTERVAL", str(12 * 60)))
PROMPT_RETRY_MIN = int(os.environ.get("CONTROLLER_PROMPT_RETRY", str(6 * 60)))
STUDY_DIMS = ("welcome_message", "voice")


def _same_dist(a: dict, b: dict) -> bool:
    keys = set(a) | set(b)
    return all(abs(float(a.get(k, 0)) - float(b.get(k, 0))) < 1e-6 for k in keys)


class Engine:
    def __init__(self, client: SimClient, settings: Settings | None = None,
                 run_id: str | None = None, scenario: str | None = None):
        self.client = client
        self.settings = settings or default_settings()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.run_id = run_id or f"run-{stamp}-{uuid.uuid4().hex[:6]}"
        self.scenario = scenario or "unknown"
        self.conn = db.connect(self.settings.db_file())
        db.init_schema(self.conn)
        self.ledger = DecisionLedger(self.conn, self.run_id, self.scenario)
        self.workflows: dict[str, dict] = {}
        self.governance: Governance | None = None
        self.ingestor: Ingestor | None = None
        self.sim_minute = 0
        self.next_eval = 0
        self.next_schedule = 0
        self.next_report = 0

    # ------------------------------------------------------------------ setup
    def _check_world(self, state: dict) -> None:
        key = {"scenario": state.get("scenario"), "end_minute": state.get("end_minute")}
        world = db.get_meta(self.conn, "world")
        minute = int(state.get("sim_minute") or 0)
        reset = False
        if world:
            reset = (world.get("scenario") != key["scenario"]
                     or minute < int(world.get("seen_minute", -1)))
        else:
            self.ledger.record(
                decision_type="world_observed",
                decision_status="observed",
                summary=f"Attached to simulator world {key['scenario']}",
                decision_statement=("The controller connected to a new simulator world. No prior "
                                    "analytics exist for it; history is ingested before the first "
                                    "decision."),
                source=[source_ref("simulator_api", "sim/state", endpoint="/sim/state")],
                evidence={"state": state}, sim_minute=minute,
                policy_reference="ingestion.bootstrap", selected_action="ingest_history",
                risk_assessment="Acting before history is ingested would waste traffic and credits.",
            )
        if reset:
            db.reset_analytics(self.conn)
            db.set_meta(self.conn, "supervisor_edits", {})
            self.ledger.record(
                decision_type="world_reset",
                decision_status="observed",
                summary="Simulator world changed; analytics reset",
                decision_statement=("The simulator reports a different scenario or an earlier clock "
                                    "than the last observed run. World-scoped aggregates are cleared; "
                                    "the decision ledger is append-only and is kept."),
                source=[source_ref("simulator_api", "sim/state", endpoint="/sim/state"),
                        source_ref("controller_database", "meta:world")],
                evidence={"new": key, "previous": world, "previous_minute":
                          (world or {}).get("seen_minute")},
                sim_minute=minute, policy_reference="ingestion.world_identity",
                selected_action="reset_analytics",
                risk_assessment="Keeping the old world's aggregates would poison every decision.",
            )
        db.set_meta(self.conn, "world", {**key, "seen_minute": minute, "run_id": self.run_id})

    def _store_workflows(self) -> None:
        with db.transaction(self.conn):
            for wid, wf in self.workflows.items():
                self.conn.execute(
                    "INSERT INTO workflows(workflow_id,tenant_id,public_json,updated_sim_minute) "
                    "VALUES(?,?,?,?) ON CONFLICT(workflow_id) DO UPDATE SET "
                    "tenant_id=excluded.tenant_id,public_json=excluded.public_json,"
                    "updated_sim_minute=excluded.updated_sim_minute",
                    (wid, wf.get("tenant_id"), db.json_dumps(wf), self.sim_minute))

    def bootstrap(self) -> None:
        self.client.wait_ready()
        state = self.client.state()
        self.scenario = state.get("scenario") or self.scenario
        self.ledger.scenario = self.scenario
        self.sim_minute = int(state.get("sim_minute") or 0)
        self._check_world(state)
        self.workflows = {w["workflow_id"]: w for w in self.client.workflows()}
        self._store_workflows()
        self.governance = Governance(self.conn, self.client, self.settings, self.ledger,
                                     self.workflows, self.run_id)
        self.ingestor = Ingestor(self.conn, self.client, self.settings, self.run_id,
                                 self.workflows)
        for wid in self.workflows:
            self.governance.reconcile_history(wid, self.sim_minute)
            try:
                self.governance.sync_config(wid, self.sim_minute)
            except SimError as e:
                log.warning("initial config sync failed", extra={"workflow_id": wid, "error": str(e)})
        self.governance.reconcile_approvals(self.sim_minute)
        self.ingestor.ingest_pending()
        self._sync_supervisor_edits(self.sim_minute)
        db.set_meta(self.conn, "credits", state.get("credits_remaining"))
        self.next_eval = self.sim_minute
        self.next_schedule = self.sim_minute
        self.next_report = self.sim_minute
        log.info("bootstrap complete", extra={"scenario": self.scenario,
                                              "workflows": len(self.workflows),
                                              "sim_minute": self.sim_minute,
                                              "events": self.ingestor.total_events})

    # ------------------------------------------------------------------ loop
    def run(self) -> None:
        self.bootstrap()
        finished = False
        while not finished:
            state = self.client.state()
            if state.get("finished"):
                break
            tick = int(state.get("tick_minutes") or 15)
            remaining = int(state.get("end_minute") or 0) - int(state.get("sim_minute") or 0)
            step = min(self.settings.step_minutes, remaining)
            step = max(tick, (step // tick) * tick)
            resp = self.client.advance(step)
            self.sim_minute = int(resp.get("sim_minute") or self.sim_minute + step)
            finished = bool(resp.get("finished"))
            self._after_advance(resp)
            db.set_meta(self.conn, "world", {
                "scenario": state.get("scenario"), "end_minute": state.get("end_minute"),
                "seen_minute": self.sim_minute, "run_id": self.run_id,
            })
        self._finish()

    def _after_advance(self, resp: dict) -> None:
        try:
            self.ingestor.ingest_pending()
        except Exception as e:  # keep the clock moving; the next tick retries
            log.warning("ingestion failed; will retry", extra={"error": str(e)})
        self._sync_configs()
        self._sync_supervisor_edits(self.sim_minute)
        try:
            self.governance.poll_and_apply(self.sim_minute)
        except Exception as e:
            log.warning("approval polling failed", extra={"error": str(e)})
        self._incident_tick()
        if self.sim_minute >= self.next_eval:
            self.next_eval = self.sim_minute + self.settings.eval_interval_minutes
            self._evaluation_tick()
        if self.sim_minute >= self.next_schedule:
            self.next_schedule = self.sim_minute + SCHEDULE_INTERVAL_MIN
            self._schedule_tick()
        if self.sim_minute >= self.next_report:
            self.next_report = self.sim_minute + self.settings.report_interval_minutes
            self._snapshot(final=False)

    def _finish(self) -> None:
        self._snapshot(final=True)
        log.info("run finished", extra={"scenario": self.scenario, "run_id": self.run_id,
                                        "sim_minute": self.sim_minute})

    # ------------------------------------------------------------------ config
    def _sync_configs(self) -> None:
        for wid in self.workflows:
            try:
                self.governance.sync_config(wid, self.sim_minute)
            except SimError as e:
                log.warning("config sync failed", extra={"workflow_id": wid, "error": str(e)})

    def _config(self, wid: str) -> tuple[int, dict]:
        return self.governance.current_config(wid)

    def _sync_supervisor_edits(self, sim_minute: int) -> None:
        edits = db.get_meta(self.conn, "supervisor_edits", {}) or {}
        changed = False
        for wid, wf in self.workflows.items():
            rows = self.conn.execute(
                "SELECT version,changed_dims_json FROM config_versions "
                "WHERE workflow_id=? AND actor='supervisor' ORDER BY version", (wid,)).fetchall()
            for row in rows:
                rec = edits.get(wid) or {}
                version = int(row["version"])
                if version <= int(rec.get("version", 0)):
                    continue
                try:
                    dims = json.loads(row["changed_dims_json"] or "[]")
                except (TypeError, ValueError):
                    dims = []
                edits[wid] = {"version": version, "dims": dims, "seen_minute": sim_minute}
                changed = True
                self.ledger.record(
                    decision_type="supervisor_edit",
                    decision_status="observed",
                    summary=f"Supervisor changed {wid} (version {version})",
                    decision_statement=(f"A client supervisor applied version {version}, changing "
                                        f"{dims or ['unknown dimensions']}. The controller treats the "
                                        "new version as the live baseline, records the change and "
                                        "will not propose a reversal of the same dimensions during "
                                        f"the {SUPERVISOR_COOLDOWN_MIN // 60}h cooldown."),
                    source=[source_ref("simulator_api", f"workflows/{wid}/config/history",
                                       endpoint=f"/workflows/{wid}/config/history"),
                            source_ref("controller_database", f"config_version:{wid}:{version}")],
                    evidence={"version": version, "changed_dims": dims},
                    workflow_id=wid, tenant_id=wf.get("tenant_id"), sim_minute=sim_minute,
                    policy_reference="governance.supervisor_edits",
                    selected_action="adopt_as_baseline",
                    risk_assessment=("Reverting a supervisor edit immediately would breach the "
                                     "governance boundary and confound the running experiment."),
                )
                self._reset_after_supervisor(wid, dims, sim_minute)
        if changed:
            db.set_meta(self.conn, "supervisor_edits", edits)

    def _reset_after_supervisor(self, wid: str, dims: list[str], sim_minute: int) -> None:
        state = experiments.load_state(self.conn, wid)
        touched = False
        for dim in STUDY_DIMS:
            if dim not in dims:
                continue
            state.setdefault("supervisor_dims", {})[dim] = sim_minute
            touched = True
            st = state.get("studies", {}).get(dim)
            if st and st.get("status") != "closed":
                st["status"] = "closed"
                st["closed_minute"] = sim_minute
                st["close_reason"] = "supervisor_edit"
            if state.get("promotions", {}).pop(dim, None) is not None:
                touched = True
        if touched:
            experiments.save_state(self.conn, wid, state)

    def _supervisor_blocks(self, wid: str, dims: list[str], sim_minute: int) -> bool:
        sup = (db.get_meta(self.conn, "supervisor_edits", {}) or {}).get(wid)
        if not sup:
            return False
        if sim_minute >= int(sup.get("seen_minute", 0)) + SUPERVISOR_COOLDOWN_MIN:
            return False
        return bool(set(dims) & set(sup.get("dims") or []))

    # ------------------------------------------------------------------ propose
    def _propose(self, wid: str, wf: dict, proposed: dict, *, kind: str, summary: str,
                 rationale: str, evidence: dict, impact: float, sim_minute: int,
                 allow_auto: bool = True, ignore_supervisor_gate: bool = False) -> dict | None:
        _, current = self._config(wid)
        dims = cohorts.changed_dims(current, proposed)
        if not dims:
            return None
        if not ignore_supervisor_gate and self._supervisor_blocks(wid, dims, sim_minute):
            log.info("proposal blocked by supervisor cooldown",
                     extra={"workflow_id": wid, "dims": dims})
            return None
        day = sim_minute // 1440
        affected = analytics.active_leads(self.conn, wid, {}, day)
        dry_run = {
            "method": "historical_cohort_replay_estimate",
            "window": [max(-9999, day - 7), day],
            "affected_leads": affected,
            "note": ("Estimate from observed aggregate rates for the matching population; it is "
                     "not a counterfactual and is labelled as an estimate."),
        }
        return self.governance.propose(
            wid=wid, proposed_config=proposed, change_summary=summary, rationale=rationale,
            evidence=evidence, affected_leads=affected, dry_run=dry_run,
            impact_score=max(0.0, min(1.0, float(impact))), kind=kind,
            sim_minute=sim_minute, allow_auto=allow_auto,
        )

    # ------------------------------------------------------------------ incidents
    def _incident_tick(self) -> None:
        for wid, wf in self.workflows.items():
            try:
                _, config = self._config(wid)
                for action in incidents.detect_did_incidents(
                        self.conn, wf, config, self.sim_minute, self.settings):
                    if action.get("action") == "observe":
                        incidents.record_avoided_did(self.ledger, wf, self.sim_minute,
                                                     action["evidence"])
                        continue
                    if action.get("proposed_config"):
                        self._record_incident_detected(wid, wf, action)
                        self._propose(
                            wid, wf, action["proposed_config"], kind="incident_did_rotate",
                            summary=action["summary"], rationale=action["rationale"],
                            evidence=action["evidence"], impact=action["impact"],
                            sim_minute=self.sim_minute, allow_auto=True,
                            ignore_supervisor_gate=False,
                        )
            except Exception as e:
                log.warning("caller-ID detection failed", extra={"workflow_id": wid,
                                                                 "error": str(e)})

    def _record_incident_detected(self, wid: str, wf: dict, action: dict) -> None:
        iid = action.get("incident_id")
        prior = self.conn.execute(
            "SELECT 1 FROM decisions WHERE decision_type='incident_did_detected' "
            "AND result_reference=? LIMIT 1", (iid,)).fetchone()
        if prior:
            return
        ev = action.get("evidence") or {}
        self.ledger.record(
            decision_type="incident_did_detected",
            decision_status="detected",
            summary=action.get("summary") or f"Caller ID incident {iid}",
            decision_statement=(f"Caller ID {ev.get('caller_id')} pickup fell from "
                                f"{ev.get('baseline_pickup')} to {ev.get('recent_pickup')} "
                                f"on {ev.get('recent', {}).get('dials')} recent dials "
                                f"(relative {ev.get('relative_change')}, p={ev.get('p_value')}). "
                                "The controller is proposing to move its share to the remaining "
                                "caller IDs through the governance path."),
            source=[source_ref("controller_database", f"did_daily:{wid}"),
                    source_ref("controller_database", f"incident:{iid}")],
            evidence=ev, workflow_id=wid, tenant_id=wf.get("tenant_id"),
            sim_minute=self.sim_minute, policy_reference="incident.caller_id_health",
            selected_action="rotate_caller_id", result_reference=iid,
            risk_assessment="A false positive spends approver attention but does not change scripts.",
        )

    def _prompt_tick(self) -> None:
        for wid, wf in self.workflows.items():
            try:
                version, config = self._config(wid)
                row = self.conn.execute(
                    "SELECT actor FROM config_versions WHERE workflow_id=? AND version=?",
                    (wid, version)).fetchone()
                actor = (row["actor"] if row else "") or ""
                actions = incidents.detect_prompt_incidents(
                    self.conn, wf, config, version, actor, self.sim_minute, self.settings)
                if not actions:
                    incidents.resolve_prompt_incidents(
                        self.conn, self.ledger, wf, config, self.sim_minute, self.settings)
                    continue
                for action in actions:
                    if action.get("action") not in ("fix", "existing"):
                        continue
                    if self._prompt_retry_blocked(wid):
                        continue
                    ev = action.get("evidence") or {}
                    bad = action.get("bad_prompt") or ev.get("prompt_version") or config.get(
                        "prompt_version")
                    fix = incidents.prompt_fix_action(
                        self.conn, self.ledger, wf, config, action["incident_id"], bad,
                        self.sim_minute, self.settings, self.client)
                    self._touch(wid, last_prompt_sim_minute=self.sim_minute)
                    if not fix:
                        continue
                    self.ledger.record(
                        decision_type="incident_prompt_fix",
                        decision_status="decided",
                        summary=fix["summary"],
                        decision_statement=(fix["rationale"] + " The change is routed through the "
                                            "human approval path even where auto-apply is enabled."),
                        source=[source_ref("controller_database",
                                           f"incident:{action['incident_id']}"),
                                source_ref("simulator_api", "validation/runs",
                                           endpoint="/validation/runs")],
                        evidence=fix["evidence"], workflow_id=wid,
                        tenant_id=wf.get("tenant_id"), sim_minute=self.sim_minute,
                        policy_reference="incident.prompt_safety",
                        selected_action="request_approval",
                        alternatives=[config.get("prompt_version")],
                        risk_assessment=("A validated prompt can still reduce engagement; the "
                                         "approver and post-apply monitoring remain in control."),
                    )
                    self._propose(
                        wid, wf, fix["proposed_config"], kind="incident_prompt_fix",
                        summary=fix["summary"], rationale=fix["rationale"],
                        evidence=fix["evidence"], impact=fix["impact"],
                        sim_minute=self.sim_minute, allow_auto=False,
                        ignore_supervisor_gate=True,
                    )
                incidents.resolve_prompt_incidents(
                    self.conn, self.ledger, wf, config, self.sim_minute, self.settings)
            except Exception as e:
                log.warning("prompt incident handling failed", extra={"workflow_id": wid,
                                                                     "error": str(e)})

    def _prompt_retry_blocked(self, wid: str) -> bool:
        row = self.conn.execute(
            "SELECT last_prompt_sim_minute FROM workflow_state WHERE workflow_id=?",
            (wid,)).fetchone()
        last = int(row["last_prompt_sim_minute"] or 0) if row else 0
        return self.sim_minute - last < PROMPT_RETRY_MIN

    # ------------------------------------------------------------------ evaluation
    def _evaluation_tick(self) -> None:
        for wid, wf in self.workflows.items():
            try:
                version, config = self._config(wid)
                state = experiments.load_state(self.conn, wid)
                created = experiments.ensure_studies(
                    self.conn, wf, config, state, self.sim_minute, self.settings)
                for action in created:
                    self._record_experiment(wid, wf, action)
                started = experiments.mark_started(wf, config, state, self.sim_minute)
                for action in started:
                    self._record_experiment(wid, wf, action)
                actions = experiments.evaluate(
                    self.conn, wf, config, state, self.sim_minute, self.settings)
                for action in actions:
                    self._record_experiment(wid, wf, action)
                # Retry a confirmed cohort plan every tick. A proposal deferred
                # outside approver hours or blocked by a pending request used to
                # be lost forever when it was only attempted on the winner tick.
                if state.get("cohort_wins"):
                    self._apply_cohort_plan(wid, wf, state)
                experiments.save_state(self.conn, wid, state)
                self._sync_study_allocation(wid, wf, state)
                drift = incidents.detect_engagement_drift(
                    self.conn, wf, self.sim_minute, self.settings)
                if drift:
                    self._record_drift(wid, wf, drift)
            except Exception as e:
                log.warning("evaluation failed", extra={"workflow_id": wid, "error": str(e)})
        self._prompt_tick()

    def _sync_study_allocation(self, wid: str, wf: dict, state: dict) -> None:
        if not (state.get("studies") or state.get("promotions")):
            return
        _, config = self._config(wid)
        proposed = copy.deepcopy(config)
        default = proposed.setdefault("allocations", {}).setdefault("default", {})
        changed = False
        for dim in STUDY_DIMS:
            if dim not in state.get("studies", {}) and dim not in state.get("promotions", {}):
                continue
            dist = experiments.desired_distribution(wf, proposed, state, dim, self.settings)
            if dist and not _same_dist(default.get(dim) or {}, dist):
                default[dim] = {k: float(v) for k, v in dist.items() if v > 0}
                changed = True
        if not changed:
            return
        evidence = {
            "studies": {d: {"status": s.get("status"), "started_day": s.get("started_day"),
                            "allocation": s.get("allocation")}
                        for d, s in state.get("studies", {}).items()},
            "promotions": {d: {"value": p.get("value"), "control": p.get("control"),
                               "at_minute": p.get("at_minute")}
                           for d, p in state.get("promotions", {}).items()},
            "deterministic_design": True,
        }
        self._propose(
            wid, wf, proposed, kind="experiment_allocation",
            summary="Update experiment traffic split for " + wid,
            rationale=("The randomised split follows the controller's declared experiment design: "
                       "a permanent control arm and challenger mass. No winner is declared by this "
                       "change; the split only keeps the comparison powered."),
            evidence=evidence, impact=0.35, sim_minute=self.sim_minute, allow_auto=True,
        )

    def _apply_cohort_plan(self, wid: str, wf: dict, state: dict) -> None:
        plan = experiments.cohort_plan(wf, state, self.settings)
        if not plan:
            return
        _, config = self._config(wid)
        proposed = copy.deepcopy(config)
        existing = {c.get("id") for c in proposed.get("cohorts") or []}
        added = []
        for entry in plan:
            if entry["id"] in existing:
                continue
            proposed.setdefault("cohorts", []).append(
                {"id": entry["id"], "where": entry["where"]})
            proposed.setdefault("allocations", {})[entry["id"]] = entry["allocations"]
            added.append(entry["id"])
        if not added:
            return
        self._propose(
            wid, wf, proposed, kind="cohort_targeting",
            summary=f"Target confirmed feature cohort(s) for {wid}",
            rationale=("A feature-by-variant cell survived repeated deterministic tests and its "
                       "population share is material. Existing cohorts keep their order and are "
                       "not overwritten; the new cohort is appended."),
            evidence={"cohorts": plan, "added": added},
            impact=0.55, sim_minute=self.sim_minute, allow_auto=True,
        )

    def _record_experiment(self, wid: str, wf: dict, action: dict) -> None:
        kind = action.get("type")
        dim = action.get("dim")
        ev = action.get("evidence") if isinstance(action.get("evidence"), dict) else action
        statements = {
            "study_created": (
                "A randomised study was created for this dimension. The baseline option keeps "
                "the majority of traffic until a challenger passes the deterministic tests."),
            "study_started": (
                "The live default allocation now matches the declared split; the observation "
                "window starts at this simulated minute."),
            "promote": (
                "The challenger met the minimum sample, practical-effect, significance and "
                "repeated-confirmation rules. Promotion keeps a control arm for decay checks."),
            "demote": (
                "The promoted option's recent rate fell against its own earlier window while "
                "the control field did not fall by the same amount. The promotion is withdrawn."),
            "drop_candidate": (
                "A challenger was clearly worse than the baseline on repeated tests; its traffic "
                "is returned to the remaining arms."),
            "allocation_changed": (
                "The traffic split changed because a losing challenger was removed. This is a "
                "design change, not a winner promotion."),
            "close_study": (
                "The study window ended without a challenger meeting the promotion rules. The "
                "baseline is retained rather than guessing."),
            "cohort_win": (
                "A feature-by-variant cell passed repeated deterministic tests and is large "
                "enough to target with a cohort."),
        }
        status = "decided" if kind in ("promote", "demote", "drop_candidate",
                                       "allocation_changed", "cohort_win") else "observed"
        self.ledger.record(
            decision_type=f"experiment_{kind}",
            decision_status=status,
            summary=action.get("summary") or f"Experiment {kind} on {wid} {dim or ''}".strip(),
            decision_statement=statements.get(kind, "Experiment state changed."),
            source=[source_ref("controller_database", f"variant_daily:{wid}"),
                    source_ref("controller_database", f"experiment:{wid}:{dim or 'all'}")],
            evidence=ev, workflow_id=wid, tenant_id=wf.get("tenant_id"),
            sim_minute=self.sim_minute,
            policy_reference="experiment.deterministic_evaluation",
            selected_action=kind,
            alternatives=[action.get("baseline")] if action.get("baseline") else [],
            expected_impact=({"relative_lift": (ev or {}).get("relative_lift")}
                             if kind == "promote" else {}),
            risk_assessment=("Frequentist fixed-horizon test with Bonferroni correction and "
                             "repeated confirmation; a false discovery remains possible."),
        )

    def _record_drift(self, wid: str, wf: dict, drift: dict) -> None:
        self.ledger.record(
            decision_type="engagement_drift_detected",
            decision_status="observed",
            summary=f"Engagement drift detected on {wid}",
            decision_statement=("A broad engagement drop passed the statistical threshold. This is "
                                "an audit signal only: the experiment layer owns rollback, and the "
                                "controller will not invent an explanation it cannot attribute."),
            source=[source_ref("controller_database", f"variant_daily:{wid}")],
            evidence=drift.get("evidence") or drift, workflow_id=wid,
            tenant_id=wf.get("tenant_id"), sim_minute=self.sim_minute,
            policy_reference="detection.engagement_drift", selected_action="monitor",
            result_reference=drift.get("incident_id"),
            risk_assessment="A shared drop can be external; acting on it without attribution is unsafe.",
        )

    # ------------------------------------------------------------------ schedule
    def _schedule_tick(self) -> None:
        for wid, wf in self.workflows.items():
            try:
                _, config = self._config(wid)
                plan = schedule.desired_schedule(
                    self.conn, wf, config, self.sim_minute, self.settings)
                if not plan:
                    continue
                proposed = schedule.proposed_config(config, plan["schedule"])
                result = self._propose(
                    wid, wf, proposed, kind="schedule_learning",
                    summary=plan["summary"], rationale=plan["rationale"],
                    evidence=plan["evidence"], impact=plan["impact"],
                    sim_minute=self.sim_minute, allow_auto=True,
                )
                if result is not None:
                    self._touch(wid, last_schedule_sim_minute=self.sim_minute)
            except Exception as e:
                log.warning("schedule learning failed", extra={"workflow_id": wid,
                                                              "error": str(e)})

    # ------------------------------------------------------------------ reports
    def _snapshot(self, final: bool) -> None:
        try:
            state = self.client.state()
            db.set_meta(self.conn, "credits", state.get("credits_remaining"))
            report = write_report(self.conn, self.workflows, self.sim_minute,
                                  self.settings.state_dir, run_id=self.run_id,
                                  scenario=self.scenario, final=final, state=state)
            if final:
                log.info("final report written", extra={"path": report.get("path")})
        except Exception as e:
            log.warning("report snapshot failed", extra={"error": str(e)})

    # ------------------------------------------------------------------ misc
    def _touch(self, wid: str, **fields: Any) -> None:
        notes = db.load_notes(self.conn, wid)
        db.save_notes(self.conn, wid, notes, **fields)

    def close(self) -> None:
        try:
            self.conn.close()
        finally:
            self.client.close()
