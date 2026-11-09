"""Schedule learning: hour weights and per-person preferred hours.

The dialler picks *who* to call from a weight table; it does not change the
number of calls.  Hour weights are therefore learned from observed pickup (or
engagement, when that is the primary KPI) with a Beta prior and hard bounds so
a thin hour cannot be starved or over-promoted.

Per-person preferred hours are only learned for low-volume workflows.  At high
volume the person table would be enormous and mostly noise, so those workflows
use cohort/global hour weights instead.
"""
from __future__ import annotations

import copy
import logging
from collections import defaultdict

from . import analytics
from .cohorts import bounded_hour_weights
from .config import Settings

log = logging.getLogger("controller.schedule")


def _metric_rates(conn, wid: str, *, day_from: int | None, day_to: int | None,
                  cohort_id: str | None, primary: str) -> tuple[dict, int]:
    rates = analytics.hour_rates(conn, wid, day_from=day_from, day_to=day_to,
                                 cohort_id=cohort_id)
    total = sum(d for _, d, _ in rates.values())
    if primary == "pickup_rate":
        return {h: (k, n) for h, (k, n, _) in rates.items()}, total
    return {h: (e, c) for h, (_, c, e) in rates.items() if c}, total


def _max_ratio(weights: dict[str, float]) -> float:
    vals = [v for v in weights.values() if v > 0]
    return (max(vals) / min(vals)) if vals else 1.0


def _tv(a: dict[str, float], b: dict[str, float]) -> float:
    keys = set(a) | set(b)
    sa = sum(a.values()) or 1.0
    sb = sum(b.values()) or 1.0
    return 0.5 * sum(abs(a.get(k, 0) / sa - b.get(k, 0) / sb) for k in keys)


def _person_preferences(conn, wf: dict, sim_minute: int, primary: str,
                        settings: Settings) -> dict[str, int]:
    wid = wf["workflow_id"]
    if int(wf.get("dials_per_hour") or 0) > settings.person_hour_max_dph:
        return {}
    day = sim_minute // 1440
    rows = conn.execute(
        "SELECT person_id,hour,SUM(dials) dials,SUM(connected) connected,"
        "SUM(engaged) engaged FROM person_hour WHERE workflow_id=? AND last_minute>=? "
        "GROUP BY person_id,hour",
        (wid, (day - 7) * 1440)).fetchall()
    per: dict[str, dict[int, tuple[int, int]]] = defaultdict(dict)
    for r in rows:
        n = int(r["dials"])
        if n <= 0:
            continue
        if primary == "pickup_rate":
            k = int(r["connected"])
        else:
            k = int(r["engaged"])
            n = int(r["connected"])
            if n <= 0:
                continue
        per[r["person_id"]][int(r["hour"])] = (k, n)
    out: dict[str, int] = {}
    ranked: list[tuple[float, str, int]] = []
    for pid, hours in per.items():
        total_k = sum(k for k, _ in hours.values())
        total_n = sum(n for _, n in hours.values())
        if total_n < 4:
            continue
        overall = (total_k + 1.0) / (total_n + 2.0)
        best = None
        for hour, (k, n) in hours.items():
            if n < 2:
                continue
            rate = (k + 1.0) / (n + 2.0)
            if best is None or rate > best[0] or (rate == best[0] and n > best[2]):
                best = (rate, hour, n)
        if best is None or best[0] < overall * 1.10:
            continue
        ranked.append((best[0], pid, best[1]))
    ranked.sort(key=lambda x: (-x[0], x[1]))
    for _, pid, hour in ranked[:settings.person_hour_max_entries]:
        out[pid] = int(hour)
    return out


