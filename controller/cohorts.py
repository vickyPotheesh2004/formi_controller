"""Shared cohort objects, config validation and governance-safe diffs.

This module is intentionally the only place that knows how a workflow config
is shaped.  Detection, experiments and the supervisor report all use the same
cohort IDs and the same matching rule: AND across features, OR inside a
feature's value list, first matching cohort wins.
"""
from __future__ import annotations

import copy
import math
from typing import Any

from .config import AGENT_DIMS, CHANGE_DIMS, FEATURES


def _dist(cfg: dict, cohort: str, dim: str) -> dict:
    return (cfg["allocations"].get(cohort) or {}).get(dim) or cfg["allocations"]["default"][dim]


def normalise_config(wf: dict, cfg: dict) -> tuple[dict, list[str]]:
    """Validate and normalise a config against the documented hard rules.

    Mirrors the simulator contract from docs/SIMULATOR.md.  The simulator is
    still the authority; this local check exists so the controller does not
    waste approval requests on configs it can already see are invalid.
    """
    problems: list[str] = []
    if not isinstance(cfg, dict):
        return {}, ["config must be an object"]
    extra = set(cfg) - {"cohorts", "allocations", "prompt_version", "schedule"}
    if extra:
        problems.append(f"unknown keys: {sorted(extra)}")
    out: dict = {"cohorts": [], "allocations": {}, "prompt_version": None,
                 "schedule": {"hour_weights": {}, "lead_preferred_hour": {}}}
    cohort_ids: list[str] = []
    for i, c in enumerate(cfg.get("cohorts") or []):
        cid = c.get("id") if isinstance(c, dict) else None
        if not isinstance(cid, str) or not cid or cid == "default" or cid in cohort_ids:
            problems.append(f"cohorts[{i}].id must be a unique non-empty string other than 'default'")
            continue
        where = c.get("where") or {}
        for feat, vals in where.items():
            if feat not in FEATURES:
                problems.append(f"cohort {cid}: unknown feature {feat}")
            elif not isinstance(vals, list) or not vals or any(v not in FEATURES[feat] for v in vals):
                problems.append(f"cohort {cid}: bad values for {feat}: {vals}")
        cohort_ids.append(cid)
        out["cohorts"].append({"id": cid, "where": {k: list(v) for k, v in where.items()}})
    if len(cohort_ids) > 20:
        problems.append("at most 20 cohorts")
    allocs = cfg.get("allocations") or {}
    if "default" not in allocs:
        problems.append("allocations.default is required")
    options = wf.get("options") or {}
    for key, dims in allocs.items():
        if key != "default" and key not in cohort_ids:
            problems.append(f"allocations.{key}: not a defined cohort")
            continue
        out["allocations"][key] = {}
        for dim, weights in (dims or {}).items():
            if dim not in AGENT_DIMS:
                problems.append(f"allocations.{key}.{dim}: unknown dimension")
                continue
            opts = options.get(dim, [])
            if not isinstance(weights, dict) or not weights:
                problems.append(f"allocations.{key}.{dim}: must be a non-empty {{option: weight}} map")
                continue
            bad = [o for o in weights if o not in opts]
            if bad:
                problems.append(f"allocations.{key}.{dim}: options not allowed for this workflow: {bad}")
                continue
            if any((not isinstance(w, (int, float))) or w < 0 or math.isnan(w) for w in weights.values()):
                problems.append(f"allocations.{key}.{dim}: weights must be >= 0")
                continue
            s = float(sum(weights.values()))
            if s <= 0:
                problems.append(f"allocations.{key}.{dim}: weights sum to 0")
                continue
            out["allocations"][key][dim] = {o: float(w) / s for o, w in weights.items() if w > 0}
        if key == "default" and "default" in out["allocations"]:
            missing = [d for d in AGENT_DIMS if d not in out["allocations"]["default"]]
            if missing:
                problems.append(f"allocations.default missing dims {missing}")
    pv = cfg.get("prompt_version")
    if pv not in options.get("prompt_version", []):
        problems.append(f"prompt_version {pv!r} not allowed")
    out["prompt_version"] = pv
    sched = cfg.get("schedule") or {}
    lo, hi = (wf.get("dialling_window") or [0, 24])
    for key, hw in (sched.get("hour_weights") or {}).items():
        if key != "default" and key not in cohort_ids:
            problems.append(f"schedule.hour_weights.{key}: not a defined cohort")
            continue
        clean: dict[str, float] = {}
        for h, w in (hw or {}).items():
            try:
                hh = int(h)
            except (TypeError, ValueError):
                problems.append(f"schedule.hour_weights.{key}: bad hour {h!r}")
                continue
            if not lo <= hh < hi:
                problems.append(f"schedule.hour_weights.{key}: hour {hh} outside [{lo},{hi})")
                continue
            if not isinstance(w, (int, float)) or w < 0:
                problems.append(f"schedule.hour_weights.{key}.{h}: weight must be >= 0")
                continue
            clean[str(hh)] = float(w)
        out["schedule"]["hour_weights"][key] = clean
    lph = sched.get("lead_preferred_hour") or {}
    if len(lph) > 200_000:
        problems.append("schedule.lead_preferred_hour: at most 200k entries")
    for pid, h in lph.items():
        if not isinstance(h, int) or not lo <= h < hi:
            problems.append(f"schedule.lead_preferred_hour[{pid}]: hour {h!r} outside window")
            break
    out["schedule"]["lead_preferred_hour"] = {str(k): int(v) for k, v in lph.items() if isinstance(v, int)}
    return out, problems


