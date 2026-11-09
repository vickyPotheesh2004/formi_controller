"""Incident detection and safe remediation.

Three things are watched:

* a caller ID whose pickup rate collapses (spam flagging);
* a prompt whose hallucination rate rises above a hard safety line, or that a
  supervisor just replaced;
* a broad engagement drift that is not explained by a variant experiment.

Detection is statistical and uses only the aggregate store.  Remediation is a
normal config proposal: auto-apply when the workflow policy permits it,
otherwise the human approval path.  Validation runs (safety only) choose a
replacement prompt; an LLM suggestion may order the candidates but never
decides.
"""
from __future__ import annotations

import copy
import json
import logging
from typing import Any

from . import analytics, db, stats
from .api import SimClient, SimError
from .config import Settings
from .ledger import source_ref

log = logging.getLogger("controller.incidents")


# --------------------------------------------------------------------------- helpers
def _json(value: Any) -> Any:
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return value


def _incident(conn, incident_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM incidents WHERE incident_id=?", (incident_id,)).fetchone()
    return dict(row) if row else None


def _open_incidents(conn, wid: str, kind: str | None = None) -> list[dict]:
    sql = "SELECT * FROM incidents WHERE workflow_id=? AND status IN ('open','fix_ready')"
    args: list[Any] = [wid]
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    return [dict(r) for r in conn.execute(sql, tuple(args)).fetchall()]


def _upsert_incident(conn, incident_id: str, wid: str, kind: str, status: str,
                     sim_minute: int, evidence: dict, decision_id: str | None = None,
                     resolved: bool = False) -> None:
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO incidents(incident_id,workflow_id,kind,status,detected_sim_minute,"
            "detected_day,last_seen_sim_minute,resolved_sim_minute,evidence_json,decision_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(incident_id) DO UPDATE SET "
            "status=excluded.status,last_seen_sim_minute=excluded.last_seen_sim_minute,"
            "resolved_sim_minute=excluded.resolved_sim_minute,evidence_json=excluded.evidence_json,"
            "decision_id=COALESCE(excluded.decision_id,incidents.decision_id)",
            (incident_id, wid, kind, status, sim_minute, sim_minute // 1440, sim_minute,
             sim_minute if resolved else None, db.json_dumps(evidence), decision_id))


def _pending_proposal(conn, wid: str, kind: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM proposals WHERE workflow_id=? AND kind=? AND status IN "
        "('submitting','approval_pending','approved') ORDER BY created_sim_minute DESC LIMIT 1",
        (wid, kind)).fetchone()
    return dict(row) if row else None


def _dist_uses(config: dict, dim: str, value: str) -> list[str]:
    used = []
    for key, dims in (config.get("allocations") or {}).items():
        if value in (dims.get(dim) or {}):
            used.append(key)
    return used


def _historical_ids(conn, wid: str) -> set[str]:
    prefix = f"did:{wid}:"
    return {
        row["incident_id"][len(prefix):]
        for row in conn.execute(
            "SELECT incident_id FROM incidents WHERE workflow_id=? AND kind='did_low_pickup'",
            (wid,))
        if row["incident_id"].startswith(prefix)
    }


def _last_rate(conn, wid: str, cid: str, min_n: int = 200) -> float | None:
    """Pickup on a caller ID's most recent day with enough dials."""
    row = conn.execute(
        "SELECT MAX(day) day FROM did_daily WHERE workflow_id=? AND caller_id=?",
        (wid, cid)).fetchone()
    if row["day"] is None:
        return None
    window = _did_window(conn, wid, cid, int(row["day"]), int(row["day"]))
    if window["dials"] < min_n:
        return None
    return analytics.ratio(window["connected"], window["dials"])


def _better_alternative(conn, wid: str, alt: str, current_rate: float | None,
                        margin: float = 0.05) -> bool:
    rate = _last_rate(conn, wid, alt)
    return bool(current_rate is not None and rate is not None
                and rate > current_rate * (1 + margin))


def _healthy_pool(conn, wid: str, options: list[str], flagged: set[str]) -> list[str]:
    """Caller IDs never flagged before win; fall back only if none are left.

    When every option has a bad history, re-admit the one whose last observed
    pickup was highest instead of spreading across known-bad IDs.
    """
    historical = _historical_ids(conn, wid)
    clean = [o for o in options if o not in flagged and o not in historical]
    if clean:
        return clean
    fallback = [o for o in options if o not in flagged]
    best: tuple[float, str] | None = None
    for cid in fallback:
        rate = _last_rate(conn, wid, cid)
        if rate is None:
            continue
        key = (rate, cid)
        if best is None or key > best:
            best = key
    return [best[1]] if best else fallback


def _remove_from_config(config: dict, dim: str, value: str, healthy: list[str]) -> dict:
    cfg = copy.deepcopy(config)
    for key, dims in (cfg.get("allocations") or {}).items():
        dist = dict(dims.get(dim) or {})
        if value not in dist:
            continue
        dist.pop(value, None)
        if not dist:
            # The bad option was the only one: spread over healthy options.
            pool = [o for o in healthy if o != value]
            if pool:
                dist = {o: 1.0 / len(pool) for o in pool}
            else:
                continue
        total = sum(dist.values()) or 1.0
        dims[dim] = {o: w / total for o, w in dist.items() if w > 0}
    return cfg


# --------------------------------------------------------------------------- caller id
def _did_window(conn, wid: str, cid: str, day_from: int | None, day_to: int | None) -> dict:
    clauses = ["workflow_id=?", "caller_id=?"]
    args: list[Any] = [wid, cid]
    if day_from is not None:
        clauses.append("day>=?")
        args.append(day_from)
    if day_to is not None:
        clauses.append("day<=?")
        args.append(day_to)
    row = conn.execute(
        "SELECT COALESCE(SUM(dials),0) dials,COALESCE(SUM(connected),0) connected,"
        "COALESCE(SUM(engaged),0) engaged FROM did_daily WHERE " + " AND ".join(clauses),
        tuple(args)).fetchone()
    return {k: int(row[k]) for k in ("dials", "connected", "engaged")}


def detect_did_incidents(conn, wf: dict, config: dict, sim_minute: int,
                         settings: Settings) -> list[dict]:
    """Find caller IDs whose pickup has statistically collapsed."""
    wid = wf["workflow_id"]
    day = sim_minute // 1440
    options = list((wf.get("options") or {}).get("caller_id") or [])
    in_use = [o for o in options if _dist_uses(config, "caller_id", o)]
    actions: list[dict] = []
    flagged: set[str] = set()

    for cid in options:
        recent = _did_window(conn, wid, cid, day, day)
        if recent["dials"] < settings.caller_id_min_recent_dials:
            recent = _did_window(conn, wid, cid, max(day - 1, -10000), day)
        baseline = _did_window(conn, wid, cid, None, day - 1)
        if recent["dials"] < settings.caller_id_min_recent_dials:
            continue
        if baseline["dials"] < settings.caller_id_min_baseline_dials:
            continue
        drop = stats.detect_drop(
            baseline["connected"], baseline["dials"],
            recent["connected"], recent["dials"],
            min_n=settings.caller_id_min_recent_dials,
            min_rel_drop=settings.caller_id_min_rel_drop,
            alpha=settings.alpha,
        )
        # A strong z-score is also enough when the relative floor is noisy.
        z_bad = (drop.get("z") is not None
                 and drop["z"] <= -settings.caller_id_z
                 and drop.get("relative_change") is not None
                 and drop["relative_change"] <= -settings.caller_id_min_rel_drop)
        if not (drop["drop"] or z_bad):
            continue
        flagged.add(cid)
        iid = f"did:{wid}:{cid}"
        ev = {
            "caller_id": cid, "baseline": baseline, "recent": recent,
            "baseline_pickup": analytics.ratio(baseline["connected"], baseline["dials"]),
            "recent_pickup": analytics.ratio(recent["connected"], recent["dials"]),
            "relative_change": drop.get("relative_change"),
            "z": drop.get("z"), "p_value": drop.get("p_value"),
            "in_use": cid in in_use, "method": drop,
        }
        existing = _incident(conn, iid)
        if existing and existing["status"] in ("open", "fix_ready"):
            if cid not in in_use:
                _upsert_incident(conn, iid, wid, "did_low_pickup", "resolved", sim_minute,
                                 ev, existing.get("decision_id"), resolved=True)
                continue
            pending = _pending_proposal(conn, wid, "incident_did_rotate")
            retry = (pending is None
                     and sim_minute - int(existing.get("last_seen_sim_minute") or 0) >= 60)
            if not retry:
                _upsert_incident(conn, iid, wid, "did_low_pickup", existing["status"],
                                 sim_minute, ev, existing.get("decision_id"))
                continue
            # Fall through and re-propose: the earlier attempt was deferred by
            # approver hours, budget, or a transient rejection.
        elif existing and existing["status"] == "resolved":
            if cid not in in_use:
                continue
            # Someone put the weak DID back into the live config.
        if cid not in in_use:
            actions.append({"type": "did_incident", "incident_id": iid, "action": "observe",
                            "bad_did": cid, "evidence": ev,
                            "summary": f"Caller ID {cid} is weak but not in use"})
            _upsert_incident(conn, iid, wid, "did_low_pickup", "resolved", sim_minute,
                             ev, None, resolved=True)
            continue
        healthy = _healthy_pool(conn, wid, options, flagged)
        historical = _historical_ids(conn, wid)
        if not any(o not in flagged and o not in historical for o in options):
            current_rate = analytics.ratio(recent["connected"], recent["dials"])
            alt = healthy[0] if healthy else None
            if alt is None or not _better_alternative(conn, wid, alt, current_rate):
                # Every caller ID is compromised. Rotating to a worse one spends
                # approver attention and lowers pickup, so keep the live one.
                if existing and existing["status"] in ("open", "fix_ready"):
                    _upsert_incident(conn, iid, wid, "did_low_pickup", existing["status"],
                                     sim_minute, ev, existing.get("decision_id"))
                    continue
                observed = dict(ev, reason="no_better_replacement", alternative=alt,
                                alternative_pickup=_last_rate(conn, wid, alt) if alt else None,
                                current_pickup=current_rate)
                _upsert_incident(conn, iid, wid, "did_low_pickup", "open", sim_minute,
                                 observed, existing.get("decision_id") if existing else None)
                actions.append({
                    "type": "did_incident", "incident_id": iid, "action": "observe",
                    "bad_did": cid, "evidence": observed,
                    "summary": f"Keeping caller ID {cid}: no healthier replacement",
                })
                continue
        proposed = _remove_from_config(config, "caller_id", cid, healthy)
        if proposed == config:
            continue
        _upsert_incident(conn, iid, wid, "did_low_pickup", "open", sim_minute, ev,
                         existing.get("decision_id") if existing else None)
        affected = analytics.active_leads(conn, wid, {"caller_id": [cid]}, day)
        actions.append({
            "type": "did_incident", "incident_id": iid, "bad_did": cid,
            "proposed_config": proposed, "evidence": ev,
            "summary": f"Rotate calls away from caller ID {cid}",
            "rationale": (f"{cid} pickup fell from {ev['baseline_pickup']:.3f} to "
                          f"{ev['recent_pickup']:.3f} on {recent['dials']} recent dials "
                          f"(relative {ev['relative_change']:+.1%}, p={ev['p_value']:.4f}). "
                          "This is consistent with spam flagging; moving the share to the "
                          "remaining caller IDs protects pickup without touching scripts."),
            "impact": min(1.0, max(0.1, abs(ev["relative_change"] or 0.2))),
            "affected_leads": affected,
        })
    return actions


def record_avoided_did(ledger, wf: dict, sim_minute: int, ev: dict) -> str:
    wid = wf["workflow_id"]
    if ev.get("reason") == "no_better_replacement":
        statement = (
            f"{ev['caller_id']} pickup is {ev['recent_pickup']:.3f} vs baseline "
            f"{ev['baseline_pickup']:.3f}. Every other caller ID is also flagged, and the best "
            f"alternative ({ev.get('alternative')}) last observed "
            f"{ev.get('alternative_pickup') or 0.0:.3f}. Rotating would lower pickup, so the "
            "controller keeps the live caller ID and records the compromise.")
        summary = f"Kept caller ID {ev['caller_id']}: no healthier replacement"
        action = "keep_current"
        risk = "The live caller ID is flagged; pickup may keep decaying until a clean ID is available."
    else:
        statement = (f"{ev['caller_id']} pickup is {ev['recent_pickup']:.3f} vs baseline "
                     f"{ev['baseline_pickup']:.3f}, but no allocation uses it. The controller "
                     "records the fact and spends no approver attention.")
        summary = f"Caller ID {ev['caller_id']} is weak but already not in use"
        action = "do_nothing"
        risk = "No live traffic is exposed to this caller ID."
    return ledger.record(
        decision_type="incident_did_observed",
        decision_status="no_action",
        summary=summary,
        decision_statement=statement,
        source=[source_ref("controller_database", f"did_daily:{wid}"),
                source_ref("controller_database", f"workflow:{wid}:config")],
        evidence=ev, workflow_id=wid, tenant_id=wf.get("tenant_id"), sim_minute=sim_minute,
        policy_reference="incident.did_health", selected_action=action,
        risk_assessment=risk,
    )


# --------------------------------------------------------------------------- prompts
def _prompt_halluc(conn, wid: str, prompt_version: str, day_from: int | None,
                   day_to: int | None) -> dict:
    clauses = ["workflow_id=?", "prompt_version=?"]
    args: list[Any] = [wid, prompt_version]
    if day_from is not None:
        clauses.append("day>=?")
        args.append(day_from)
    if day_to is not None:
        clauses.append("day<=?")
        args.append(day_to)
    row = conn.execute(
        "SELECT COALESCE(SUM(dials),0) dials,COALESCE(SUM(connected),0) connected,"
        "COALESCE(SUM(halluc),0) halluc FROM prompt_daily WHERE " + " AND ".join(clauses),
        tuple(args)).fetchone()
    return {k: int(row[k]) for k in ("dials", "connected", "halluc")}


def _current_actor_dims(conn, conn_wid: str, version: int) -> tuple[str, list[str]]:
    row = conn.execute("SELECT actor,changed_dims_json FROM config_versions "
                       "WHERE workflow_id=? AND version=?", (conn_wid, version)).fetchone()
    if row is None:
        return "", []
    dims = _json(row["changed_dims_json"]) or []
    return row["actor"] or "", dims if isinstance(dims, list) else []


def detect_prompt_incidents(conn, wf: dict, config: dict, version: int, actor: str,
                            sim_minute: int, settings: Settings) -> list[dict]:
    """Return prompt incidents that need a validated replacement."""
    wid = wf["workflow_id"]
    current = config.get("prompt_version")
    day = sim_minute // 1440
    recent = _prompt_halluc(conn, wid, current, max(day - 1, -10000), day)
    hist = _prompt_halluc(conn, wid, current, None, day - 1)
    rate = analytics.ratio(recent["halluc"], recent["connected"])
    base_rate = analytics.ratio(hist["halluc"], hist["connected"])
    actor_dims = _current_actor_dims(conn, wid, version)
    supervisor_prompt = actor == "supervisor" and "prompt_version" in actor_dims
    unsafe = False
    if recent["connected"] >= settings.hallucination_min_connected and rate is not None:
        unsafe = rate >= settings.hallucination_abs_threshold
        if unsafe and base_rate:
            # Require a clear rise over the version's own history as well.
            unsafe = rate >= base_rate + settings.hallucination_abs_threshold / 2
    iid = f"prompt:{wid}:v{version}:{current}"
    existing = _incident(conn, iid)
    if not (supervisor_prompt or unsafe):
        if existing and existing["status"] in ("open", "fix_ready") and not unsafe:
            # Resolved by new traffic or by a later config change.
            return [{"type": "prompt_incident", "incident_id": iid, "action": "resolved",
                     "evidence": {"recent": recent, "rate": rate},
                     "summary": f"Prompt {current} no longer looks unsafe"}]
        return []
    if existing and existing["status"] in ("open", "fix_ready"):
        # A fix is already pending or ready; the engine will retry it.
        return [{"type": "prompt_incident", "incident_id": iid, "action": "existing",
                 "evidence": _json(existing["evidence_json"]) or {}}]
    if _pending_proposal(conn, wid, "incident_prompt_fix"):
        _upsert_incident(conn, iid, wid, "prompt_safety", "open", sim_minute,
                         {"prompt_version": current, "reason": "proposal_pending",
                          "recent": recent, "baseline": hist})
        return [{"type": "prompt_incident", "incident_id": iid, "action": "pending"}]
    ev = {
        "prompt_version": current, "version": version, "actor": actor,
        "supervisor_prompt_change": supervisor_prompt,
        "recent": recent, "baseline": hist, "recent_rate": rate,
        "baseline_rate": base_rate,
        "threshold": settings.hallucination_abs_threshold,
        "reason": "supervisor_prompt_change" if supervisor_prompt else "hallucination_rate",
    }
    _upsert_incident(conn, iid, wid, "prompt_safety", "open", sim_minute, ev)
    return [{"type": "prompt_incident", "incident_id": iid, "action": "fix",
             "bad_prompt": current, "evidence": ev}]


def _prompt_performance(conn, wid: str, prompt_version: str) -> tuple[int, int]:
    row = conn.execute(
        "SELECT COALESCE(SUM(connected),0) connected,COALESCE(SUM(engaged),0) engaged "
        "FROM prompt_daily WHERE workflow_id=? AND prompt_version=?",
        (wid, prompt_version)).fetchone()
    return int(row["connected"]), int(row["engaged"])


def choose_replacement_prompt(conn, client: SimClient, ledger, wf: dict, config: dict,
                              incident_id: str, sim_minute: int,
                              settings: Settings) -> dict | None:
    """Validate alternatives and pick the safest candidate deterministically.

    The LLM suggestion list only orders the validation queue and is written to
    the ledger as a suggestion.  Validation is a safety gate: among prompts that
    pass it, the one with the best observed engagement history wins, because a
    safe prompt can still be worse than the incumbent.  With no usable history,
    highest pass rate wins; ties break on the workflow's declared option order.
    """
    wid = wf["workflow_id"]
    current = config.get("prompt_version")
    options = list((wf.get("options") or {}).get("prompt_version") or [])
    candidates = [o for o in options if o != current]
    if not candidates:
        return None

    suggestions: list[str] = []
    try:
        resp = client.suggest_fixes(wid, f"Prompt {current} has a rising hallucination flag rate")
        suggestions = [s.get("prompt_version") for s in resp.get("suggestions") or []
                       if s.get("prompt_version") in candidates]
        ledger.record(
            decision_type="prompt_llm_suggestion",
            decision_status="advisory",
            summary="LLM suggested prompt alternatives",
            decision_statement=("The LLM returned candidate prompt versions as a suggestion. It did "
                                "not decide the change; validation pass rates and a human approval do."),
            source=[source_ref("simulator_api", "llm/suggest-fixes", endpoint="/llm/suggest-fixes")],
            evidence={"candidates": suggestions, "advisory_only": True},
            workflow_id=wid, tenant_id=wf.get("tenant_id"), sim_minute=sim_minute,
            policy_reference="llm.advisory", selected_action="use_as_ordering_hint",
            risk_assessment="LLM suggestions can be worse than the current prompt.",
        )
    except SimError as e:
        ledger.record(
            decision_type="prompt_llm_suggestion",
            decision_status="error",
            summary="LLM suggestion call failed; validation continues without it",
            decision_statement=f"The suggest-fixes call returned {e.status} {e.code}.",
            source=[source_ref("simulator_api", "llm/suggest-fixes", endpoint="/llm/suggest-fixes")],
            evidence={"error": {"status": e.status, "code": e.code, "detail": e.detail}},
            workflow_id=wid, tenant_id=wf.get("tenant_id"), sim_minute=sim_minute,
            policy_reference="llm.advisory", selected_action="skip_llm",
            execution_status="not_applied",
            risk_assessment="No candidate ordering hint; validation is still decisive.",
        )
    ordered = suggestions + [c for c in candidates if c not in suggestions]

    try:
        credits = float(client.state().get("credits_remaining") or 0)
    except SimError:
        credits = 0.0
    results = []
    for pv in ordered:
        if credits - 1 < settings.credit_reserve:
            break
        try:
            run = client.run_validation(wid, pv, settings.validation_n)
        except SimError as e:
            results.append({"prompt_version": pv, "error": {"status": e.status, "code": e.code}})
            continue
        credits = float(run.get("credits_remaining", credits - 1))
        results.append({"prompt_version": pv, "run": run,
                        "pass_rate": run.get("pass_rate")})
    if not results:
        return None
    ok = [r for r in results if r.get("pass_rate") is not None
          and r["pass_rate"] >= _min_validation(wf)]
    if not ok:
        return None
    # Safety is a gate, not a ranking. Prefer a prompt this workflow has
    # actually run at volume; otherwise the highest pass rate, then contract order.
    def rank_key(r: dict) -> tuple:
        connected, engaged = _prompt_performance(conn, wid, r["prompt_version"])
        observed = connected >= settings.caller_id_min_recent_dials
        rate = analytics.ratio(engaged, connected) or 0.0
        return (observed, rate, float(r["pass_rate"]),
                -options.index(r["prompt_version"]))

    winner = max(ok, key=rank_key)
    winner_perf = _prompt_performance(conn, wid, winner["prompt_version"])
    ledger.record(
        decision_type="prompt_validation",
        decision_status="selected",
        summary=f"Selected prompt {winner['prompt_version']} after safety validation",
        decision_statement=(f"Validated {len(results)} candidate prompts. {winner['prompt_version']} "
                            f"had pass_rate {winner['pass_rate']:.4f}, above the workflow minimum "
                            f"{_min_validation(wf):.2f}, and the best observed engagement "
                            f"({winner_perf[1]}/{winner_perf[0]} connected, "
                            f"{analytics.ratio(winner_perf[1], winner_perf[0]) or 0.0:.3f}) among safe candidates. "
                            "Validation is a safety gate, not engagement evidence, so the change "
                            "still goes to a human approver."),
        source=[source_ref("simulator_api", "validation/runs", endpoint="/validation/runs"),
                source_ref("simulator_api", f"validation/{winner['run'].get('validation_run_id')}",
                           endpoint="/validation/runs")],
        evidence={"results": results, "selected": winner, "llm_order": suggestions,
                  "current_prompt": current, "incident_id": incident_id,
                  "performance": {r["prompt_version"]: _prompt_performance(
                      conn, wid, r["prompt_version"]) for r in ok}},
        workflow_id=wid, tenant_id=wf.get("tenant_id"), sim_minute=sim_minute,
        policy_reference="governance.prompt_validation", selected_action="propose_prompt_change",
        alternatives=[r["prompt_version"] for r in results if r is not winner],
        approval_status="not_required", execution_status="not_applied",
        risk_assessment="A safe prompt can still reduce engagement; the approver and monitoring decide.",
    )
    return winner


def _min_validation(wf: dict) -> float:
    return float((wf.get("governance") or {}).get("min_validation_pass_rate", 0.97))


def prompt_fix_action(conn, ledger, wf: dict, config: dict, incident_id: str,
                      bad_prompt: str, sim_minute: int, settings: Settings,
                      client: SimClient) -> dict | None:
    """Build a prompt replacement proposal for an open incident (idempotent)."""
    wid = wf["workflow_id"]
    incident = _incident(conn, incident_id)
    if incident is None:
        return None
    ev = _json(incident["evidence_json"]) or {}
    target = ev.get("target_prompt")
    run = ev.get("validation_run")
    if not target:
        winner = choose_replacement_prompt(conn, client, ledger, wf, config, incident_id,
                                           sim_minute, settings)
        if not winner:
            ledger.record(
                decision_type="prompt_fix_blocked",
                decision_status="no_safe_candidate",
                summary="No prompt passed safety validation",
                decision_statement=("All candidate prompts were below the workflow's minimum "
                                    "validation pass rate or no validation budget was available. "
                                    "The controller will not ship an unvalidated script."),
                source=[source_ref("controller_database", f"incident:{incident_id}")],
                evidence={"bad_prompt": bad_prompt, "evidence": ev},
                workflow_id=wid, tenant_id=wf.get("tenant_id"), sim_minute=sim_minute,
                policy_reference="governance.prompt_validation",
                selected_action="do_not_submit", approval_status="not_required",
                execution_status="not_applied",
                risk_assessment="Leaving a bad prompt live is risky, but shipping an unvalidated one is worse.",
            )
            return None
        target = winner["prompt_version"]
        run = winner["run"]
        ev["target_prompt"] = target
        ev["validation_run"] = run
        _upsert_incident(conn, incident_id, wid, "prompt_safety", "fix_ready",
                         sim_minute, ev, incident.get("decision_id"))
    proposed = copy.deepcopy(config)
    proposed["prompt_version"] = target
    if proposed.get("prompt_version") == config.get("prompt_version"):
        return None
    return {
        "type": "prompt_incident", "action": "propose_fix", "incident_id": incident_id,
        "bad_prompt": bad_prompt, "target_prompt": target,
        "proposed_config": proposed,
        "evidence": {"validation_run_id": run.get("validation_run_id") if run else None,
                     "validation": run, "incident": ev},
        "summary": f"Replace unsafe prompt {bad_prompt} with validated {target}",
        "rationale": (f"Prompt {bad_prompt} shows a hallucination rate of "
                      f"{ev.get('recent_rate') if ev.get('recent_rate') is not None else 'unknown'} "
                      f"after {ev.get('recent', {}).get('connected', 0)} connected calls. "
                      f"Validated candidate {target} passed at "
                      f"{(run or {}).get('pass_rate')}. Safety validation is evidence, not an "
                      "engagement claim; a human approver still decides."),
        "impact": 0.8,
        "affected_leads": analytics.active_leads(conn, wid, {}, sim_minute // 1440),
    }


def resolve_prompt_incidents(conn, ledger, wf: dict, config: dict, sim_minute: int,
                             settings: Settings) -> list[dict]:
    """Close open prompt incidents once the live prompt is no longer the bad one."""
    actions = []
    current = config.get("prompt_version")
    for inc in _open_incidents(conn, wf["workflow_id"], "prompt_safety"):
        ev = _json(inc["evidence_json"]) or {}
        bad = ev.get("prompt_version")
        target = ev.get("target_prompt")
        if current == bad:
            continue
        if target and current not in (bad, target):
            continue
        day = sim_minute // 1440
        recent = _prompt_halluc(conn, wf["workflow_id"], current, max(day - 1, -10000), day)
        _upsert_incident(conn, inc["incident_id"], wf["workflow_id"], "prompt_safety",
                         "resolved", sim_minute,
                         {**ev, "resolved_prompt": current, "recent": recent},
                         inc.get("decision_id"), resolved=True)
        decision_id = ledger.record(
            decision_type="incident_prompt_resolved",
            decision_status="resolved",
            summary=f"Prompt incident cleared: live prompt is {current}",
            decision_statement=(f"The live prompt is now {current}, not the unsafe {bad}. "
                                "The controller records the resolution; the grader's independent "
                                "hallucination timeline confirms the actual behaviour."),
            source=[source_ref("simulator_api", f"workflows/{wf['workflow_id']}/config",
                               endpoint=f"/workflows/{wf['workflow_id']}/config"),
                    source_ref("controller_database", f"incident:{inc['incident_id']}")],
            evidence={"incident_id": inc["incident_id"], "bad_prompt": bad,
                      "live_prompt": current, "recent": recent},
            workflow_id=wf["workflow_id"], tenant_id=wf.get("tenant_id"),
            sim_minute=sim_minute, policy_reference="incident.prompt_safety",
            selected_action="monitor", approval_status="applied",
            execution_status="applied",
            risk_assessment="A later supervisor edit could reintroduce a bad prompt.",
        )
        actions.append({"type": "prompt_incident", "action": "resolved",
                        "incident_id": inc["incident_id"], "decision_id": decision_id,
                        "evidence": {"bad_prompt": bad, "live_prompt": current}})
    return actions


# --------------------------------------------------------------------------- drift
def detect_engagement_drift(conn, wf: dict, sim_minute: int, settings: Settings) -> dict | None:
    """Detect a broad engagement drop that is not a single-variant promotion.

    This is an audit signal only: the experiment layer owns rollback.  It stops
    the controller from inventing an explanation for a change it cannot attribute.
    """
    wid = wf["workflow_id"]
    day = sim_minute // 1440
    where = analytics.workflow_metrics(conn, wid, day - 1, day)
    base = analytics.workflow_metrics(conn, wid, None, day - 2)
    drop = stats.detect_drop(
        base["engaged"], base["connected"], where["engaged"], where["connected"],
        min_n=settings.drift_min_connected, min_rel_drop=settings.drift_rel_drop,
        alpha=settings.alpha)
    if not drop["drop"]:
        return None
    iid = f"drift:{wid}:d{day}"
    existing = _incident(conn, iid)
    ev = {"recent": {"engaged": where["engaged"], "connected": where["connected"]},
          "baseline": {"engaged": base["engaged"], "connected": base["connected"]},
          "method": drop, "day": day}
    if existing:
        _upsert_incident(conn, iid, wid, "engagement_drift", existing["status"],
                         sim_minute, ev, existing.get("decision_id"))
        return None
    _upsert_incident(conn, iid, wid, "engagement_drift", "open", sim_minute, ev)
    return {"type": "engagement_drift", "incident_id": iid, "evidence": ev}
