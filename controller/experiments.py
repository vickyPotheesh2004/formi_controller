"""Experiment design, deterministic evaluation, and decay detection.

A workflow's agent dimensions are explored one study at a time (welcome
message and voice).  Evaluations are deterministic: two-proportion tests with
a practical-effect floor, a guardrail check, and a repeated-confirmation rule.
Feature-level heterogeneity is only turned into a cohort after the same cell
survives repeated evaluation, which keeps false cohorts out while still
letting the controller learn "this voice is better for this group" from the
same randomised traffic.

The module owns *learning state*, not governance.  It returns proposed actions;
the engine records them and routes every config change through ``Governance``.
"""
from __future__ import annotations

import copy
import logging
from typing import Any

from . import analytics, db, stats
from .cohorts import cohort_id_for, split_allocations, winner_allocation
from .config import AGENT_DIMS, Settings

log = logging.getLogger("controller.experiments")

STUDY_DIMS = ("welcome_message", "voice")
MAX_STUDY_DAYS = 5.0


def load_state(conn, wid: str) -> dict:
    state = db.load_notes(conn, wid)
    state.setdefault("studies", {})
    state.setdefault("promotions", {})
    state.setdefault("cohort_wins", {})
    state.setdefault("blocked", {})
    return state


def save_state(conn, wid: str, state: dict) -> None:
    db.save_notes(conn, wid, state)


def _blocked(state: dict, dim: str, option: str, sim_minute: int) -> bool:
    rec = (state.get("blocked") or {}).get(f"{dim}:{option}")
    return bool(rec and int(rec.get("until_minute", 0)) > sim_minute)


def block(state: dict, dim: str, option: str, sim_minute: int, days: float, reason: str) -> None:
    state.setdefault("blocked", {})[f"{dim}:{option}"] = {
        "until_minute": int(sim_minute + days * 1440), "reason": reason,
    }


def _dist_close(a: dict, b: dict) -> bool:
    keys = set(a) | set(b)
    return all(abs(float(a.get(k, 0)) - float(b.get(k, 0))) < 0.02 for k in keys)


# --------------------------------------------------------------------------- studies
def ensure_studies(conn, wf: dict, config: dict, state: dict, sim_minute: int,
                   settings: Settings) -> list[dict]:
    """Create an exploration study for each agent dimension of a KPI.

    Pickup-primary workflows are handled by caller-ID health and scheduling;
    changing their welcome/voice would risk the engagement guardrail without
    moving the primary KPI.
    """
    if analytics.primary_kpi(wf) != "engagement_rate":
        return []
    options_by_dim = wf.get("options") or {}
    created = []
    for dim in STUDY_DIMS:
        if dim in (state.get("supervisor_dims") or {}):
            # A supervisor explicitly set this dimension; re-randomising it after
            # the cooldown would quietly undo their decision and confound the base.
            continue
        existing = state["studies"].get(dim)
        if existing and existing.get("status") != "closed":
            continue
        if dim in state.get("promotions", {}):
            continue
        win = (state.get("cohort_wins") or {}).get(dim)
        if win and sim_minute - int(win.get("at_minute", 0)) < 3 * 1440:
            # A targeted cohort plan is still being routed/applied. Re-opening
            # global exploration would overwrite the win with a confounded
            # mixture before the cohort has any traffic.
            continue
        options = list(options_by_dim.get(dim) or [])
        if len(options) < 2:
            continue
        base = analytics.option_baseline(wf, config, dim) or options[0]
        candidates = [o for o in options
                      if o != base and not _blocked(state, dim, o, sim_minute)]
        if not candidates:
            continue
        alloc = split_allocations(options, base, exploration=settings.exploration_share,
                                  control=0.0)
        alloc = {k: v for k, v in alloc.items() if v > 1e-9}
        state["studies"][dim] = {
            "baseline": base,
            "candidates": candidates,
            "allocation": alloc,
            "created_minute": sim_minute,
            "started_minute": None,
            "started_day": None,
            "status": "planned",
            "confirm": {c: 0 for c in candidates},
            "drop": {c: 0 for c in candidates},
            "feature_confirm": {},
            "scan_day_to": None,
        }
        created.append({"type": "study_created", "dim": dim, "baseline": base,
                        "candidates": candidates, "allocation": alloc})
    return created


