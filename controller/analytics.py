"""Metric queries over the aggregate store.

All rates use the definitions from the assignment and return ``None`` rather
than fabricating a value when the denominator is zero.
"""
from __future__ import annotations

from typing import Any

from .cohorts import match_features


def _one(conn, sql: str, args: tuple = ()) -> dict:
    row = conn.execute(sql, args).fetchone()
    return dict(row) if row else {}


def _where(wf: str, day_from: int | None, day_to: int | None,
           alias: str = "") -> tuple[str, list[Any]]:
    p = f"{alias}." if alias else ""
    clauses = [f"{p}workflow_id=?"]
    args: list[Any] = [wf]
    if day_from is not None:
        clauses.append(f"{p}day>=?")
        args.append(int(day_from))
    if day_to is not None:
        clauses.append(f"{p}day<=?")
        args.append(int(day_to))
    return " AND ".join(clauses), args


def ratio(k: int | float | None, n: int | float | None) -> float | None:
    if not n:
        return None
    return float(k or 0) / float(n)


def workflow_metrics(conn, wf: str, day_from: int | None = None,
                     day_to: int | None = None) -> dict:
    where, args = _where(wf, day_from, day_to)
    v = _one(conn, f"SELECT COALESCE(SUM(dials),0) dials, COALESCE(SUM(connected),0) connected,"
                   f" COALESCE(SUM(engaged),0) engaged, COALESCE(SUM(halluc),0) halluc,"
                   f" COALESCE(SUM(interested),0) interested, COALESCE(SUM(flow_sum),0) flow_sum,"
                   f" COALESCE(SUM(flow_n),0) flow_n FROM variant_daily WHERE {where}", tuple(args))
    c = _one(conn, f"SELECT COALESCE(SUM(concluded),0) concluded FROM concluded_daily WHERE {where}",
             tuple(args))
    camp_clauses = ["workflow_id=?"]
    camp_args: list[Any] = [wf]
    if day_from is not None:
        camp_clauses.append("created_day>=?")
        camp_args.append(int(day_from))
    if day_to is not None:
        camp_clauses.append("created_day<=?")
        camp_args.append(int(day_to))
    camp = _one(conn, "SELECT COALESCE(SUM(leads),0) uploaded, COUNT(*) campaigns "
                      f"FROM campaigns WHERE {' AND '.join(camp_clauses)}", tuple(camp_args))
    out = {
        "workflow_id": wf,
        "dials": int(v.get("dials", 0)),
        "connected": int(v.get("connected", 0)),
        "engaged": int(v.get("engaged", 0)),
        "hallucinated": int(v.get("halluc", 0)),
        "interested": int(v.get("interested", 0)),
        "flow_adherence": (v["flow_sum"] / v["flow_n"]) if v.get("flow_n") else None,
        "concluded_leads": int(c.get("concluded", 0)),
        "uploaded_leads": int(camp.get("uploaded", 0)),
        "campaigns": int(camp.get("campaigns", 0)),
    }
    out["pickup_rate"] = ratio(out["connected"], out["dials"])
    out["engagement_rate"] = ratio(out["engaged"], out["connected"])
    out["conclusion_rate"] = ratio(out["concluded_leads"], out["uploaded_leads"])
    out["hallucination_rate"] = ratio(out["hallucinated"], out["connected"])
    return out


def arm_stats(conn, wf: str, dim: str, value: str, *, day_from: int | None = None,
              day_to: int | None = None, cohort_id: str | None = None,
              config_version: int | None = None,
              config_version_min: int | None = None) -> dict:
    if dim not in ("welcome_message", "voice", "caller_id"):
        raise ValueError(dim)
    clauses = ["workflow_id=?", f"{dim}=?"]
    args: list[Any] = [wf, value]
    if day_from is not None:
        clauses.append("day>=?")
        args.append(int(day_from))
    if day_to is not None:
        clauses.append("day<=?")
        args.append(int(day_to))
    if cohort_id:
        clauses.append("cohort_id=?")
        args.append(cohort_id)
    if config_version is not None:
        clauses.append("config_version=?")
        args.append(int(config_version))
    if config_version_min is not None:
        clauses.append("config_version>=?")
        args.append(int(config_version_min))
    row = _one(conn, "SELECT COALESCE(SUM(dials),0) dials,COALESCE(SUM(connected),0) connected,"
                     "COALESCE(SUM(engaged),0) engaged,COALESCE(SUM(halluc),0) halluc "
                     f"FROM variant_daily WHERE {' AND '.join(clauses)}", tuple(args))
    out = {k: int(row.get(k, 0)) for k in ("dials", "connected", "engaged", "halluc")}
    out["pickup_rate"] = ratio(out["connected"], out["dials"])
    out["engagement_rate"] = ratio(out["engaged"], out["connected"])
    out["dim"] = dim
    out["value"] = value
    return out