def desired_schedule(conn, wf: dict, config: dict, sim_minute: int,
                     settings: Settings) -> dict | None:
    """Return a materially better schedule, or ``None``.

    The learning window includes the history chunk and every day seen so far.
    A proposal is only made when there is enough traffic and the change is not
    noise-sized, which keeps the approver queue and auto-apply log quiet.
    """
    wid = wf["workflow_id"]
    primary = analytics.primary_kpi(wf)
    day = sim_minute // 1440
    lo, hi = wf.get("dialling_window") or [0, 24]
    current = (config.get("schedule") or {})
    cur_hw = dict(current.get("hour_weights") or {})
    cur_pref = dict(current.get("lead_preferred_hour") or {})

    default_rates, total = _metric_rates(conn, wid, day_from=None, day_to=day,
                                         cohort_id=None, primary=primary)
    if total < settings.schedule_min_dials:
        return None
    new_default = bounded_hour_weights(default_rates, [lo, hi])
    if _max_ratio(new_default) < 1.10:
        # Flat demand: a table would only add churn.
        new_default = {}

    new_hw: dict[str, dict[str, float]] = {}
    if new_default:
        new_hw["default"] = new_default
    for c in config.get("cohorts") or []:
        cid = c.get("id")
        if not cid:
            continue
        rates, n = _metric_rates(conn, wid, day_from=None, day_to=day, cohort_id=cid,
                                 primary=primary)
        if n < max(300, settings.schedule_min_dials // 3):
            continue
        w = bounded_hour_weights(rates, [lo, hi])
        if _max_ratio(w) >= 1.15:
            new_hw[cid] = w

    new_pref = {}
    if int(wf.get("dials_per_hour") or 0) <= settings.person_hour_max_dph:
        new_pref = _person_preferences(conn, wf, sim_minute, primary, settings)
        if len(new_pref) < 10:
            new_pref = {}
    # Keep older preferences that are still within the cap; the newly measured
    # ones win on conflict.
    merged_pref = dict(cur_pref)
    merged_pref.update(new_pref)
    if len(merged_pref) > settings.person_hour_max_entries:
        keep = sorted(new_pref)[: settings.person_hour_max_entries]
        merged_pref = {p: new_pref[p] for p in keep}
    changed_pref = sum(1 for p, h in new_pref.items() if cur_pref.get(p) != h)
    pref_material = changed_pref >= max(20, int(0.05 * max(len(cur_pref), 1)))

    hw_tv = _tv(cur_hw.get("default") or {}, new_hw.get("default") or {})
    cohort_tv = max([_tv(cur_hw.get(c) or {}, new_hw.get(c) or {})
                     for c in set(cur_hw) | set(new_hw) if c != "default"] or [0.0])
    weight_material = hw_tv >= 0.15 or cohort_tv >= 0.20
    if not weight_material and not pref_material:
        return None

    schedule = {
        "hour_weights": new_hw if new_hw else cur_hw,
        "lead_preferred_hour": merged_pref if merged_pref else cur_pref,
    }
    if schedule == current:
        return None
    top_hours = sorted(new_default.items(), key=lambda kv: -kv[1])[:6] if new_default else []
    evidence = {
        "method": ("Beta-smoothed observed primary-KPI rate per hour, clipped to "
                   "[0.3, 3.0]; all dialling-window hours retained"),
        "primary": primary,
        "window": [lo, hi],
        "observed_dials": total,
        "default_weights": new_default,
        "cohort_tables": list(new_hw),
        "top_hours": top_hours,
        "person_hours_learned": len(new_pref),
        "person_hours_changed": changed_pref,
        "hour_weight_tv": round(hw_tv, 4),
        "cohort_weight_tv": round(cohort_tv, 4),
        "previous_default": cur_hw.get("default") or {},
    }
    if weight_material:
        rationale = (f"Across {total} observed dials, the best and worst dialling hours "
                     f"differ enough to matter (max/min weight "
                     f"{_max_ratio(new_default or {}):.2f}).")
    else:
        rationale = f"Measured {len(new_pref)} returning people with a repeatable best hour."
    return {
        "schedule": schedule, "evidence": evidence,
        "summary": ("Rebalance dialling hours toward the hours that actually connect"
                    if weight_material else "Learn per-person preferred dialling hours"),
        "rationale": rationale,
        "impact": min(0.6, 0.25 + hw_tv),
        "affected_leads": analytics.active_leads(conn, wid, {}, day),
    }


def proposed_config(config: dict, schedule: dict) -> dict:
    cfg = copy.deepcopy(config)
    cfg["schedule"] = schedule
    return cfg