def mark_started(wf: dict, config: dict, state: dict, sim_minute: int) -> list[dict]:
    started = []
    default = (config.get("allocations") or {}).get("default") or {}
    for dim, st in state["studies"].items():
        if st.get("status") != "planned":
            continue
        if _dist_close(default.get(dim) or {}, st.get("allocation") or {}):
            st["status"] = "running"
            st["started_minute"] = sim_minute
            st["started_day"] = sim_minute // 1440
            started.append({"type": "study_started", "dim": dim,
                            "at": sim_minute, "allocation": st["allocation"]})
    return started


def desired_distribution(wf: dict, config: dict, state: dict, dim: str,
                         settings: Settings) -> dict:
    """The default-cohort distribution the controller wants for one dimension."""
    default = ((config.get("allocations") or {}).get("default") or {})
    current = dict(default.get(dim) or {})
    promo = state.get("promotions", {}).get(dim)
    if promo:
        out = winner_allocation(list((wf.get("options") or {}).get(dim) or []),
                                promo["value"], settings.control_share)
        if out:
            return out
    st = state["studies"].get(dim)
    if st and st.get("status") in ("planned", "running"):
        return copy.deepcopy(st.get("allocation") or current)
    if st and st.get("status") == "closed":
        if st.get("close_reason") == "cohort_win":
            base = st.get("baseline")
            return {base: 1.0} if base else current
        # A study that ended without a winner (or was closed by a supervisor
        # edit) leaves the live split untouched. Reverting to 100% baseline for
        # a few hours only to re-randomise for the next study is pure churn and
        # can throw away a real, still-unconfirmed lift.
        return current
    return current


# --------------------------------------------------------------------------- evaluation
def _rate_row(row: dict, primary: str) -> tuple[int, int]:
    if primary == "pickup_rate":
        return int(row.get("connected", 0)), int(row.get("dials", 0))
    return int(row.get("engaged", 0)), int(row.get("connected", 0))


def _other_arm(conn, wf: dict, dim: str, winner: str, options: list[str], *,
               day_from: int, day_to: int | None, cohort_id: str | None = None) -> dict:
    total = {"connected": 0, "engaged": 0, "dials": 0, "halluc": 0}
    for o in options:
        if o == winner:
            continue
        s = analytics.arm_stats(conn, wf["workflow_id"], dim, o, day_from=day_from,
                                day_to=day_to, cohort_id=cohort_id)
        for k in total:
            total[k] += int(s.get(k, 0))
    total["pickup_rate"] = analytics.ratio(total["connected"], total["dials"])
    total["engagement_rate"] = analytics.ratio(total["engaged"], total["connected"])
    return total