def feature_variant_rows(conn, wf: str, dim: str, *, day_from: int | None = None,
                         day_to: int | None = None,
                         config_version: int | None = None,
                         config_version_min: int | None = None) -> list[dict]:
    clauses = ["workflow_id=?", "dim=?"]
    args: list[Any] = [wf, dim]
    if day_from is not None:
        clauses.append("day>=?")
        args.append(int(day_from))
    if day_to is not None:
        clauses.append("day<=?")
        args.append(int(day_to))
    if config_version is not None:
        clauses.append("config_version=?")
        args.append(int(config_version))
    if config_version_min is not None:
        clauses.append("config_version>=?")
        args.append(int(config_version_min))
    rows = conn.execute(
        "SELECT feature,feature_value,variant_value,SUM(dials) dials,SUM(connected) connected,"
        "SUM(engaged) engaged,SUM(halluc) halluc FROM feature_variant_daily "
        f"WHERE {' AND '.join(clauses)} GROUP BY feature,feature_value,variant_value", tuple(args)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["pickup_rate"] = ratio(d["connected"], d["dials"])
        d["engagement_rate"] = ratio(d["engaged"], d["connected"])
        out.append(d)
    return out


def did_rows(conn, wf: str, *, day_from: int | None = None,
             day_to: int | None = None) -> list[dict]:
    where, args = _where(wf, day_from, day_to)
    rows = conn.execute(
        f"SELECT caller_id,SUM(dials) dials,SUM(connected) connected,SUM(engaged) engaged,"
        f"SUM(halluc) halluc FROM did_daily WHERE {where} GROUP BY caller_id",
        tuple(args)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["pickup_rate"] = ratio(d["connected"], d["dials"])
        d["engagement_rate"] = ratio(d["engaged"], d["connected"])
        out.append(d)
    return out


def did_by_day(conn, wf: str, caller_id: str, *, day_from: int | None = None,
               day_to: int | None = None) -> list[dict]:
    where, args = _where(wf, day_from, day_to)
    rows = conn.execute(
        f"SELECT day,SUM(dials) dials,SUM(connected) connected,SUM(engaged) engaged,SUM(halluc) halluc "
        f"FROM did_daily WHERE {where} AND caller_id=? GROUP BY day ORDER BY day",
        tuple(args) + (caller_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["pickup_rate"] = ratio(d["connected"], d["dials"])
        out.append(d)
    return out


def prompt_rows(conn, wf: str, *, day_from: int | None = None,
                day_to: int | None = None) -> list[dict]:
    where, args = _where(wf, day_from, day_to)
    rows = conn.execute(
        f"SELECT prompt_version,SUM(dials) dials,SUM(connected) connected,SUM(engaged) engaged,"
        f"SUM(halluc) halluc FROM prompt_daily WHERE {where} GROUP BY prompt_version",
        tuple(args)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["pickup_rate"] = ratio(d["connected"], d["dials"])
        d["engagement_rate"] = ratio(d["engaged"], d["connected"])
        d["hallucination_rate"] = ratio(d["halluc"], d["connected"])
        out.append(d)
    return out


def prompt_by_day(conn, wf: str, prompt_version: str, *, day_from: int | None = None,
                  day_to: int | None = None) -> list[dict]:
    where, args = _where(wf, day_from, day_to)
    rows = conn.execute(
        f"SELECT day,SUM(dials) dials,SUM(connected) connected,SUM(engaged) engaged,SUM(halluc) halluc "
        f"FROM prompt_daily WHERE {where} AND prompt_version=? GROUP BY day ORDER BY day",
        tuple(args) + (prompt_version,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["hallucination_rate"] = ratio(d["halluc"], d["connected"])
        out.append(d)
    return out


def hour_rates(conn, wf: str, *, day_from: int | None = None, day_to: int | None = None,
               cohort_id: str | None = None, feature: str | None = None,
               feature_value: str | None = None) -> dict[int, tuple[int, int, int]]:
    if cohort_id is not None:
        where, args = _where(wf, day_from, day_to)
        rows = conn.execute(
            f"SELECT hour,SUM(dials) dials,SUM(connected) connected,SUM(engaged) engaged "
            f"FROM cohort_hour_daily WHERE {where} AND cohort_id=? GROUP BY hour",
            tuple(args) + (cohort_id,)).fetchall()
    elif feature and feature_value is not None:
        where, args = _where(wf, day_from, day_to)
        rows = conn.execute(
            f"SELECT hour,SUM(dials) dials,SUM(connected) connected,SUM(engaged) engaged "
            f"FROM hour_feature_daily WHERE {where} AND feature=? AND feature_value=? GROUP BY hour",
            tuple(args) + (feature, feature_value)).fetchall()
    else:
        where, args = _where(wf, day_from, day_to)
        rows = conn.execute(
            f"SELECT hour,SUM(dials) dials,SUM(connected) connected,SUM(engaged) engaged "
            f"FROM cohort_hour_daily WHERE {where} GROUP BY hour",
            tuple(args)).fetchall()
    return {int(r["hour"]): (int(r["connected"]), int(r["dials"]), int(r["engaged"]))
            for r in rows}


def lead_rows(conn, wf: str, *, day_from: int | None = None,
              day_to: int | None = None) -> list[dict]:
    where, args = _where(wf, day_from, day_to)
    return [dict(r) for r in conn.execute(
        f"SELECT lang,city_tier,age_band,value_band,SUM(uploaded) uploaded "
        f"FROM lead_daily WHERE {where} GROUP BY lang,city_tier,age_band,value_band",
        tuple(args)).fetchall()]


def cohort_share(conn, wf: str, where: dict, *, day_from: int | None = None,
                 day_to: int | None = None) -> dict:
    rows = lead_rows(conn, wf, day_from=day_from, day_to=day_to)
    total = sum(int(r["uploaded"]) for r in rows)
    matched = sum(int(r["uploaded"]) for r in rows if match_features(where, r))
    return {"matched": matched, "total": total,
            "share": (matched / total) if total else None}


def active_leads(conn, wf: str, where: dict, current_day: int, lookback: int = 3) -> int:
    share = cohort_share(conn, wf, where, day_from=current_day - lookback, day_to=current_day)
    # Uploaded over a few days is a conservative proxy for leads still in flight.
    return int(share["matched"])


def variant_daily_series(conn, wf: str, dim: str, value: str, *, day_from: int | None = None,
                         day_to: int | None = None, cohort_id: str | None = None,
                         config_version_min: int | None = None) -> list[dict]:
    clauses = ["workflow_id=?", f"{dim}=?"]
    args: list[Any] = [wf, value]
    if day_from is not None:
        clauses.append("day>=?")
        args.append(int(day_from))
    if day_to is not None:
        clauses.append("day<=?")
        args.append(int(day_to))
    if cohort_id:
        clauses.append("cohort_id=?")
        args.append(cohort_id)
    if config_version_min is not None:
        clauses.append("config_version>=?")
        args.append(int(config_version_min))
    rows = conn.execute(
        "SELECT day,SUM(dials) dials,SUM(connected) connected,SUM(engaged) engaged,"
        "SUM(halluc) halluc FROM variant_daily "
        f"WHERE {' AND '.join(clauses)} GROUP BY day ORDER BY day", tuple(args)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["pickup_rate"] = ratio(d["connected"], d["dials"])
        d["engagement_rate"] = ratio(d["engaged"], d["connected"])
        out.append(d)
    return out


def guardrail_spec(wf: dict) -> tuple[str, float]:
    obj = wf.get("objective") or {}
    primary = obj.get("primary") or "engagement_rate"
    guard = obj.get("guardrail") or {}
    if primary == "pickup_rate":
        return "engagement_rate", float(guard.get("engagement_rate_max_drop", 0.02))
    return "pickup_rate", float(guard.get("pickup_rate_max_drop", 0.02))


def primary_kpi(wf: dict) -> str:
    return (wf.get("objective") or {}).get("primary") or "engagement_rate"


def current_prompt_config(config: dict) -> str:
    return config.get("prompt_version") or ""


def option_baseline(wf: dict, config: dict, dim: str) -> str | None:
    dist = ((config.get("allocations") or {}).get("default") or {}).get(dim) or {}
    if not dist:
        return None
    return max(dist, key=lambda o: dist[o])


def total_dials(conn, wf: str, day_from: int | None = None, day_to: int | None = None) -> int:
    return int(workflow_metrics(conn, wf, day_from, day_to)["dials"])