def changed_dims(old: dict, new: dict) -> list[str]:
    ch: list[str] = []
    if old.get("cohorts") != new.get("cohorts"):
        ch.append("cohorts")
    keys = set(old.get("allocations", {})) | set(new.get("allocations", {}))
    for dim in AGENT_DIMS:
        if any(_dist(old, k, dim) != _dist(new, k, dim) for k in keys):
            ch.append(dim)
    if old.get("prompt_version") != new.get("prompt_version"):
        ch.append("prompt_version")
    if old.get("schedule") != new.get("schedule"):
        ch.append("schedule")
    return [d for d in CHANGE_DIMS if d in ch]


def max_shift(old: dict, new: dict) -> float:
    keys = ({"default"} | {c["id"] for c in old.get("cohorts", [])}
            | {c["id"] for c in new.get("cohorts", [])})
    worst = 0.0
    for k in keys:
        for dim in AGENT_DIMS:
            try:
                a, b = _dist(old, k, dim), _dist(new, k, dim)
            except KeyError:
                continue
            tv = 0.5 * sum(abs(a.get(o, 0) - b.get(o, 0)) for o in set(a) | set(b))
            worst = max(worst, tv)
    return worst


def auto_apply_check(wf: dict, old: dict, new: dict) -> dict:
    gov = (wf.get("governance") or {}).get("auto_apply") or {}
    dims = changed_dims(old, new)
    shift = max_shift(old, new)
    reasons: list[str] = []
    if not gov.get("enabled"):
        reasons.append("auto-apply disabled for this workflow")
    bad = [d for d in dims if d not in gov.get("allowed_dims", [])]
    if bad:
        reasons.append(f"dimensions not permitted: {bad}")
    if shift > gov.get("max_shift", 0) + 1e-9:
        reasons.append(f"allocation shift {shift:.3f} exceeds max_shift {gov.get('max_shift', 0)}")
    if not dims:
        reasons.append("no change")
    return {"allowed": not reasons, "reasons": reasons, "dims": dims, "shift": shift,
            "policy": gov}


def match_features(where: dict, features: dict) -> bool:
    for feat, allowed in (where or {}).items():
        if features.get(feat) not in allowed:
            return False
    return True


def cohort_of(cfg: dict, features: dict) -> str:
    for c in cfg.get("cohorts") or []:
        if match_features(c.get("where") or {}, features):
            return c["id"]
    return "default"


def cohort_id_for(where: dict) -> str:
    parts = []
    for feat in sorted(where):
        vals = where[feat]
        token = "_".join(str(v).replace("+", "plus").replace("-", "to") for v in vals)
        parts.append(f"{feat}_{token}")
    return "__".join(parts) or "default"


def coverage(order: list[dict], where: dict) -> float:
    """Rough share of a cohort that would be intercepted by earlier cohorts.

    Used to avoid adding a cohort that is already fully covered by a more
    specific rule; exact population math lives in analytics.
    """
    for c in order:
        other = c.get("where") or {}
        # If every feature/value pair in `other` is compatible with `where`,
        # the earlier cohort fully contains this one.
        if all(set(other.get(f, [])) <= set(where.get(f, [])) for f in other):
            return 1.0
    return 0.0


def validate_cohort_set(cohorts: list[dict]) -> list[str]:
    problems: list[str] = []
    if len(cohorts) > 20:
        problems.append("more than 20 cohorts")
    seen: set[str] = set()
    for c in cohorts:
        cid = c.get("id")
        if not cid or cid == "default" or cid in seen:
            problems.append(f"duplicate/invalid cohort id {cid!r}")
        seen.add(cid)
        where = c.get("where") or {}
        if not where:
            problems.append(f"cohort {cid} has an empty where clause")
        for feat, vals in where.items():
            if feat not in FEATURES:
                problems.append(f"cohort {cid} unknown feature {feat}")
            elif not isinstance(vals, list) or not vals or any(v not in FEATURES[feat] for v in vals):
                problems.append(f"cohort {cid} bad values {feat}={vals}")
    return problems


