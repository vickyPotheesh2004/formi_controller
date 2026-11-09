"""Governance client: proposals, approvals, auto-apply, version safety.

The controller never applies a change outside the simulator's documented
governance paths.  Auto-apply is attempted only after the local policy check
agrees with the workflow's policy; prompt changes always take the human
approval path even if a policy would technically allow auto-apply, because a
safety validation score is not evidence of improved engagement and compliance
requires a named reviewer for customer-facing scripts.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from typing import Any

from . import analytics, db
from .api import SimClient, SimError
from .cohorts import auto_apply_check, changed_dims, diff_configs, normalise_config
from .config import Settings
from .ledger import DecisionLedger, source_ref

log = logging.getLogger("controller.governance")


def _minute_from_ts(ts: str | None) -> int | None:
    if not ts:
        return None
    try:
        from .config import SIM_START, SIM_TZ
        t = datetime.fromisoformat(ts)
        if t.tzinfo is None:
            t = t.replace(tzinfo=SIM_TZ)
        return int((t - SIM_START).total_seconds() // 60)
    except (TypeError, ValueError):
        return None


class Governance:
    def __init__(self, conn, client: SimClient, settings: Settings, ledger: DecisionLedger,
                 workflows: dict[str, dict], run_id: str):
        self.conn = conn
        self.client = client
        self.settings = settings
        self.ledger = ledger
        self.workflows = workflows
        self.run_id = run_id

    # -- state ---------------------------------------------------------------
    def current_config(self, wid: str) -> tuple[int, dict]:
        row = self.conn.execute(
            "SELECT version,config_json FROM config_versions WHERE workflow_id=? "
            "AND config_json NOT IN ('','{}') ORDER BY version DESC LIMIT 1", (wid,)).fetchone()
        if row:
            return int(row["version"]), json.loads(row["config_json"])
        data = self.client.config(wid)
        cfg, _ = normalise_config(self.workflows[wid], data.get("config") or {})
        return int(data["version"]), cfg

    def sync_config(self, wid: str, sim_minute: int) -> tuple[int, dict]:
        data = self.client.config(wid)
        cfg, problems = normalise_config(self.workflows[wid], data.get("config") or {})
        if problems:
            log.warning("current config failed local validation", extra={"workflow_id": wid,
                                                                       "problems": problems})
        version = int(data["version"])
        existing = self.conn.execute(
            "SELECT actor,changed_dims_json,proposal_id,approval_id FROM config_versions "
            "WHERE workflow_id=? AND version=?", (wid, version)).fetchone()
        with db.transaction(self.conn):
            if existing:
                # The event stream carries authoritative provenance.  A config
                # poll must never erase the actor, changed dimensions or the
                # proposal/approval link recorded by ingestion.
                self.conn.execute(
                    "UPDATE config_versions SET config_json=?,applied_ts=? "
                    "WHERE workflow_id=? AND version=?",
                    (db.json_dumps(cfg), data.get("applied_at"), wid, version))
            else:
                self.conn.execute(
                    "INSERT INTO config_versions(workflow_id,version,actor,changed_dims_json,"
                    "config_json,applied_ts,proposal_id,approval_id) VALUES(?,?,?,?,?,?,?,?)",
                    (wid, version, data.get("actor"), db.json_dumps([]), db.json_dumps(cfg),
                     data.get("applied_at"), None, None))
        return version, cfg

    def reconcile_history(self, wid: str, sim_minute: int) -> None:
        """Merge the simulator's config history into the local audit table.

        Used once at startup (and after a world reset) so a crash between an
        apply and the local commit cannot lose the provenance of a version.
        """
        try:
            history = self.client.config_history(wid)
        except SimError as e:
            log.warning("config history unavailable", extra={"workflow_id": wid, "error": str(e)})
            return
        with db.transaction(self.conn):
            for v in history:
                version = int(v.get("version") or 0)
                if version <= 0:
                    continue
                self.conn.execute(
                    "INSERT INTO config_versions(workflow_id,version,actor,changed_dims_json,"
                    "config_json,applied_ts,proposal_id,approval_id) VALUES(?,?,?,?,'{}',?,?,?) "
                    "ON CONFLICT(workflow_id,version) DO UPDATE SET actor=excluded.actor,"
                    "changed_dims_json=excluded.changed_dims_json,applied_ts=excluded.applied_ts,"
                    "proposal_id=excluded.proposal_id,approval_id=excluded.approval_id",
                    (wid, version, v.get("actor"), db.json_dumps(v.get("changed_dims") or []),
                     v.get("applied_at"), v.get("proposal_id"), v.get("approval_id")))
                # Crash between the simulator apply and the local commit: the
                # history is authoritative, so finish the proposal lifecycle.
                if v.get("proposal_id"):
                    self.conn.execute(
                        "UPDATE proposals SET status='applied',applied_version=?"
                        " WHERE proposal_id=? AND status NOT IN ('applied','stale')",
                        (version, v["proposal_id"]))

    def reconcile_approvals(self, sim_minute: int) -> None:
        """Adopt approvals that exist in the simulator but not yet locally.

        This closes the crash window between ``POST /approvals`` and the local
        insert.  A later ``poll_and_apply`` (or startup) can then finish the
        lifecycle without ever creating a second request.
        """
        for wid, wf in self.workflows.items():
            try:
                approvals = self.client.list_approvals(wid)
            except SimError as e:
                log.warning("approval list failed", extra={"workflow_id": wid, "error": str(e)})
                continue
            for ap in approvals:
                aid = ap.get("approval_id")
                if not aid:
                    continue
                created = _minute_from_ts(ap.get("created_at"))
                expires = _minute_from_ts(ap.get("expires_at"))
                with db.transaction(self.conn):
                    self.conn.execute(
                        "INSERT INTO approvals(approval_id,proposal_id,workflow_id,status,base_version,"
                        "created_sim_minute,expires_sim_minute,apply_by,reasons_json,raw_json) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(approval_id) DO UPDATE SET "
                        "status=excluded.status,expires_sim_minute=excluded.expires_sim_minute,"
                        "apply_by=excluded.apply_by,reasons_json=excluded.reasons_json,"
                        "raw_json=excluded.raw_json",
                        (aid, ap.get("proposal_id") or "", wid, ap.get("status") or "unknown",
                         ap.get("base_version"), created, expires, ap.get("apply_by"),
                         db.json_dumps(ap.get("reasons") or []), db.json_dumps(ap)))
                pid = ap.get("proposal_id") or ""
                local = self.conn.execute("SELECT * FROM proposals WHERE proposal_id=?", (pid,)).fetchone()
                if local is None:
                    self._record_orphan_approval(aid, wid, ap, sim_minute)
                    continue
                st = ap.get("status")
                mapping = {"pending": "approval_pending", "approved": "approved",
                           "rejected": "rejected", "expired": "expired",
                           "lapsed": "lapsed", "applied": "applied"}
                new_status = mapping.get(st)
                if not new_status:
                    continue
                current = local["status"]
                if current == "applied":
                    if st == "applied":
                        with db.transaction(self.conn):
                            self.conn.execute("UPDATE approvals SET applied_version=COALESCE("
                                              "applied_version,?) WHERE approval_id=?",
                                              (local["applied_version"], aid))
                    continue
                if current in ("approval_pending", "approved") or new_status in ("applied", "rejected",
                                                                                 "expired", "lapsed"):
                    with db.transaction(self.conn):
                        self.conn.execute(
                            "UPDATE proposals SET approval_id=COALESCE(approval_id,?),status=? "
                            "WHERE proposal_id=? AND status NOT IN ('applied','stale')",
                            (aid, new_status, pid))

    def _record_orphan_approval(self, aid: str, wid: str, ap: dict, sim_minute: int) -> None:
        existing = self.conn.execute(
            "SELECT 1 FROM decisions WHERE decision_type='approval_orphan' AND result_reference=? "
            "LIMIT 1", (aid,)).fetchone()
        if existing:
            return
        wf = self.workflows[wid]
        self.ledger.record(
            decision_type="approval_orphan",
            decision_status="observed",
            summary=f"Approval {aid} has no local proposal record",
            decision_statement=(f"Reconciliation found approval {aid} in the simulator but no local "
                                "proposal row, which can only happen if the controller crashed between "
                                "the request and its local commit. The approval is not applied blind: "
                                "the controller records it and relies on config history for what was "
                                "actually applied."),
            source=[source_ref("simulator_api", f"approvals/{aid}", endpoint=f"/approvals/{aid}")],
            evidence={"approval": ap}, workflow_id=wid, tenant_id=wf.get("tenant_id"),
            sim_minute=sim_minute, policy_reference="governance.reconciliation",
            selected_action="record_only", approval_status=ap.get("status") or "unknown",
            execution_status="not_applied", result_reference=aid,
            risk_assessment="Applying without the recorded proposed config could overwrite a later edit.",
        )

    def sync_all_configs(self, sim_minute: int) -> None:
        for wid in self.workflows:
            try:
                self.sync_config(wid, sim_minute)
            except Exception as e:
                log.warning("config sync failed", extra={"workflow_id": wid, "error": str(e)})

    def snapshot(self, wid: str, version: int, config: dict, dims: list[str],
                 reason: str, sim_minute: int, decision_id: str | None = None) -> None:
        with db.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO rollback_snapshots(workflow_id,config_version,config_json,changed_dims_json,"
                "reason,created_sim_minute,source_decision_id,status) VALUES(?,?,?,?,?,?,?, 'available')",
                (wid, version, db.json_dumps(config), db.json_dumps(dims), reason,
                 sim_minute, decision_id))

    # -- policy --------------------------------------------------------------
    def _approvals_last_24h(self, wid: str, sim_minute: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) n FROM approvals WHERE workflow_id=? AND created_sim_minute IS NOT NULL "
            "AND created_sim_minute > ?", (wid, sim_minute - 1440)).fetchone()
        return int(row["n"])

    def _approvals_today_global(self, sim_minute: int) -> int:
        day_start = (sim_minute // 1440) * 1440
        row = self.conn.execute(
            "SELECT COUNT(*) n FROM approvals WHERE created_sim_minute IS NOT NULL "
            "AND created_sim_minute >= ?", (day_start,)).fetchone()
        return int(row["n"])

    def _pending(self, wid: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) n FROM proposals WHERE workflow_id=? AND status IN "
            "('approval_pending','approved')", (wid,)).fetchone()
        return int(row["n"])

    def _in_approver_hours(self, wf: dict, sim_minute: int) -> bool:
        lo, hi = (wf.get("governance") or {}).get("approver_hours", [0, 24])
        hour = (sim_minute % 1440) // 60
        return lo <= hour < hi

    def _next_proposal_allowed(self, wid: str) -> int:
        row = self.conn.execute("SELECT next_proposal_sim_minute FROM workflow_state WHERE workflow_id=?",
                                (wid,)).fetchone()
        return int(row["next_proposal_sim_minute"] or 0) if row else 0

    def _set_next_allowed(self, wid: str, sim_minute: int) -> None:
        with db.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO workflow_state(workflow_id,next_proposal_sim_minute) VALUES(?,?) "
                "ON CONFLICT(workflow_id) DO UPDATE SET next_proposal_sim_minute=excluded.next_proposal_sim_minute",
                (wid, sim_minute))

    def _recent_decision(self, wid: str, decision_type: str, sim_minute: int,
                         minutes: int = 360) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM decisions WHERE workflow_id=? AND decision_type=? AND sim_minute>? LIMIT 1",
            (wid, decision_type, sim_minute - minutes)).fetchone()
        return row is not None

    # -- proposal / apply ----------------------------------------------------
    def propose(self, *, wid: str, proposed_config: dict, change_summary: str,
                rationale: str, evidence: dict, affected_leads: int, dry_run: dict,
                impact_score: float, kind: str, sim_minute: int,
                parent_id: str | None = None, allow_auto: bool = True) -> dict:
        wf = self.workflows[wid]
        base_version, current = self.current_config(wid)
        proposed = dict(proposed_config)
        proposed.setdefault("prompt_version", current.get("prompt_version"))
        proposed.setdefault("allocations", current.get("allocations", {}))
        proposed.setdefault("cohorts", current.get("cohorts", []))
        proposed.setdefault("schedule", current.get("schedule", {}))
        clean, problems = normalise_config(wf, proposed)
        if problems:
            decision_id = self.ledger.record(
                decision_type="proposal_validation",
                decision_status="rejected",
                summary="Proposed configuration failed local contract validation",
                decision_statement=("The controller did not submit this change because local validation "
                                    "found problems: " + "; ".join(problems)),
                source=[source_ref("controller_database", f"workflow:{wid}:{base_version}")],
                evidence={"problems": problems, "base_version": base_version},
                workflow_id=wid, tenant_id=wf.get("tenant_id"), sim_minute=sim_minute,
                policy_reference="config_contract", selected_action="do_not_submit",
                approval_status="not_required", execution_status="not_applied",
                risk_assessment="An invalid config would waste an approver request or be rejected.",
            )
            return {"ok": False, "reason": "invalid_config", "problems": problems,
                    "decision_id": decision_id}
        dims = changed_dims(current, clean)
        if not dims:
            decision_id = self.ledger.record(
                decision_type="proposal_no_change",
                decision_status="no_action",
                summary="Proposed configuration is identical to the live configuration",
                decision_statement="No change was submitted because the proposed config has no diff.",
                source=[source_ref("controller_database", f"workflow:{wid}:{base_version}"),
                        source_ref("simulator_api", f"workflows/{wid}/config",
                                   endpoint=f"/workflows/{wid}/config")],
                evidence={"base_version": base_version, "dims": []},
                workflow_id=wid, tenant_id=wf.get("tenant_id"), sim_minute=sim_minute,
                policy_reference="governance", selected_action="do_nothing",
                execution_status="not_applied",
                risk_assessment="Submitting a no-op wastes approver attention.",
            )
            return {"ok": False, "reason": "no_change", "decision_id": decision_id}

        dedupe = hashlib.sha256(db.json_dumps([wid, base_version, clean, kind, parent_id]).encode()).hexdigest()[:16]
        prior = self.conn.execute("SELECT * FROM proposals WHERE dedupe_key=? ORDER BY created_sim_minute DESC",
                                  (dedupe,)).fetchone()
        if prior:
            st = prior["status"]
            if st in ("approval_pending", "approved", "applied", "submitting"):
                return {"ok": True, "suppressed": True, "proposal_id": prior["proposal_id"],
                        "status": st}
            attempts = int(prior["attempts"] or 1)
            if st in ("rejected", "expired", "lapsed", "failed") \
                    and attempts >= self.settings.max_proposal_attempts:
                decision_id = self.ledger.record(
                    decision_type="proposal_resubmission",
                    decision_status="rejected",
                    summary=f"Not resubmitting an unchanged {st} proposal",
                    decision_statement=(f"Proposal {prior['proposal_id']} was {st} after {attempts} "
                                        "attempts. The controller will not resubmit an unchanged "
                                        "request without new evidence or a new base version."),
                    source=[source_ref("controller_database", prior["proposal_id"]),
                            source_ref("simulator_api", f"approvals/{prior['approval_id']}",
                                       endpoint=f"/approvals/{prior['approval_id']}")],
                    evidence={"proposal_id": prior["proposal_id"], "status": st,
                              "attempts": attempts, "base_version": base_version},
                    workflow_id=wid, tenant_id=wf.get("tenant_id"), sim_minute=sim_minute,
                    policy_reference="no_repeat_without_new_evidence",
                    selected_action="do_not_resubmit", approval_status=st,
                    execution_status="not_applied",
                    risk_assessment="Repeated unchanged requests waste approver attention and breach fairness.",
                )
                return {"ok": False, "reason": "resubmission_suppressed", "decision_id": decision_id}
        attempt = int(prior["attempts"] or 1) + 1 if prior else 1

        auto = auto_apply_check(wf, current, clean)
        auto_mode = bool(allow_auto and auto["allowed"] and "prompt_version" not in dims)
        if auto_mode:
            return self._auto_apply(wid, wf, current, clean, base_version, dims, auto,
                                    change_summary, rationale, evidence, sim_minute,
                                    dedupe, kind, parent_id, attempt)
        return self._request_approval(wid, wf, current, clean, base_version, dims,
                                      change_summary, rationale, evidence, affected_leads,
                                      dry_run, impact_score, sim_minute, dedupe, kind,
                                      parent_id, auto, attempt)

    def _auto_apply(self, wid: str, wf: dict, current: dict, proposed: dict, base_version: int,
                    dims: list[str], auto: dict, summary: str, rationale: str, evidence: dict,
                    sim_minute: int, dedupe: str, kind: str, parent_id: str | None,
                    attempt: int = 1) -> dict:
        proposal_id = f"ctl-auto-{wid}-{dedupe}-a{attempt}"
        existing = self.conn.execute("SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
        if existing and existing["status"] == "applied":
            return {"ok": True, "suppressed": True, "proposal_id": proposal_id, "status": "applied"}
        with db.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO proposals(proposal_id,workflow_id,kind,parent_id,base_version,"
                "proposed_config_json,change_summary,diff_json,dry_run_json,affected_leads,impact_score,"
                "rationale,evidence_json,mode,status,created_sim_minute,attempts,dedupe_key) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?, 'auto','submitting',?,?,?) "
                "ON CONFLICT(proposal_id) DO UPDATE SET status='submitting',attempts=attempts+1",
                (proposal_id, wid, kind, parent_id, base_version, db.json_dumps(proposed),
                 summary, db.json_dumps(diff_configs(current, proposed)), db.json_dumps({"mode": "auto"}),
                 0, 0.0, rationale, db.json_dumps(evidence), sim_minute, attempt, dedupe))
        decision_id = self.ledger.record(
            decision_type="auto_apply_attempt",
            decision_status="decided",
            summary=f"Auto-applying {'/'.join(dims)} under workflow policy",
            decision_statement=(f"Policy for {wid} permits auto-apply for {dims}; largest allocation "
                                f"shift is {auto['shift']:.3f} within max_shift "
                                f"{auto['policy'].get('max_shift')}. The controller is applying "
                                f"version {base_version + 1} without a human approval, as permitted."),
            source=[source_ref("simulator_api", f"workflows/{wid}/config",
                               endpoint=f"/workflows/{wid}/config"),
                    source_ref("controller_evidence", f"proposal:{proposal_id}")],
            evidence={"dims": dims, "shift": auto["shift"], "base_version": base_version,
                      "policy": auto["policy"], "proposed_version": base_version + 1},
            workflow_id=wid, tenant_id=wf.get("tenant_id"), sim_minute=sim_minute,
            policy_reference="governance.auto_apply", selected_action="auto_apply",
            expected_impact={"estimate": None, "confidence": "estimate_only"},
            risk_assessment="Policy permits this class of change; monitoring continues after apply.",
            approval_status="not_required", execution_status="in_progress",
        )
        try:
            resp = self.client.auto_apply(wid, {
                "proposal_id": proposal_id, "base_version": base_version,
                "proposed_config": proposed, "rationale": rationale,
                "evidence": evidence,
            })
            version = int(resp.get("version") or base_version + 1)
            with db.transaction(self.conn):
                self.conn.execute("UPDATE proposals SET status='applied',applied_version=?,"
                                  "decided_sim_minute=? WHERE proposal_id=?",
                                  (version, sim_minute, proposal_id))
            self.sync_config(wid, sim_minute)
            self.snapshot(wid, base_version, current, dims, "auto-apply", sim_minute, decision_id)
            self.ledger.lifecycle(
                decision_id, stage="applied",
                summary=f"Auto-applied version {version}",
                statement=(f"The simulator accepted the auto-apply and recorded version {version}; "
                           f"changed dimensions: {dims}."),
                source=[source_ref("simulator_api", f"workflows/{wid}/config",
                                   endpoint=f"/workflows/{wid}/config"),
                        source_ref("controller_database", f"proposal:{proposal_id}")],
                evidence={"version": version, "dims": resp.get("changed_dims", dims),
                          "proposal_id": proposal_id},
                execution_status="applied", sim_minute=sim_minute, workflow_id=wid,
                tenant_id=wf.get("tenant_id"),
            )
            return {"ok": True, "proposal_id": proposal_id, "status": "applied",
                    "version": version, "decision_id": decision_id}
        except SimError as e:
            if e.status == 409 and e.code == "duplicate_proposal_id":
                hist = self.client.config_history(wid)
                if any(v.get("proposal_id") == proposal_id for v in hist):
                    with db.transaction(self.conn):
                        self.conn.execute("UPDATE proposals SET status='applied' WHERE proposal_id=?",
                                          (proposal_id,))
                    self.ledger.lifecycle(
                        decision_id, stage="applied_after_retry",
                        summary="Auto-apply was already applied before the retry",
                        statement=("The simulator rejected a duplicate proposal id, and config history "
                                   "shows this proposal already applied. No second change was made."),
                        source=[source_ref("simulator_api", f"workflows/{wid}/config/history",
                                           endpoint=f"/workflows/{wid}/config/history")],
                        evidence={"proposal_id": proposal_id}, execution_status="applied",
                        sim_minute=sim_minute, workflow_id=wid, tenant_id=wf.get("tenant_id"))
                    return {"ok": True, "proposal_id": proposal_id, "status": "applied",
                            "decision_id": decision_id}
            with db.transaction(self.conn):
                self.conn.execute("UPDATE proposals SET status='failed',last_error=? WHERE proposal_id=?",
                                  (str(e), proposal_id))
            self.ledger.lifecycle(
                decision_id, stage="not_applied",
                summary="Auto-apply was rejected by the simulator",
                statement=(f"The simulator rejected the auto-apply with {e.status} {e.code}: "
                           f"{e.detail}. No configuration was changed. The controller will not retry "
                           "the same request blindly."),
                source=[source_ref("simulator_api", f"workflows/{wid}/config",
                                   endpoint=f"/workflows/{wid}/config")],
                evidence={"error": {"status": e.status, "code": e.code, "detail": e.detail}},
                execution_status="not_applied", sim_minute=sim_minute, workflow_id=wid,
                tenant_id=wf.get("tenant_id"))
            return {"ok": False, "reason": e.code, "detail": e.detail, "decision_id": decision_id}

    def _request_approval(self, wid: str, wf: dict, current: dict, proposed: dict,
                          base_version: int, dims: list[str], summary: str, rationale: str,
                          evidence: dict, affected_leads: int, dry_run: dict, impact_score: float,
                          sim_minute: int, dedupe: str, kind: str, parent_id: str | None,
                          auto: dict, attempt: int = 1) -> dict:
        gov = wf.get("governance") or {}
        if not self._in_approver_hours(wf, sim_minute):
            if not self._recent_decision(wid, "proposal_deferred", sim_minute, 360):
                self.ledger.record(
                    decision_type="proposal_deferred",
                    decision_status="deferred",
                    summary="Approval request deferred to approver hours",
                    decision_statement=(f"The change is ready, but the workflow's approver works "
                                        f"{gov.get('approver_hours')} and the simulated clock is "
                                        f"{sim_minute % 1440 // 60:02d}:00. Sending now would sit in a "
                                        "queue and waste approver attention."),
                    source=[source_ref("simulator_api", "sim/state", endpoint="/sim/state"),
                            source_ref("controller_evidence", f"proposal:{dedupe}")],
                    evidence={"dims": dims, "approver_hours": gov.get("approver_hours"),
                              "reason": "outside_approver_hours", "base_version": base_version},
                    workflow_id=wid, tenant_id=wf.get("tenant_id"), sim_minute=sim_minute,
                    policy_reference="governance.approver_hours", selected_action="defer",
                    approval_status="not_submitted", execution_status="not_applied",
                    risk_assessment="Deferring delays the change but respects the approver contract.",
                )
            return {"ok": False, "reason": "outside_approver_hours"}
        if self._pending(wid) > 0:
            return {"ok": False, "reason": "proposal_already_pending"}
        if sim_minute < self._next_proposal_allowed(wid):
            return {"ok": False, "reason": "cooldown"}
        if self._approvals_last_24h(wid, sim_minute) + 1 > int(gov.get("max_requests_per_24h", 4)):
            if not self._recent_decision(wid, "proposal_deferred", sim_minute, 360):
                self.ledger.record(
                    decision_type="proposal_deferred",
                    decision_status="deferred",
                    summary="Approval request deferred by the 24-hour approver limit",
                    decision_statement=(f"Submitting would exceed max_requests_per_24h="
                                        f"{gov.get('max_requests_per_24h')} for {wid}. The controller "
                                        "batches and waits instead of flooding the approver."),
                    source=[source_ref("controller_database", f"approvals:{wid}"),
                            source_ref("simulator_api", f"workflows/{wid}",
                                       endpoint=f"/workflows/{wid}")],
                    evidence={"requests_last_24h": self._approvals_last_24h(wid, sim_minute),
                              "limit": gov.get("max_requests_per_24h")},
                    workflow_id=wid, tenant_id=wf.get("tenant_id"), sim_minute=sim_minute,
                    policy_reference="governance.max_requests_per_24h",
                    selected_action="defer", approval_status="not_submitted",
                    execution_status="not_applied",
                    risk_assessment="Exceeding the limit would be rejected and damage trust.",
                )
            return {"ok": False, "reason": "approver_fatigue_limit"}
        if self._approvals_today_global(sim_minute) + 1 > self.settings.max_approvals_per_day_global:
            return {"ok": False, "reason": "global_approval_budget"}
        if "prompt_version" in dims and not (evidence or {}).get("validation_run_id"):
            self.ledger.record(
                decision_type="prompt_change_rejected",
                decision_status="rejected",
                summary="Prompt change blocked: validation evidence is missing",
                decision_statement=("The proposed prompt change has no validation_run_id with a passing "
                                    "safety run, so it was not submitted."),
                source=[source_ref("controller_database", f"proposal:{dedupe}")],
                evidence={"dims": dims, "evidence": evidence},
                workflow_id=wid, tenant_id=wf.get("tenant_id"), sim_minute=sim_minute,
                policy_reference="governance.prompt_validation", selected_action="do_not_submit",
                execution_status="not_applied",
                risk_assessment="Shipping an unvalidated script risks hallucination harm.",
            )
            return {"ok": False, "reason": "missing_validation_evidence"}

        diff = diff_configs(current, proposed)
        if not diff:
            diff = {d: {"changed": True} for d in dims}
        dry_run = dict(dry_run or {})
        if not dry_run:
            dry_run = {"method": "no_historical_replay_available",
                       "note": "No dry-run evidence was available; the proposal is labelled as an estimate."}
        impact = max(0.0, min(1.0, float(impact_score)))
        body = {
            "proposal_id": f"ctl-apr-{wid}-{dedupe}-a{attempt}",
            "workflow_id": wid,
            "base_version": base_version,
            "proposed_config": proposed,
            "change_summary": summary,
            "diff": diff,
            "affected_leads": int(max(0, affected_leads)),
            "dry_run": dry_run,
            "impact_score": impact,
            "rationale": rationale,
            "evidence": evidence or {},
            "expires_in_hours": min(float(gov.get("proposal_ttl_hours", 24)), 24.0),
        }
        decision_id = self.ledger.record(
            decision_type="proposal_submission",
            decision_status="proposed",
            summary=f"Submitting approval request: {summary}",
            decision_statement=(f"Base version {base_version}; changed dimensions {dims}. "
                                f"{rationale}"),
            source=[source_ref("simulator_api", f"workflows/{wid}/config",
                               endpoint=f"/workflows/{wid}/config"),
                    source_ref("controller_evidence", f"proposal:{dedupe}")],
            evidence={"dims": dims, "base_version": base_version, "affected_leads": affected_leads,
                      "impact_score": impact, "dry_run": dry_run, "evidence": evidence},
            workflow_id=wid, tenant_id=wf.get("tenant_id"), sim_minute=sim_minute,
            policy_reference="governance.approval_path", selected_action="request_approval",
            expected_impact={"estimate": impact, "confidence": "controller_estimate"},
            risk_assessment="Approver may reject or let the request expire; no config changes until approved.",
            approval_status="requesting", execution_status="not_applied",
        )
        try:
            resp = self.client.request_approval(body)
        except SimError as e:
            if e.status == 409 and e.code == "duplicate_proposal_id":
                found = None
                try:
                    for ap in self.client.list_approvals(wid):
                        if ap.get("proposal_id") == body["proposal_id"]:
                            found = ap
                            break
                except SimError:
                    found = None
                if found:
                    aid = found["approval_id"]
                    with db.transaction(self.conn):
                        self.conn.execute(
                            "INSERT INTO proposals(proposal_id,workflow_id,kind,parent_id,base_version,"
                            "proposed_config_json,change_summary,diff_json,dry_run_json,affected_leads,"
                            "impact_score,rationale,evidence_json,mode,approval_id,status,"
                            "created_sim_minute,attempts,dedupe_key) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?, 'approval',?, 'approval_pending',?,?,?) "
                            "ON CONFLICT(proposal_id) DO UPDATE SET status='approval_pending',"
                            "approval_id=excluded.approval_id",
                            (body["proposal_id"], wid, kind, parent_id, base_version,
                             db.json_dumps(proposed), summary, db.json_dumps(diff),
                             db.json_dumps(dry_run), int(affected_leads), impact, rationale,
                             db.json_dumps(evidence or {}), aid, sim_minute, attempt, dedupe))
                        self.conn.execute(
                            "INSERT INTO approvals(approval_id,proposal_id,workflow_id,status,"
                            "base_version,created_sim_minute,expires_sim_minute,apply_by,raw_json) "
                            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(approval_id) DO UPDATE SET "
                            "status=excluded.status,raw_json=excluded.raw_json",
                            (aid, body["proposal_id"], wid, found.get("status", "pending"),
                             base_version, _minute_from_ts(found.get("created_at")),
                             _minute_from_ts(found.get("expires_at")), found.get("apply_by"),
                             db.json_dumps(found)))
                    self.ledger.lifecycle(
                        decision_id, stage="adopted_after_retry",
                        summary=f"Adopted existing approval {aid} after a duplicate-id reply",
                        statement=("The simulator rejected a duplicate proposal id, which means the "
                                   "request exists from before a crash. Reconciliation found it and "
                                   "linked it locally instead of creating a second request."),
                        source=[source_ref("simulator_api", f"approvals/{aid}",
                                           endpoint=f"/approvals/{aid}")],
                        evidence={"approval_id": aid, "proposal_id": body["proposal_id"]},
                        approval_status=found.get("status", "pending"),
                        execution_status="not_applied", result_reference=aid,
                        sim_minute=sim_minute, workflow_id=wid, tenant_id=wf.get("tenant_id"))
                    return {"ok": True, "proposal_id": body["proposal_id"], "approval_id": aid,
                            "status": found.get("status", "pending"), "decision_id": decision_id}
            self.ledger.lifecycle(
                decision_id, stage="submission_failed",
                summary=f"Approval request was not accepted ({e.code})",
                statement=(f"The simulator returned {e.status} {e.code}: {e.detail}. No approval "
                           "was created and no configuration changed."),
                source=[source_ref("simulator_api", "approvals", endpoint="/approvals")],
                evidence={"error": {"status": e.status, "code": e.code, "detail": e.detail},
                          "base_version": base_version},
                execution_status="not_applied", sim_minute=sim_minute, workflow_id=wid,
                tenant_id=wf.get("tenant_id"))
            if e.status == 409 and e.code == "stale_base_version":
                self._set_next_allowed(wid, sim_minute + 60)
            return {"ok": False, "reason": e.code, "detail": e.detail, "decision_id": decision_id}

        aid = resp.get("approval_id")
        with db.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO proposals(proposal_id,workflow_id,kind,parent_id,base_version,"
                "proposed_config_json,change_summary,diff_json,dry_run_json,affected_leads,impact_score,"
                "rationale,evidence_json,mode,approval_id,status,created_sim_minute,attempts,dedupe_key) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?, 'approval',?, 'approval_pending',?,?,?) "
                "ON CONFLICT(proposal_id) DO UPDATE SET status='approval_pending',"
                "approval_id=excluded.approval_id,attempts=attempts+1",
                (body["proposal_id"], wid, kind, parent_id, base_version,
                 db.json_dumps(proposed), summary, db.json_dumps(diff), db.json_dumps(dry_run),
                 int(affected_leads), impact, rationale, db.json_dumps(evidence or {}), aid,
                 sim_minute, attempt, dedupe))
            self.conn.execute(
                "INSERT INTO approvals(approval_id,proposal_id,workflow_id,status,base_version,"
                "created_sim_minute,apply_by,raw_json) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(approval_id) DO UPDATE SET status=excluded.status,"
                "raw_json=excluded.raw_json",
                (aid, body["proposal_id"], wid, resp.get("status", "pending"), base_version,
                 sim_minute, resp.get("apply_by"), db.json_dumps(resp)))
        self.ledger.lifecycle(
            decision_id, stage="approval_pending",
            summary=f"Approval {aid} is pending",
            statement=(f"The simulator accepted the request and assigned approval {aid}. "
                       "The controller will poll it and apply only if approved."),
            source=[source_ref("simulator_api", f"approvals/{aid}", endpoint=f"/approvals/{aid}")],
            evidence={"approval_id": aid, "status": resp.get("status"),
                      "expires_at": resp.get("expires_at"), "apply_by": resp.get("apply_by")},
            approval_status="pending", execution_status="not_applied",
            result_reference=aid, sim_minute=sim_minute, workflow_id=wid,
            tenant_id=wf.get("tenant_id"))
        return {"ok": True, "proposal_id": body["proposal_id"], "approval_id": aid,
                "status": resp.get("status"), "decision_id": decision_id}

    # -- approval lifecycle --------------------------------------------------
    def poll_and_apply(self, sim_minute: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT p.*, a.status AS approval_status FROM proposals p "
            "LEFT JOIN approvals a ON a.approval_id=p.approval_id "
            "WHERE p.status IN ('approval_pending','approved')").fetchall()
        out = []
        for p in rows:
            aid = p["approval_id"]
            if not aid:
                continue
            try:
                ap = self.client.get_approval(aid)
            except SimError as e:
                if e.status == 404:
                    self.ledger.record(
                        decision_type="approval_poll",
                        decision_status="error",
                        summary="Approval not found while reconciling",
                        decision_statement=f"GET /approvals/{aid} returned 404; the proposal remains recorded.",
                        source=[source_ref("simulator_api", f"approvals/{aid}",
                                           endpoint=f"/approvals/{aid}")],
                        evidence={"error": e.code}, workflow_id=p["workflow_id"],
                        sim_minute=sim_minute, selected_action="keep_pending",
                        approval_status="unknown", execution_status="not_applied")
                continue
            st = ap.get("status")
            with db.transaction(self.conn):
                self.conn.execute(
                    "UPDATE approvals SET status=?,created_sim_minute=COALESCE(created_sim_minute,?),"
                    "decided_sim_minute=?,expires_sim_minute=?,apply_by=?,reasons_json=?,"
                    "raw_json=?,last_polled_sim_minute=? WHERE approval_id=?",
                    (st, _minute_from_ts(ap.get("created_at")),
                     _minute_from_ts(ap.get("decided_at")),
                     _minute_from_ts(ap.get("expires_at")), ap.get("apply_by"),
                     db.json_dumps(ap.get("reasons") or []), db.json_dumps(ap), sim_minute, aid))
            if st == "applied":
                out.append(self._finish_already_applied(p, ap, sim_minute))
            elif st == "approved":
                out.append(self._apply_approved(p, ap, sim_minute))
            elif st in ("rejected", "expired", "lapsed"):
                out.append(self._close_rejected(p, ap, sim_minute))
        return out

    def _finish_already_applied(self, p, ap: dict, sim_minute: int) -> dict:
        """Close a proposal whose approval the simulator already applied.

        This is the crash case between the simulator's apply and the local
        commit: the change exists, but the controller must not apply it twice.
        """
        wid = p["workflow_id"]
        wf = self.workflows[wid]
        aid = p["approval_id"]
        if p["status"] == "applied":
            return {"ok": True, "suppressed": True, "reason": "already_applied"}
        try:
            self.sync_config(wid, sim_minute)
        except SimError:
            pass
        with db.transaction(self.conn):
            self.conn.execute("UPDATE proposals SET status='applied',applied_version=COALESCE("
                              "applied_version,?),decided_sim_minute=? WHERE proposal_id=?",
                              (ap.get("applied_version"), sim_minute, p["proposal_id"]))
            self.conn.execute("UPDATE approvals SET status='applied',applied_version=? "
                              "WHERE approval_id=?", (ap.get("applied_version"), aid))
        decision_id = self.ledger.record(
            decision_type="approval_apply",
            decision_status="applied",
            summary=f"Reconciled an already-applied approval {aid}",
            decision_statement=(f"Approval {aid} is marked applied by the simulator but the local "
                                "proposal was still open. The controller syncs config history and "
                                "closes the record without applying again."),
            source=[source_ref("simulator_api", f"approvals/{aid}", endpoint=f"/approvals/{aid}"),
                    source_ref("simulator_api", f"workflows/{wid}/config/history",
                               endpoint=f"/workflows/{wid}/config/history")],
            evidence={"approval_id": aid, "applied_version": ap.get("applied_version"),
                      "proposal_id": p["proposal_id"]},
            workflow_id=wid, tenant_id=wf.get("tenant_id"), sim_minute=sim_minute,
            policy_reference="governance.reconciliation", selected_action="reconcile",
            approval_status="applied", execution_status="applied", result_reference=aid,
            risk_assessment="A blind retry would have asked the approver twice.",
        )
        return {"ok": True, "version": ap.get("applied_version"), "decision_id": decision_id,
                "approval_id": aid}

    def _apply_approved(self, p, ap: dict, sim_minute: int) -> dict:
        wid = p["workflow_id"]
        wf = self.workflows[wid]
        aid = p["approval_id"]
        try:
            self.sync_config(wid, sim_minute)
        except SimError:
            pass
        base_version, current = self.current_config(wid)
        if base_version != int(p["base_version"]):
            with db.transaction(self.conn):
                self.conn.execute("UPDATE proposals SET status='stale' WHERE proposal_id=?", (p["proposal_id"],))
            decision_id = self.ledger.record(
                decision_type="approval_apply",
                decision_status="rejected",
                summary="Approved change not applied: base version is stale",
                decision_statement=(f"Approval {aid} was approved against version {p['base_version']}, "
                                    f"but the live version is {base_version} (likely a supervisor edit). "
                                    "The controller will not overwrite the newer config."),
                source=[source_ref("simulator_api", f"workflows/{wid}/config",
                                   endpoint=f"/workflows/{wid}/config"),
                        source_ref("simulator_api", f"approvals/{aid}", endpoint=f"/approvals/{aid}")],
                evidence={"approval_id": aid, "proposed_base_version": p["base_version"],
                          "live_version": base_version},
                workflow_id=wid, tenant_id=wf.get("tenant_id"), sim_minute=sim_minute,
                policy_reference="governance.stale_base_version", selected_action="do_not_apply",
                approval_status="approved_but_stale", execution_status="not_applied",
                risk_assessment="Applying would erase a supervisor's newer change.",
            )
            self._set_next_allowed(wid, sim_minute + 360)
            return {"ok": False, "reason": "stale", "decision_id": decision_id}
        try:
            resp = self.client.apply(wid, aid)
        except SimError as e:
            if e.status == 409 and e.code == "stale_base_version":
                with db.transaction(self.conn):
                    self.conn.execute("UPDATE proposals SET status='stale' WHERE proposal_id=?",
                                      (p["proposal_id"],))
                decision_id = self.ledger.record(
                    decision_type="approval_apply",
                    decision_status="rejected",
                    summary="Apply rejected as stale by the simulator",
                    decision_statement=(f"POST apply for {aid} returned 409 stale_base_version. "
                                        "No change was applied; the controller will not overwrite it."),
                    source=[source_ref("simulator_api", f"approvals/{aid}",
                                       endpoint=f"/approvals/{aid}")],
                    evidence={"error": e.detail, "approval_id": aid},
                    workflow_id=wid, tenant_id=wf.get("tenant_id"), sim_minute=sim_minute,
                    policy_reference="governance.stale_base_version", selected_action="do_not_apply",
                    approval_status="approved_but_stale", execution_status="not_applied")
                return {"ok": False, "reason": "stale", "decision_id": decision_id}
            if e.status == 410 and e.code == "approval_lapsed":
                with db.transaction(self.conn):
                    self.conn.execute("UPDATE proposals SET status='lapsed' WHERE proposal_id=?",
                                      (p["proposal_id"],))
                decision_id = self.ledger.record(
                    decision_type="approval_apply",
                    decision_status="lapsed",
                    summary="Approved change lapsed before it could be applied",
                    decision_statement=(f"Approval {aid} passed its apply window and lapsed. "
                                        "The controller records the loss and will re-propose with fresh "
                                        "evidence only if still justified."),
                    source=[source_ref("simulator_api", f"approvals/{aid}",
                                       endpoint=f"/approvals/{aid}")],
                    evidence={"approval_id": aid, "error": e.detail},
                    workflow_id=wid, tenant_id=wf.get("tenant_id"), sim_minute=sim_minute,
                    policy_reference="governance.apply_window", selected_action="do_nothing",
                    approval_status="lapsed", execution_status="not_applied")
                self._set_next_allowed(wid, sim_minute + 360)
                return {"ok": False, "reason": "lapsed", "decision_id": decision_id}
            raise
        version = int(resp.get("version") or p["base_version"] + 1)
        with db.transaction(self.conn):
            self.conn.execute("UPDATE proposals SET status='applied',applied_version=?,"
                              "decided_sim_minute=? WHERE proposal_id=?",
                              (version, sim_minute, p["proposal_id"]))
            self.conn.execute("UPDATE approvals SET status='applied',applied_version=? WHERE approval_id=?",
                              (version, aid))
        decision_id = self.ledger.record(
            decision_type="approval_apply",
            decision_status="applied",
            summary=f"Applied approved change as version {version}",
            decision_statement=(f"Approval {aid} was approved and applied. The simulator recorded "
                                f"version {version} with dimensions {resp.get('changed_dims')}."),
            source=[source_ref("simulator_api", f"workflows/{wid}/apply",
                               endpoint=f"/workflows/{wid}/apply"),
                    source_ref("simulator_api", f"approvals/{aid}", endpoint=f"/approvals/{aid}")],
            evidence={"approval_id": aid, "version": version,
                      "dims": resp.get("changed_dims", []), "base_version": p["base_version"]},
            workflow_id=wid, tenant_id=wf.get("tenant_id"), sim_minute=sim_minute,
            policy_reference="governance.approval_path", selected_action="apply",
            approval_status="approved", execution_status="applied", result_reference=aid,
        )
        self.sync_config(wid, sim_minute)
        cfg = json.loads(p["proposed_config_json"])
        self.snapshot(wid, int(p["base_version"]), current, resp.get("changed_dims") or [],
                      "approval-apply", sim_minute, decision_id)
        return {"ok": True, "version": version, "decision_id": decision_id, "approval_id": aid,
                "config": cfg}

    def _close_rejected(self, p, ap: dict, sim_minute: int) -> dict:
        wid = p["workflow_id"]
        wf = self.workflows[wid]
        st = ap.get("status")
        with db.transaction(self.conn):
            self.conn.execute("UPDATE proposals SET status=?,decided_sim_minute=? WHERE proposal_id=?",
                              (st, sim_minute, p["proposal_id"]))
        reasons = ap.get("reasons") or []
        decision_id = self.ledger.record(
            decision_type="approval_outcome",
            decision_status=st,
            summary=f"Approval {p['approval_id']} ended {st}",
            decision_statement=(f"Approval {p['approval_id']} for {wid} ended {st}. "
                                f"Reasons: {reasons or ['none supplied']}. No configuration was applied."),
            source=[source_ref("simulator_api", f"approvals/{p['approval_id']}",
                               endpoint=f"/approvals/{p['approval_id']}"),
                    source_ref("controller_database", p["proposal_id"])],
            evidence={"approval_id": p["approval_id"], "status": st, "reasons": reasons,
                      "proposed_base_version": p["base_version"]},
            workflow_id=wid, tenant_id=wf.get("tenant_id"), sim_minute=sim_minute,
            policy_reference="governance.approval_path", selected_action="do_nothing",
            approval_status=st, execution_status="not_applied",
            risk_assessment=("An expired or rejected request is not evidence of failure; the controller "
                             "will revisit only with fresh data."),
            counterfactual=(
                {"observed": False, "type": "governance_counterfactual",
                 "statement": (f"If approval {p['approval_id']} had been approved before expiry and the "
                               f"base version had still been {p['base_version']}, the controller would "
                               f"have applied the recorded proposed config. It did not, because the "
                               f"approver status is {st}."),
                 "conditions": {"status": "approved", "base_version": p["base_version"]}}
                if st in ("rejected", "expired", "lapsed") else None),
        )
        cooldown = 720 if st in ("rejected", "expired") else 360
        if any("approver fatigue" in str(r) or "max_requests" in str(r) for r in reasons):
            cooldown = 1440
        self._set_next_allowed(wid, sim_minute + cooldown)
        return {"ok": False, "reason": st, "decision_id": decision_id}