def evaluate(conn, wf: dict, config: dict, state: dict, sim_minute: int,
             settings: Settings) -> list[dict]:
    """Evaluate running studies and decay of promoted variants."""
    actions: list[dict] = []
    primary = analytics.primary_kpi(wf)
    guard_metric, guard_drop = analytics.guardrail_spec(wf)
    current_day = sim_minute // 1440

    for dim, st in list(state["studies"].items()):
        if st.get("status") != "running":
            continue
        started = st.get("started_minute")
        if started is None:
            continue
        elapsed_days = (sim_minute - started) / 1440.0
        if elapsed_days < settings.min_observation_days:
            continue
        base = analytics.arm_stats(conn, wf["workflow_id"], dim, st["baseline"],
                                   day_from=st["started_day"], day_to=current_day)
        options = list((wf.get("options") or {}).get(dim) or [])
        cands = [c for c in st.get("candidates", []) if c in options]
        alpha = stats.bonferroni(settings.promotion_alpha, max(1, len(cands)))
        winner = None
        for cand in cands:
            cand_stats = analytics.arm_stats(conn, wf["workflow_id"], dim, cand,
                                             day_from=st["started_day"], day_to=current_day)
            k1, n1 = _rate_row(base, primary)
            k2, n2 = _rate_row(cand_stats, primary)
            ev = stats.evaluate_candidate(k1, n1, k2, n2, min_n=settings.min_arm_connected,
                                          mde_rel=(settings.mde_pickup if primary == "pickup_rate"
                                                   else settings.mde_engagement), alpha=alpha)
            guard_ok = True
            if guard_metric == "pickup_rate" and primary != "pickup_rate":
                g = stats.guardrail_regressed(
                    base["connected"], base["dials"], cand_stats["connected"],
                    cand_stats["dials"], max_abs_drop=guard_drop, alpha=alpha,
                    min_n=settings.min_arm_connected)
                guard_ok = not g["regressed"]
                ev["guardrail"] = g
            ev["candidate"] = cand
            ev["dim"] = dim

            if ev["eligible"] and guard_ok:
                st["confirm"][cand] = int(st["confirm"].get(cand, 0)) + 1
                st["drop"][cand] = 0
                if st["confirm"][cand] >= settings.promotion_confirmations:
                    rel = ev.get("relative_lift") or 0.0
                    if winner is None or rel > winner[0]:
                        winner = (rel, cand, ev)
            else:
                st["confirm"][cand] = 0
                # A challenger that is clearly worse is removed so traffic is
                # not spent on a known loser.
                if ev["sufficient"] and ev.get("diff") is not None and ev["diff"] < 0 \
                        and ev.get("p_value") is not None and ev["p_value"] < alpha \
                        and (ev.get("relative_lift") or 0) <= -settings.mde_engagement:
                    st["drop"][cand] = int(st["drop"].get(cand, 0)) + 1
                    if st["drop"][cand] >= 2:
                        actions.append({"type": "drop_candidate", "dim": dim, "value": cand,
                                        "evidence": ev, "at": sim_minute})
                        st["candidates"] = [c for c in st["candidates"] if c != cand]
                        block(state, dim, cand, sim_minute, 3.0,
                              "statistically worse than baseline")
                        alloc = dict(st.get("allocation") or {})
                        alloc.pop(cand, None)
                        if alloc:
                            total = sum(alloc.values()) or 1.0
                            st["allocation"] = {k: v / total for k, v in alloc.items()}
                        actions.append({"type": "allocation_changed", "dim": dim,
                                        "allocation": st.get("allocation"),
                                        "reason": "dropped_loser", "at": sim_minute})

        if winner is not None:
            rel, cand, ev = winner
            state["promotions"][dim] = {
                "value": cand, "baseline": st["baseline"], "at_minute": sim_minute,
                "control": settings.control_share, "evidence": ev,
            }
            st["status"] = "closed"
            st["closed_minute"] = sim_minute
            st["winner"] = cand
            actions.append({"type": "promote", "dim": dim, "value": cand,
                            "baseline": st["baseline"], "evidence": ev, "at": sim_minute,
                            "study": st})
            continue

        # Feature-level discovery is useful only while the default allocation
        # is still randomised across options.  A promotion closes the study.
        if elapsed_days >= settings.min_observation_days:
            found = _feature_scan(conn, wf, state, st, dim, sim_minute, settings, primary)
            if found:
                actions.append(found)
                # Stop global exploration: apply the targeted win instead of
                # continuing to spend traffic on a confounded mixture.
                st["status"] = "closed"
                st["closed_minute"] = sim_minute
                st["close_reason"] = "cohort_win"
                state.setdefault("cohort_wins", {})[dim] = {
                    "where": found["where"], "value": found["value"],
                    "baseline": found["baseline"], "at_minute": sim_minute,
                    "evidence": found["evidence"],
                }
                found["study_closed"] = True
                continue

        if elapsed_days > MAX_STUDY_DAYS:
            st["status"] = "closed"
            st["closed_minute"] = sim_minute
            st["close_reason"] = "no_winner"
            actions.append({"type": "close_study", "dim": dim, "reason": "no_winner",
                            "baseline": st["baseline"], "at": sim_minute,
                            "evidence": {"elapsed_days": elapsed_days,
                                         "baseline": base, "candidates": cands}})

    actions.extend(_decay_actions(conn, wf, state, sim_minute, settings, primary))
    return actions


def _cell_eval(conn, wf: dict, st: dict, dim: str, feature: str, value: str,
               cand: str, *, day_from: int, day_to: int, settings: Settings,
               primary: str) -> dict | None:
    rows = analytics.feature_variant_rows(conn, wf["workflow_id"], dim,
                                          day_from=day_from, day_to=day_to)
    arms = {r["variant_value"]: r for r in rows
            if r["feature"] == feature and r["feature_value"] == value}
    base = arms.get(st["baseline"])
    arm = arms.get(cand)
    if not base or not arm:
        return None
    b_k, b_n = _rate_row(base, primary)
    c_k, c_n = _rate_row(arm, primary)
    return stats.evaluate_candidate(
        b_k, b_n, c_k, c_n, min_n=settings.min_cohort_arm_connected,
        mde_rel=(settings.mde_pickup if primary == "pickup_rate"
                 else settings.mde_engagement),
        alpha=settings.promotion_alpha)


