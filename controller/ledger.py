"""Append-only decision ledger and provenance helpers.

Every meaningful controller decision is written here with a concrete source
list and structured evidence.  Counterfactuals live in their own append-only
table and are always labelled as hypothetical, never mixed into observations.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from . import db
from .config import SIM_START


class LedgerError(ValueError):
    pass


def _ts(sim_minute: int | None) -> str:
    if sim_minute is None:
        return datetime.now(timezone.utc).isoformat()
    return (SIM_START + timedelta(minutes=int(sim_minute))).isoformat()


def source_ref(kind: str, reference: str, *, endpoint: str | None = None,
               detail: str | None = None) -> dict:
    out = {"type": kind, "reference": str(reference)}
    if endpoint:
        out["endpoint"] = endpoint
    if detail:
        out["detail"] = detail
    return out


def validate_decision(d: dict) -> None:
    required = ["decision_type", "decision_status", "summary", "decision_statement"]
    missing = [k for k in required if not str(d.get(k) or "").strip()]
    if missing:
        raise LedgerError(f"decision missing required fields: {missing}")
    sources = d.get("source")
    if not isinstance(sources, list) or not sources:
        raise LedgerError("decision requires at least one concrete source")
    for s in sources:
        if not isinstance(s, dict) or not s.get("type") or not s.get("reference"):
            raise LedgerError("each source needs type and reference")
    if not isinstance(d.get("evidence"), dict):
        raise LedgerError("decision evidence must be a structured object")
    cf = d.get("counterfactual")
    if cf is not None:
        if not isinstance(cf, dict):
            raise LedgerError("counterfactual must be an object")
        if cf.get("observed") is not False:
            raise LedgerError("counterfactuals must be explicitly observed=false")
        if not str(cf.get("statement") or "").strip():
            raise LedgerError("counterfactual needs a statement")


class DecisionLedger:
    def __init__(self, conn, run_id: str, scenario: str):
        self.conn = conn
        self.run_id = run_id
        self.scenario = scenario

    def record(self, *, decision_type: str, decision_status: str, summary: str,
               decision_statement: str, source: list[dict], evidence: dict,
               workflow_id: str | None = None, tenant_id: str | None = None,
               sim_minute: int | None = None, policy_reference: str | None = None,
               alternatives: list[str] | None = None, selected_action: str | None = None,
               expected_impact: dict | None = None, risk_assessment: str | None = None,
               approval_status: str = "not_required", execution_status: str = "not_applied",
               result_reference: str | None = None, correlation_id: str | None = None,
               supersedes: str | None = None,
               counterfactual: dict | None = None) -> str:
        d = {
            "decision_type": decision_type,
            "decision_status": decision_status,
            "summary": summary,
            "decision_statement": decision_statement,
            "source": source,
            "evidence": evidence,
            "counterfactual": counterfactual,
        }
        validate_decision(d)
        nxt = self.conn.execute("SELECT COALESCE(MAX(seq),0)+1 AS s FROM decisions").fetchone()["s"]
        decision_id = f"dec_{int(nxt):07d}"
        now = datetime.now(timezone.utc).isoformat()
        row = (decision_id, int(nxt), _ts(sim_minute), sim_minute, self.run_id, self.scenario,
               workflow_id, tenant_id, decision_type, decision_status, summary,
               decision_statement, db.json_dumps(source), db.json_dumps(evidence),
               policy_reference, db.json_dumps(alternatives or []), selected_action,
               db.json_dumps(expected_impact or {}), risk_assessment, approval_status,
               execution_status, result_reference, correlation_id, supersedes, now)
        insert = ("INSERT INTO decisions(decision_id,seq,ts,sim_minute,run_id,scenario,workflow_id,"
                  "tenant_id,decision_type,decision_status,summary,decision_statement,source_json,"
                  "evidence_json,policy_reference,alternatives_json,selected_action,"
                  "expected_impact_json,risk_assessment,approval_status,execution_status,"
                  "result_reference,correlation_id,supersedes,created_at) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
        if self.conn.in_transaction:
            self.conn.execute(insert, row)
            if counterfactual is not None:
                self.conn.execute(
                    "INSERT INTO counterfactuals(decision_id,counterfactual_json,created_at) VALUES(?,?,?)",
                    (decision_id, db.json_dumps(counterfactual), now))
        else:
            with db.transaction(self.conn):
                self.conn.execute(insert, row)
                if counterfactual is not None:
                    self.conn.execute(
                        "INSERT INTO counterfactuals(decision_id,counterfactual_json,created_at) VALUES(?,?,?)",
                        (decision_id, db.json_dumps(counterfactual), now))
        return decision_id

    def lifecycle(self, original_id: str, *, stage: str, summary: str, statement: str,
                  source: list[dict], evidence: dict, execution_status: str,
                  approval_status: str | None = None, result_reference: str | None = None,
                  sim_minute: int | None = None, workflow_id: str | None = None,
                  tenant_id: str | None = None, counterfactual: dict | None = None) -> str:
        orig = self.get(original_id)
        return self.record(
            decision_type=f"lifecycle:{orig['decision_type']}",
            decision_status=stage,
            summary=summary,
            decision_statement=statement,
            source=source,
            evidence=evidence,
            workflow_id=workflow_id or orig.get("workflow_id"),
            tenant_id=tenant_id or orig.get("tenant_id"),
            sim_minute=sim_minute,
            policy_reference=orig.get("policy_reference"),
            alternatives=orig.get("alternatives", []),
            selected_action=orig.get("selected_action"),
            expected_impact=orig.get("expected_impact", {}),
            risk_assessment=orig.get("risk_assessment"),
            approval_status=approval_status or orig.get("approval_status", "not_required"),
            execution_status=execution_status,
            result_reference=result_reference,
            correlation_id=original_id,
            supersedes=original_id,
            counterfactual=counterfactual,
        )

    def get(self, decision_id: str) -> dict:
        row = self.conn.execute("SELECT * FROM decisions WHERE decision_id=?", (decision_id,)).fetchone()
        if row is None:
            raise KeyError(decision_id)
        return self._decode(row)

    def _decode(self, row) -> dict:
        out = dict(row)
        for k in ("source_json", "evidence_json", "alternatives_json", "expected_impact_json"):
            key = k[:-5]
            try:
                out[key] = json.loads(out.pop(k) or "null")
            except (TypeError, ValueError):
                out[key] = None
        cf = self.conn.execute(
            "SELECT counterfactual_json FROM counterfactuals WHERE decision_id=?",
            (out["decision_id"],)).fetchone()
        out["counterfactual"] = json.loads(cf["counterfactual_json"]) if cf else None
        return out

    def query(self, *, workflow_id: str | None = None, decision_type: str | None = None,
              decision_status: str | None = None, limit: int = 200,
              since_sim_minute: int | None = None, correlation_id: str | None = None) -> list[dict]:
        where = []
        args: list[Any] = []
        if workflow_id:
            where.append("workflow_id=?")
            args.append(workflow_id)
        if decision_type:
            where.append("decision_type=?")
            args.append(decision_type)
        if decision_status:
            where.append("decision_status=?")
            args.append(decision_status)
        if since_sim_minute is not None:
            where.append("sim_minute>=?")
            args.append(since_sim_minute)
        if correlation_id:
            where.append("correlation_id=?")
            args.append(correlation_id)
        sql = "SELECT * FROM decisions"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY seq ASC LIMIT ?"
        args.append(int(limit))
        return [self._decode(r) for r in self.conn.execute(sql, args).fetchall()]

    def lifecycle_rows(self, decision_id: str) -> list[dict]:
        return [self._decode(r) for r in self.conn.execute(
            "SELECT * FROM decisions WHERE correlation_id=? ORDER BY seq ASC",
            (decision_id,)).fetchall()]

    def explain(self, decision_id: str) -> str:
        d = self.get(decision_id)
        lines = [
            f"DECISION {d['decision_id']}  ({d['ts']})",
            f"scenario={d.get('scenario')} workflow={d.get('workflow_id')} tenant={d.get('tenant_id')}",
            f"type={d['decision_type']} status={d['decision_status']}",
            "",
            f"SUMMARY: {d['summary']}",
            f"WHY: {d['decision_statement']}",
            "",
            "EVIDENCE:",
            json.dumps(d.get("evidence"), indent=2, sort_keys=True),
            "",
            "SOURCES:",
        ]
        for s in d.get("source") or []:
            lines.append(f"  - {s.get('type')} {s.get('reference')}"
                         + (f" ({s.get('endpoint')})" if s.get("endpoint") else ""))
        lines += [
            "",
            f"POLICY: {d.get('policy_reference')}",
            f"ALTERNATIVES: {d.get('alternatives')}",
            f"SELECTED: {d.get('selected_action')}",
            f"EXPECTED IMPACT: {json.dumps(d.get('expected_impact'), sort_keys=True)}",
            f"RISK: {d.get('risk_assessment')}",
            f"APPROVAL: {d.get('approval_status')}  EXECUTION: {d.get('execution_status')}",
            f"RESULT: {d.get('result_reference')}",
        ]
        life = self.lifecycle_rows(decision_id)
        if life:
            lines.append("")
            lines.append("LIFECYCLE (appended, never rewritten):")
            for r in life:
                lines.append(f"  - {r['ts']} {r['decision_status']}: {r['summary']}")
        cf = d.get("counterfactual")
        if cf:
            lines += ["", "COUNTERFACTUAL (hypothetical, NOT observed):",
                      f"  {cf.get('statement')}",
                      f"  observed={cf.get('observed')} conditions={json.dumps(cf.get('conditions'), sort_keys=True)}"]
        return "\n".join(lines)
