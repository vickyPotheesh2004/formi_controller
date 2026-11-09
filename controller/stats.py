"""Deterministic proportion statistics.

The controller never asks an LLM whether a variant won.  Every promotion or
rollback decision goes through the functions here.  The method is a two-arm
proportion test with an unpooled confidence interval for the difference,
Bonferroni-adjusted alpha for multiple comparisons, a minimum practical
effect, and explicit sample-size gates.

Limitations are documented in docs/DECISION_ENGINE.md: this is a fixed-horizon
frequentist test, not a sequential test, so the controller additionally
requires repeated confirmation across evaluations and never peeks at one small
sample.
"""
from __future__ import annotations

import math
from statistics import NormalDist

_N = NormalDist()


def normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def two_sided_p(z: float) -> float:
    return max(0.0, min(1.0, 2.0 * (1.0 - normal_cdf(abs(z)))))


def z_for(alpha: float) -> float:
    return _N.inv_cdf(1.0 - alpha / 2.0)


def wilson_interval(k: int, n: int, alpha: float = 0.05) -> tuple[float, float] | None:
    if n <= 0:
        return None
    z = z_for(alpha)
    p = k / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def bonferroni(alpha: float, tests: int) -> float:
    return alpha / max(1, int(tests))


def required_n(p: float, mde_abs: float, alpha: float = 0.05, power: float = 0.8) -> int | None:
    """Equal-arm sample size per arm for a two-sided proportion test."""
    if not 0.0 < p < 1.0 or mde_abs <= 0:
        return None
    z_a = z_for(alpha)
    z_b = _N.inv_cdf(power)
    n = 2.0 * p * (1.0 - p) * (z_a + z_b) ** 2 / (mde_abs ** 2)
    return int(math.ceil(n))


def proportion_diff(k1: int, n1: int, k2: int, n2: int, alpha: float = 0.05) -> dict:
    """Difference p2 - p1 with an unpooled normal CI.

    k1/n1 is the reference arm (baseline, older window, or control cohort).
    """
    if n1 <= 0 or n2 <= 0:
        return {"p1": None, "p2": None, "diff": None, "ci_low": None, "ci_high": None,
                "z": None, "p_value": None, "se": None}
    p1, p2 = k1 / n1, k2 / n2
    diff = p2 - p1
    se = math.sqrt(max(p1 * (1 - p1) / n1 + p2 * (1 - p2) / n2, 1e-18))
    z = diff / se
    half = z_for(alpha) * se
    return {"p1": p1, "p2": p2, "diff": diff, "ci_low": diff - half,
            "ci_high": diff + half, "z": z, "p_value": two_sided_p(z), "se": se}


def evaluate_candidate(base_k: int, base_n: int, cand_k: int, cand_n: int,
                       *, min_n: int, mde_rel: float, alpha: float) -> dict:
    """Evaluate a candidate against a reference arm.

    A promotion needs all of:
      * both arms have at least ``min_n`` observations;
      * the lower end of the CI for the difference is above the practical
        threshold (``base_rate * mde_rel``);
      * the two-sided p-value is below the (already adjusted) alpha;
      * the point estimate is positive.
    """
    test = proportion_diff(base_k, base_n, cand_k, cand_n, alpha)
    p1 = test["p1"]
    sufficient = (base_n >= min_n and cand_n >= min_n and p1 is not None and p1 > 0)
    practical_threshold = (p1 * mde_rel) if p1 is not None else None
    ci_low = test["ci_low"]
    practical = bool(ci_low is not None and practical_threshold is not None and ci_low > practical_threshold)
    significant = bool(test["p_value"] is not None and test["p_value"] < alpha)
    positive = bool(test["diff"] is not None and test["diff"] > 0)
    relative = (test["diff"] / p1) if p1 else None
    eligible = bool(sufficient and practical and significant and positive)
    if not sufficient:
        reason = "insufficient_sample"
    elif not positive:
        reason = "candidate_not_better"
    elif not significant:
        reason = "statistically_uncertain"
    elif not practical:
        reason = "below_minimum_practical_effect"
    else:
        reason = "eligible"
    return {
        **test,
        "base_n": base_n, "cand_n": cand_n,
        "base_k": base_k, "cand_k": cand_k,
        "relative_lift": relative,
        "practical_threshold_abs": practical_threshold,
        "min_n": min_n,
        "mde_rel": mde_rel,
        "alpha": alpha,
        "sufficient": sufficient,
        "practical": practical,
        "significant": significant,
        "positive": positive,
        "eligible": eligible,
        "reason": reason,
    }


def detect_drop(base_k: int, base_n: int, recent_k: int, recent_n: int, *,
                min_n: int, min_rel_drop: float, alpha: float) -> dict:
    """Detect a statistically meaningful fall from a reference window."""
    test = proportion_diff(base_k, base_n, recent_k, recent_n, alpha)
    p1 = test["p1"]
    sufficient = base_n >= min_n and recent_n >= min_n and p1 is not None and p1 > 0
    rel = ((test["p2"] - p1) / p1) if p1 else None
    significant = bool(test["p_value"] is not None and test["p_value"] < alpha)
    drop = bool(sufficient and rel is not None and rel <= -abs(min_rel_drop) and significant)
    return {
        **test,
        "relative_change": rel,
        "sufficient": sufficient,
        "significant": significant,
        "drop": drop,
        "min_n": min_n,
        "min_rel_drop": min_rel_drop,
        "alpha": alpha,
    }


def guardrail_regressed(base_k: int, base_n: int, cand_k: int, cand_n: int, *,
                        max_abs_drop: float, alpha: float, min_n: int) -> dict:
    """Guardrail = a KPI that must not fall by more than ``max_abs_drop``.

    We only call a regression when the upper end of the candidate-minus-base
    CI is below ``-max_abs_drop`` and the test is significant.  This avoids
    rejecting a good change because of noise.
    """
    test = proportion_diff(base_k, base_n, cand_k, cand_n, alpha)
    sufficient = base_n >= min_n and cand_n >= min_n
    regressed = bool(
        sufficient
        and test["ci_high"] is not None
        and test["ci_high"] < -abs(max_abs_drop)
        and test["p_value"] is not None
        and test["p_value"] < alpha
    )
    return {
        **test,
        "sufficient": sufficient,
        "regressed": regressed,
        "max_abs_drop": max_abs_drop,
        "alpha": alpha,
        "min_n": min_n,
    }


def smoothed_rate(k: int, n: int, alpha_prior: float = 10.0, beta_prior: float = 10.0) -> float:
    return (k + alpha_prior) / (n + alpha_prior + beta_prior)