def _feature_scan(conn, wf: dict, state: dict, st: dict, dim: str, sim_minute: int,
                  settings: Settings, primary: str) -> dict | None:
    """Look for a feature cell that repeats on independent data.

    Aggregates are day-grained, so scanning more than once a day only re-tests
    the same observations.  A cell is first discovered on cumulative data; it
    is then re-tested on data collected *after* the discovery day.  Only a cell
    that survives that independent confirmation is targeted, which is the
    guard against the many-test false positives a single scan produces.
    """
    started_day = st.get("started_day")
    if started_day is None:
        return None
    current_day = sim_minute // 1440
    # Scan once per day, late enough that the day's calls have accumulated.
    # A midnight scan would set the day marker before any data exists and then
    # skip the rest of that day.
    if sim_minute % 1440 < 20 * 60:
        return None
    if current_day <= int(st.get("feature_scan_day", started_day - 1)):
        return None
    st["feature_scan_day"] = current_day
    rows = analytics.feature_variant_rows(conn, wf["workflow_id"], dim,
                                          day_from=started_day,
                                          day_to=current_day)
    cells: dict[tuple[str, str], dict[str, dict]] = {}
    for r in rows:
        cells.setdefault((r["feature"], r["feature_value"]), {})[r["variant_value"]] = r
    discoveries = st.setdefault("feature_discovery", {})
    best: tuple[float, dict] | None = None
    for (feature, value), arms in cells.items():
        base = arms.get(st["baseline"])
        if not base:
            continue
        for cand, arm in arms.items():
            if cand == st["baseline"] or cand not in st.get("candidates", []):
                continue
            key = f"{feature}:{value}:{cand}"
            rec = discoveries.get(key)
            if not rec:
                b_k, b_n = _rate_row(base, primary)
                c_k, c_n = _rate_row(arm, primary)
                ev = stats.evaluate_candidate(
                    b_k, b_n, c_k, c_n, min_n=settings.min_cohort_arm_connected,
                    mde_rel=(settings.mde_pickup if primary == "pickup_rate"
                             else settings.mde_engagement),
                    alpha=settings.promotion_alpha)
                if ev["eligible"] and (ev.get("diff") or 0) > 0:
                    rec = {"day": current_day, "count": 1, "evidence": ev,
                           "where": {feature: [value]}, "value": cand}
                    discoveries[key] = rec
                st["feature_confirm"][key] = int(rec.get("count", 0)) if rec else 0
                continue
            if current_day <= int(rec.get("day", 0)):
                st["feature_confirm"][key] = int(rec.get("count", 0))
                continue
            fresh = _cell_eval(conn, wf, st, dim, feature, value, cand,
                               day_from=int(rec["day"]) + 1, day_to=current_day,
                               settings=settings, primary=primary)
            if fresh and fresh["eligible"] and (fresh.get("diff") or 0) > 0:
                rec["count"] = int(rec.get("count", 1)) + 1
                rec["day"] = current_day
                rec["evidence"] = fresh
            else:
                # The independent data did not reproduce the cell. Keep the
                # discovery for a later window but do not call it a win.
                rec["count"] = 1
            st["feature_confirm"][key] = int(rec["count"])
            if int(rec["count"]) < 2:
                continue
            share = analytics.cohort_share(conn, wf["workflow_id"], {feature: [value]},
                                           day_from=current_day - 14, day_to=current_day)
            if not share["total"] or (share["share"] or 0) < settings.min_cohort_share:
                continue
            ev = rec.get("evidence") or fresh
            rel = ev.get("relative_lift") or 0.0
            if best is None or rel > best[0]:
                best = (rel, {
                    "type": "cohort_win", "dim": dim,
                    "where": {feature: [value]}, "value": cand,
                    "baseline": st["baseline"],
                    "options": list((wf.get("options") or {}).get(dim) or []),
                    "evidence": {
                        **ev, "feature": feature, "feature_value": value,
                        "cohort_share": share["share"],
                        "confirmations": int(rec["count"]),
                        "confirmation_window": [int(rec["day"]), current_day],
                    },
                    "at": sim_minute,
                })
    return best[1] if best else None