def bounded_hour_weights(rates: dict[int, tuple[int, int]], window: list[int],
                         min_weight: float = 0.3, max_weight: float = 3.0) -> dict[str, float]:
    """Turn observed pickup rates into safe hour weights.

    Every hour in the business dialling window is present, so a table can
    never accidentally leave an hour at weight 0.  A Beta prior shrinks thin
    hours toward the workflow mean; extreme weights are clipped.
    """
    lo, hi = window
    hours = list(range(lo, hi))
    observed = {h: rates.get(h) for h in hours}
    total_n = sum(n for _, n in observed.values() if n)
    total_k = sum(k for k, n in observed.values() if n)
    prior_mean = (total_k + 10.0) / (total_n + 20.0) if total_n else 0.3
    alpha = max(2.0, prior_mean * 20.0)
    beta = max(2.0, (1.0 - prior_mean) * 20.0)
    smoothed = {}
    for h in hours:
        k, n = observed.get(h) or (0, 0)
        smoothed[h] = (k + alpha) / (n + alpha + beta)
    mean = sum(smoothed.values()) / len(smoothed) if smoothed else 1.0
    out = {}
    for h in hours:
        w = smoothed[h] / mean if mean > 0 else 1.0
        out[str(h)] = round(max(min_weight, min(max_weight, w)), 4)
    return out


def split_allocations(options: list[str], baseline: str, exploration: float,
                      control: float) -> dict[str, float]:
    """Exploration design with a permanent control arm.

    ``exploration`` is the total mass outside control; the rest goes to the
    baseline option.  This keeps a reference arm for guardrail and decay
    checks even after a winner is promoted.
    """
    opts = list(options)
    if baseline not in opts or len(opts) < 2:
        return {baseline: 1.0} if baseline in opts else {}
    challengers = [o for o in opts if o != baseline]
    control = max(0.0, min(0.5, control))
    explore = max(0.0, min(1.0 - control, exploration))
    out = {baseline: 1.0 - explore}
    per = explore / len(challengers) if challengers else 0.0
    for o in challengers:
        out[o] = per
    return {k: v for k, v in out.items() if v > 1e-9}


def winner_allocation(options: list[str], winner: str, control: float = 0.15) -> dict[str, float]:
    opts = list(options)
    if winner not in opts:
        return {}
    out = {o: 0.0 for o in opts}
    out[winner] = 1.0 - control
    # Control is spread over every non-winner option so decay in the winner
    # can be compared against the pre-promotion field, not a single option.
    others = [o for o in opts if o != winner]
    if others and control > 0:
        for o in others:
            out[o] = control / len(others)
    else:
        out[winner] = 1.0
    return {k: v for k, v in out.items() if v > 1e-9}


def diff_configs(old: dict, new: dict) -> dict:
    """Human- and machine-readable diff for an approval request."""
    out: dict[str, Any] = {}
    if old.get("cohorts") != new.get("cohorts"):
        out["cohorts"] = {"before": old.get("cohorts", []), "after": new.get("cohorts", [])}
    for dim in AGENT_DIMS:
        keys = sorted(set(old.get("allocations", {})) | set(new.get("allocations", {})))
        changes = {}
        for k in keys:
            try:
                a, b = _dist(old, k, dim), _dist(new, k, dim)
            except KeyError:
                continue
            if a != b:
                changes[k] = {"before": a, "after": b}
        if changes:
            out[f"allocations.{dim}"] = changes
    if old.get("prompt_version") != new.get("prompt_version"):
        out["prompt_version"] = {"before": old.get("prompt_version"),
                                 "after": new.get("prompt_version")}
    if old.get("schedule") != new.get("schedule"):
        out["schedule"] = {"before": old.get("schedule", {}), "after": new.get("schedule", {})}
    return out


def merge_config(base: dict, proposed: dict) -> dict:
    """Deep-merge allocations; replace agent dimensions wholesale (contract)."""
    dst = copy.deepcopy(base)
    for k, v in proposed.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict) and k not in AGENT_DIMS:
            dst[k] = merge_config(dst[k], v) if k == "allocations" else {**dst[k], **copy.deepcopy(v)}
        else:
            dst[k] = copy.deepcopy(v)
    return dst
