"""Runtime configuration and decision thresholds.

Everything here is either a documented simulator contract fact (feature values,
base date) or a controller policy choice.  Thresholds are deliberately coarse
and conservative; they are explained in docs/DECISION_ENGINE.md.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# Simulator contract: D0 00:00 IST.  Used only to turn event timestamps into
# day/hour buckets; no scenario file or simulator internals are read.
SIM_TZ = timezone(timedelta(hours=5, minutes=30))
SIM_START = datetime(2027, 1, 4, 0, 0, tzinfo=SIM_TZ)

# Lead features exposed by `lead` events (docs/SIMULATOR.md).
FEATURES: dict[str, list[str]] = {
    "lang": ["hi", "en", "ta", "te", "kn", "mr", "bn"],
    "city_tier": ["1", "2", "3"],
    "age_band": ["18-30", "31-45", "46-60", "60+"],
    "value_band": ["low", "mid", "high"],
}
AGENT_DIMS = ["welcome_message", "voice", "caller_id"]
CHANGE_DIMS = ["cohorts", "welcome_message", "voice", "caller_id", "prompt_version", "schedule"]


@dataclass(frozen=True)
class Settings:
    state_dir: str = os.environ.get("CONTROLLER_STATE_DIR", "state")
    db_path: str = os.environ.get("CONTROLLER_DB", "")
    log_path: str = os.environ.get("CONTROLLER_LOG", "")
    step_minutes: int = int(os.environ.get("CONTROLLER_STEP_MINUTES", "60"))
    max_retries: int = int(os.environ.get("CONTROLLER_MAX_RETRIES", "6"))
    request_timeout_s: float = float(os.environ.get("CONTROLLER_TIMEOUT_S", "180"))

    # Evidence thresholds.  See docs/DECISION_ENGINE.md for the reasoning.
    min_arm_connected: int = int(os.environ.get("CONTROLLER_MIN_ARM_CONNECTED", "300"))
    min_arm_dials: int = int(os.environ.get("CONTROLLER_MIN_ARM_DIALS", "800"))
    min_cohort_arm_connected: int = int(os.environ.get("CONTROLLER_MIN_COHORT_ARM_CONNECTED", "120"))
    alpha: float = float(os.environ.get("CONTROLLER_ALPHA", "0.05"))
    promotion_alpha: float = float(os.environ.get("CONTROLLER_PROMOTION_ALPHA", "0.05"))
    mde_engagement: float = float(os.environ.get("CONTROLLER_MDE_ENGAGEMENT", "0.05"))
    mde_pickup: float = float(os.environ.get("CONTROLLER_MDE_PICKUP", "0.05"))
    min_observation_days: float = float(os.environ.get("CONTROLLER_MIN_OBSERVATION_DAYS", "1.0"))
    promotion_confirmations: int = int(os.environ.get("CONTROLLER_PROMOTION_CONFIRMATIONS", "2"))
    max_cohorts: int = int(os.environ.get("CONTROLLER_MAX_COHORTS", "8"))
    min_cohort_share: float = float(os.environ.get("CONTROLLER_MIN_COHORT_SHARE", "0.04"))
    exploration_share: float = float(os.environ.get("CONTROLLER_EXPLORATION_SHARE", "0.60"))
    control_share: float = float(os.environ.get("CONTROLLER_CONTROL_SHARE", "0.15"))

    # Incidents.
    caller_id_min_recent_dials: int = int(os.environ.get("CONTROLLER_DID_MIN_DIALS", "200"))
    caller_id_min_baseline_dials: int = int(os.environ.get("CONTROLLER_DID_MIN_BASE", "800"))
    caller_id_z: float = float(os.environ.get("CONTROLLER_DID_Z", "2.5"))
    caller_id_min_rel_drop: float = float(os.environ.get("CONTROLLER_DID_REL_DROP", "0.15"))
    hallucination_abs_threshold: float = float(os.environ.get("CONTROLLER_HALLUC_ABS", "0.02"))
    hallucination_min_connected: int = int(os.environ.get("CONTROLLER_HALLUC_MIN_CONNECTED", "150"))
    hallucination_z: float = float(os.environ.get("CONTROLLER_HALLUC_Z", "2.5"))
    drift_min_dials: int = int(os.environ.get("CONTROLLER_DRIFT_MIN_DIALS", "600"))
    drift_min_connected: int = int(os.environ.get("CONTROLLER_DRIFT_MIN_CONNECTED", "300"))
    drift_rel_drop: float = float(os.environ.get("CONTROLLER_DRIFT_REL_DROP", "0.10"))
    decay_rel_drop: float = float(os.environ.get("CONTROLLER_DECAY_REL_DROP", "0.10"))

    # Credits and approver fairness.
    credit_reserve: int = int(os.environ.get("CONTROLLER_CREDIT_RESERVE", "20"))
    validation_n: int = int(os.environ.get("CONTROLLER_VALIDATION_N", "300"))
    max_approvals_per_day_global: int = int(os.environ.get("CONTROLLER_MAX_APPROVALS_DAY", "6"))
    max_proposal_attempts: int = int(os.environ.get("CONTROLLER_MAX_PROPOSAL_ATTEMPTS", "2"))
    schedule_min_dials: int = int(os.environ.get("CONTROLLER_SCHEDULE_MIN_DIALS", "1500"))
    person_hour_max_dph: int = int(os.environ.get("CONTROLLER_PERSON_HOUR_MAX_DPH", "1500"))
    person_hour_max_entries: int = int(os.environ.get("CONTROLLER_PERSON_HOUR_MAX_ENTRIES", "20000"))
    eval_interval_minutes: int = int(os.environ.get("CONTROLLER_EVAL_INTERVAL", "120"))
    report_interval_minutes: int = int(os.environ.get("CONTROLLER_REPORT_INTERVAL", "360"))

    def db_file(self) -> str:
        return self.db_path or os.path.join(self.state_dir, "controller.db")

    def log_file(self) -> str:
        return self.log_path or os.path.join(self.state_dir, "controller.jsonl")


def default_settings() -> Settings:
    return Settings()