def _decay_actions(conn, wf: dict, state: dict, sim_minute: int, settings: Settings,
                   primary: str) -> list[dict]:
    actions = []
    options_by_dim = wf.get("options") or {}
    for dim, promo in list(state.get("promotions", {}).items()):
        value = promo.get("value")
        options = list(options_by_dim.get(dim) or [])
        if value not in options or value == promo.get("baseline"):
            continue
        promo_day = int(promo.get("at_minute", 0)) // 1440
        current_day = sim_minute // 1440
        if current_day - promo_day < 2:
            continue
        recent_day = current_day
        early_to = current_day - 1
        series = analytics.variant_daily_series(conn, wf["workflow_id"], dim, value,
                                                day_from=promo_day, day_to=early_to)
        recent = analytics.variant_daily_series(conn, wf["workflow_id"], dim, value,
                                                day_from=recent_day, day_to=current_day)
        e = sum(int(r["engaged"]) for r in series)
        e_n = sum(int(r["connected"]) for r in series)
        r = sum(int(r["engaged"]) for r in recent)
        r_n = sum(int(r["connected"]) for r in recent)
        if primary == "pickup_rate":
            e, e_n = sum(int(x["connected"]) for x in series), sum(int(x["dials"]) for x in series)
            r, r_n = sum(int(x["connected"]) for x in recent), sum(int(x["dials"]) for x in recent)
        drop = stats.detect_drop(e, e_n, r, r_n, min_n=settings.drift_min_connected,
                                 min_rel_drop=settings.decay_rel_drop, alpha=settings.alpha)
        if not drop["drop"]:
            continue
        # A shared drop across the control arms is drift, not variant decay.
        ctrl_series = _other_arm(conn, wf, dim, value, options,
                                 day_from=promo_day, day_to=early_to)
        ctrl_recent = _other_arm(conn, wf, dim, value, options,
                                 day_from=recent_day, day_to=current_day)
        if primary == "pickup_rate":
            c_e, c_en = ctrl_series["connected"], ctrl_series["dials"]
            c_r, c_rn = ctrl_recent["connected"], ctrl_recent["dials"]
        else:
            c_e, c_en = ctrl_series["engaged"], ctrl_series["connected"]
            c_r, c_rn = ctrl_recent["engaged"], ctrl_recent["connected"]
        ctrl = stats.detect_drop(c_e, c_en, c_r, c_rn, min_n=settings.drift_min_connected,
                                 min_rel_drop=settings.decay_rel_drop, alpha=settings.alpha)
        if ctrl["drop"] and (ctrl.get("relative_change") or 0) <= (drop.get("relative_change") or 0) * 1.2:
            continue
        del state["promotions"][dim]
        block(state, dim, value, sim_minute, 3.0, "winner decayed")
        actions.append({"type": "demote", "dim": dim, "value": value,
                        "baseline": promo.get("baseline"), "evidence": {
                            **drop, "control": ctrl, "promoted_at": promo.get("at_minute"),
                            "recent": {"engaged": r, "connected": r_n},
                            "earlier": {"engaged": e, "connected": e_n}},
                        "at": sim_minute})
    return actions


# --------------------------------------------------------------------------- cohorts
def cohort_plan(wf: dict, state: dict, settings: Settings) -> list[dict]:
    """Build the ordered cohort list from confirmed feature wins.

    Two wins on different features get an intersection cohort first, so a
    lead that qualifies for both keeps both benefits.  A single cohort object
    is used everywhere: this ID is what detection, experiments and the
    supervisor report refer to.
    """
    wins = state.get("cohort_wins") or {}
    entries = []
    for dim, win in wins.items():
        where = win.get("where") or {}
        if not where:
            continue
        entries.append((dim, win, tuple(sorted(where))))
    if not entries:
        return []
    # One win per dimension is enough; they are already the strongest cells.
    entries = entries[:2]
    out: list[dict] = []
    if len(entries) == 2 and entries[0][2] != entries[1][2]:
        where = copy.deepcopy(entries[0][1]["where"])
        for feat, vals in entries[1][1]["where"].items():
            if feat in where:
                where[feat] = list(dict.fromkeys(where[feat] + vals))
            else:
                where[feat] = list(vals)
        out.append(_cohort_entry(where, entries, settings, combined=True))
    for dim, win, _ in entries:
        out.append(_cohort_entry(win["where"], [(dim, win)], settings, combined=False))
    # De-duplicate identical rule IDs (first wins) and respect the hard cap.
    seen: set[str] = set()
    final = []
    for c in out:
        if c["id"] in seen:
            continue
        seen.add(c["id"])
        final.append(c)
        if len(final) >= 8:
            break
    return final


def _cohort_entry(where: dict, wins: list[tuple[str, dict]], settings: Settings,
                  combined: bool) -> dict:
    allocations = {}
    for dim, win, *_ in wins:
        allocations[dim] = winner_allocation(
            list(win.get("options") or [win["value"], win["baseline"]]),
            win["value"], settings.control_share)
    if not allocations:
        # Fall back to a plain two-arm split if option lists were not stored.
        for dim, win, *_ in wins:
            allocations[dim] = {win["value"]: 1.0 - settings.control_share,
                                win["baseline"]: settings.control_share}
    return {"id": cohort_id_for(where), "where": where,
            "allocations": allocations, "combined": combined}
