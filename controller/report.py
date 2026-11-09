"""Supervisor-facing report built only from the controller's own store.

One page, not fifty alerts: current KPI state, what is being learned, what is
waiting on a human, and the most recent decisions with their evidence links.
The simulator is not queried here; a report can be produced offline.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from . import analytics, db


def _pct(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.1%}"


def _num(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def _workflow_report(conn, wf: dict, sim_minute: int, day: int) -> dict:
    wid = wf["workflow_id"]
    total = analytics.workflow_metrics(conn, wid)
    today = analytics.workflow_metrics(conn, wid, day, day)
    history = analytics.workflow_metrics(conn, wid, None, -1) if day > 0 else {}
    notes = db.load_notes(conn, wid)
    studies = notes.get("studies") or {}
    promotions = notes.get("promotions") or {}
    open_incidents = [
        dict(r) for r in conn.execute(
            "SELECT incident_id,kind,status,detected_sim_minute,last_seen_sim_minute "
            "FROM incidents WHERE workflow_id=? AND status IN ('open','fix_ready') "
            "ORDER BY detected_sim_minute", (wid,)).fetchall()
    ]
    pending = [
        dict(r) for r in conn.execute(
            "SELECT proposal_id,kind,status,created_sim_minute,change_summary "
            "FROM proposals WHERE workflow_id=? AND status IN "
            "('submitting','approval_pending','approved') ORDER BY created_sim_minute", (wid,)).fetchall()
    ]
    row = conn.execute(
        "SELECT version,actor FROM config_versions WHERE workflow_id=? "
        "ORDER BY version DESC LIMIT 1", (wid,)).fetchone()
    return {
        "workflow_id": wid,
        "tenant_id": wf.get("tenant_id"),
        "objective": wf.get("objective"),
        "dials_per_hour": wf.get("dials_per_hour"),
        "config_version": dict(row) if row else None,
        "total": total,
        "today": today,
        "history": history,
        "studies": {d: {"status": s.get("status"), "baseline": s.get("baseline"),
                        "candidates": s.get("candidates"),
                        "allocation": s.get("allocation"),
                        "started_day": s.get("started_day")}
                    for d, s in studies.items()},
        "promotions": {d: {"value": p.get("value"), "control": p.get("control"),
                           "at_minute": p.get("at_minute")}
                       for d, p in promotions.items()},
        "cohort_wins": notes.get("cohort_wins") or {},
        "open_incidents": open_incidents,
        "pending_proposals": pending,
    }


def collect(conn, workflows: dict[str, dict], sim_minute: int, *, run_id: str,
            scenario: str, final: bool, state: dict | None = None,
            settings=None) -> dict:
    day = sim_minute // 1440
    wf_rows = [_workflow_report(conn, wf, sim_minute, day)
               for _, wf in sorted(workflows.items())]
    counts = {
        r["decision_type"]: int(r["n"]) for r in conn.execute(
            "SELECT decision_type,COUNT(*) n FROM decisions GROUP BY decision_type "
            "ORDER BY n DESC").fetchall()
    }
    recent = [dict(r) for r in conn.execute(
        "SELECT decision_id,ts,sim_minute,workflow_id,decision_type,decision_status,summary,"
        "policy_reference,approval_status,execution_status,result_reference "
        "FROM decisions ORDER BY seq DESC LIMIT 25").fetchall()]
    ing = conn.execute(
        "SELECT COALESCE(SUM(events),0) events,COALESCE(SUM(calls),0) calls,"
        "COALESCE(SUM(leads),0) leads,COALESCE(SUM(duplicate_events),0) duplicates,"
        "COALESCE(SUM(malformed),0) malformed,COUNT(*) batches FROM ingestion_metrics").fetchone()
    approvals = {
        r["status"]: int(r["n"]) for r in conn.execute(
            "SELECT status,COUNT(*) n FROM approvals GROUP BY status ORDER BY n DESC").fetchall()
    }
    proposals = {
        r["status"]: int(r["n"]) for r in conn.execute(
            "SELECT status,COUNT(*) n FROM proposals GROUP BY status ORDER BY n DESC").fetchall()
    }
    world = db.get_meta(conn, "world") or {}
    credits = None
    if state:
        credits = state.get("credits_remaining")
    if credits is None:
        credits = db.get_meta(conn, "credits")
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "scenario": scenario,
        "sim_minute": sim_minute,
        "sim_day": day,
        "sim_ts": (db.get_meta(conn, "world") or {}).get("scenario"),
        "finished": final,
        "credits_remaining": credits,
        "ingestion": dict(ing) if ing else {},
        "approvals": approvals,
        "proposals": proposals,
        "decision_counts": counts,
        "workflows": wf_rows,
        "recent_decisions": recent,
        "world": world,
    }


def render_markdown(report: dict) -> str:
    lines = [
        f"# Controller report — {report.get('scenario')}",
        "",
        f"- run: `{report.get('run_id')}`",
        f"- simulated day: {report.get('sim_day')} (minute {report.get('sim_minute')})",
        f"- finished: {report.get('finished')}",
        f"- credits remaining: {report.get('credits_remaining')}",
        f"- generated: {report.get('generated_at')}",
        "",
        "## Workflows",
        "",
        "| workflow | primary | pickup | engagement | conclusion | dials today | config | "
        "learning | open incidents | pending approvals |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for wf in report.get("workflows") or []:
        total = wf.get("total") or {}
        today = wf.get("today") or {}
        obj = wf.get("objective") or {}
        cfg = wf.get("config_version") or {}
        learning = []
        for dim, st in (wf.get("studies") or {}).items():
            learning.append(f"{dim}:{st.get('status')}")
        for dim, p in (wf.get("promotions") or {}).items():
            learning.append(f"{dim}->{p.get('value')}")
        lines.append(
            "| {wf} | {primary} | {pickup} | {eng} | {conc} | {dials} | v{ver} ({actor}) | "
            "{learn} | {inc} | {pend} |".format(
                wf=wf.get("workflow_id"),
                primary=obj.get("primary", "n/a"),
                pickup=_pct(total.get("pickup_rate")),
                eng=_pct(total.get("engagement_rate")),
                conc=_pct(total.get("conclusion_rate")),
                dials=_num(today.get("dials")),
                ver=cfg.get("version", "?"), actor=cfg.get("actor") or "?",
                learn=", ".join(learning) or "—",
                inc=len(wf.get("open_incidents") or []),
                pend=len(wf.get("pending_proposals") or []),
            ))
    lines += ["", "## Governance", "",
              f"- approvals by status: `{report.get('approvals')}`",
              f"- proposals by status: `{report.get('proposals')}`",
              ""]
    lines += ["## Ingestion", "",
              f"- events: {_num((report.get('ingestion') or {}).get('events'))}",
              f"- calls: {_num((report.get('ingestion') or {}).get('calls'))}",
              f"- leads: {_num((report.get('ingestion') or {}).get('leads'))}",
              f"- duplicate events skipped: {_num((report.get('ingestion') or {}).get('duplicates'))}",
              f"- malformed: {_num((report.get('ingestion') or {}).get('malformed'))}",
              ""]
    lines += ["## Decision counts", ""]
    for k, v in (report.get("decision_counts") or {}).items():
        lines.append(f"- `{k}`: {v}")
    lines += ["", "## Recent decisions", "",
              "| id | sim minute | workflow | type | status | execution | summary |",
              "|---|---|---|---|---|---|---|"]
    for d in report.get("recent_decisions") or []:
        lines.append("| {id} | {m} | {wf} | {t} | {s} | {e} | {sum} |".format(
            id=d.get("decision_id"), m=d.get("sim_minute"), wf=d.get("workflow_id") or "—",
            t=d.get("decision_type"), s=d.get("decision_status"),
            e=d.get("execution_status"), sum=(d.get("summary") or "").replace("|", "\\|")))
    lines += ["", "Full structured evidence and counterfactuals are in `report.json` and the "
                  "append-only decision store (`controller.db`).", ""]
    return "\n".join(lines)


def write_report(conn, workflows: dict[str, dict], sim_minute: int, out_dir: str, *,
                 run_id: str, scenario: str, final: bool,
                 state: dict | None = None, settings=None) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    report = collect(conn, workflows, sim_minute, run_id=run_id, scenario=scenario,
                     final=final, state=state, settings=settings)
    json_path = os.path.join(out_dir, "report.json")
    md_path = os.path.join(out_dir, "report.md")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, sort_keys=True, default=str)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_markdown(report))
    return {"path": md_path, "json": json_path, "report": report}
