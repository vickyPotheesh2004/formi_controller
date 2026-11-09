"""Command-line entry point for the controller.

    python -m controller run --url http://127.0.0.1:8700
    python -m controller decisions --state state --workflow ks_cancel
    python -m controller report --state state
    python -m controller metrics --state state
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from . import analytics, cohorts, db, stats
from .api import SimClient, SimError
from .config import Settings, default_settings
from .engine import Engine
from .ledger import DecisionLedger
from .report import collect, render_markdown, write_report

log = logging.getLogger("controller")

_LOGGER_FIELDS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


class JsonlFormatter(logging.Formatter):
    """One JSON object per line, so the loop's own logs are queryable too."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _LOGGER_FIELDS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, sort_keys=True)


def configure_logging(settings: Settings, quiet: bool = False) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    stream = logging.StreamHandler()
    stream.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    if not quiet:
        root.addHandler(stream)
    os.makedirs(settings.state_dir, exist_ok=True)
    file_handler = logging.FileHandler(settings.log_file(), encoding="utf-8")
    file_handler.setFormatter(JsonlFormatter())
    root.addHandler(file_handler)


def settings_from_args(args: argparse.Namespace) -> Settings:
    base = default_settings()
    changes: dict[str, Any] = {}
    if getattr(args, "state", None):
        changes["state_dir"] = args.state
    if getattr(args, "step", None):
        changes["step_minutes"] = int(args.step)
    if getattr(args, "db", None):
        changes["db_path"] = args.db
    if getattr(args, "log", None):
        changes["log_path"] = args.log
    return replace(base, **changes) if changes else base


def _open(args: argparse.Namespace) -> tuple[sqlite3.Connection, Settings]:
    settings = settings_from_args(args)
    conn = db.connect(settings.db_file())
    db.init_schema(conn)
    return conn, settings


# --------------------------------------------------------------------------- commands
def cmd_run(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)
    configure_logging(settings, quiet=args.quiet)
    client = SimClient(args.url, timeout=settings.request_timeout_s,
                       max_retries=settings.max_retries)
    engine = Engine(client, settings, run_id=args.run_id, scenario=args.scenario)
    try:
        engine.run()
    except SimError as e:
        if e.status == 409 and e.code == "finished":
            log.info("simulator already finished")
        else:
            log.exception("controller stopped on simulator error")
            return 2
    except KeyboardInterrupt:
        log.warning("interrupted; state is durable and the run can resume")
        return 130
    finally:
        try:
            engine._snapshot(final=True)
        except Exception:
            pass
        engine.close()
    return 0


def cmd_decisions(args: argparse.Namespace) -> int:
    conn, _ = _open(args)
    ledger = DecisionLedger(conn, "query", "query")
    rows = ledger.query(workflow_id=args.workflow, decision_type=args.type,
                        decision_status=args.status, limit=args.limit)
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True, default=str))
    else:
        for i, row in enumerate(rows):
            if i:
                print("\n" + "-" * 78 + "\n")
            print(ledger.explain(row["decision_id"]))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    conn, settings = _open(args)
    world = db.get_meta(conn, "world") or {}
    workflows = {}
    for row in conn.execute("SELECT workflow_id,public_json FROM workflows").fetchall():
        try:
            workflows[row["workflow_id"]] = json.loads(row["public_json"])
        except (TypeError, ValueError):
            continue
    if not workflows:
        print("no workflows stored yet; run the controller first", file=sys.stderr)
        return 1
    report = collect(conn, workflows, int(world.get("seen_minute") or 0),
                     run_id=world.get("run_id") or "unknown",
                     scenario=world.get("scenario") or "unknown", final=False,
                     state={"credits_remaining": db.get_meta(conn, "credits")})
    if args.out:
        write_report(conn, workflows, int(world.get("seen_minute") or 0), args.out,
                     run_id=world.get("run_id") or "unknown",
                     scenario=world.get("scenario") or "unknown", final=False,
                     state={"credits_remaining": db.get_meta(conn, "credits")})
        print(f"wrote {os.path.join(args.out, 'report.md')} and report.json")
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        print(render_markdown(report))
    return 0


def cmd_metrics(args: argparse.Namespace) -> int:
    conn, _ = _open(args)
    world = db.get_meta(conn, "world") or {}
    day = int(world.get("seen_minute") or 0) // 1440
    rows = []
    for row in conn.execute("SELECT workflow_id,public_json FROM workflows").fetchall():
        try:
            wf = json.loads(row["public_json"])
        except (TypeError, ValueError):
            continue
        wid = row["workflow_id"]
        total = analytics.workflow_metrics(conn, wid)
        today = analytics.workflow_metrics(conn, wid, day, day)
        rows.append({"workflow_id": wid, "primary": analytics.primary_kpi(wf),
                     "total": total, "today": today})
    payload = {
        "run_id": world.get("run_id"), "scenario": world.get("scenario"),
        "sim_minute": world.get("seen_minute"), "credits": db.get_meta(conn, "credits"),
        "ingestion": [dict(r) for r in conn.execute(
            "SELECT COALESCE(SUM(events),0) events,COALESCE(SUM(calls),0) calls,"
            "COALESCE(SUM(leads),0) leads,COALESCE(SUM(duplicate_events),0) duplicates,"
            "COALESCE(SUM(malformed),0) malformed,COUNT(*) batches FROM ingestion_metrics")],
        "workflows": rows,
    }
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0


def cmd_selftest(args: argparse.Namespace) -> int:
    """Fast invariant checks that do not need a simulator."""
    import tempfile
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(os.path.join(tmp, "self.db"))
        db.init_schema(conn)
        ledger = DecisionLedger(conn, "self", "self")
        did = ledger.record(
            decision_type="self_test", decision_status="observed", summary="self test",
            decision_statement="Exercise the append-only ledger.",
            source=[{"type": "self_test", "reference": "selftest"}],
            evidence={"ok": True}, sim_minute=0)
        try:
            conn.execute("UPDATE decisions SET summary='x' WHERE decision_id=?", (did,))
            failures.append("decision UPDATE was not blocked")
        except sqlite3.IntegrityError:
            pass
        try:
            conn.execute("DELETE FROM decisions WHERE decision_id=?", (did,))
            failures.append("decision DELETE was not blocked")
        except sqlite3.IntegrityError:
            pass
        if not ledger.get(did)["evidence"].get("ok"):
            failures.append("ledger round-trip failed")
        conn.close()

    good = stats.evaluate_candidate(100, 1000, 140, 1000, min_n=300, mde_rel=0.05,
                                    alpha=0.025)
    if not good["eligible"]:
        failures.append("statistics missed a large, sufficiently sampled lift")
    bad = stats.evaluate_candidate(100, 1000, 100, 1000, min_n=300, mde_rel=0.05,
                                   alpha=0.025)
    if bad["eligible"]:
        failures.append("statistics promoted an equal-rate candidate")

    wf = {"options": {"welcome_message": ["W1", "W2"], "voice": ["V1", "V2"],
                      "caller_id": ["D1"], "prompt_version": ["P1"]},
          "dialling_window": [9, 21]}
    cfg, problems = cohorts.normalise_config(wf, {
        "cohorts": [], "prompt_version": "P1", "schedule": {},
        "allocations": {"default": {"welcome_message": {"W1": 1}, "voice": {"V1": 1},
                                    "caller_id": {"D1": 1}}}})
    if problems:
        failures.append(f"valid config rejected: {problems}")
    if cohorts.changed_dims(cfg, cfg):
        failures.append("identical configs reported a change")

    if failures:
        for f in failures:
            print(f"FAIL: {f}", file=sys.stderr)
        return 1
    print("selftest OK")
    return 0


# --------------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="controller", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the controller against a simulator URL")
    run.add_argument("--url", required=True)
    run.add_argument("--state", default=None)
    run.add_argument("--step", type=int, default=None, help="simulated minutes per loop tick")
    run.add_argument("--db", default=None)
    run.add_argument("--log", default=None)
    run.add_argument("--scenario", default=None)
    run.add_argument("--run-id", default=None)
    run.add_argument("--quiet", action="store_true")
    run.set_defaults(func=cmd_run)

    dec = sub.add_parser("decisions", help="explain decisions from the ledger")
    dec.add_argument("--state", default=None)
    dec.add_argument("--db", default=None)
    dec.add_argument("--workflow", default=None)
    dec.add_argument("--type", default=None)
    dec.add_argument("--status", default=None)
    dec.add_argument("--limit", type=int, default=50)
    dec.add_argument("--json", action="store_true")
    dec.set_defaults(func=cmd_decisions)

    rep = sub.add_parser("report", help="render the supervisor report")
    rep.add_argument("--state", default=None)
    rep.add_argument("--db", default=None)
    rep.add_argument("--out", default=None)
    rep.add_argument("--json", action="store_true")
    rep.set_defaults(func=cmd_report)

    met = sub.add_parser("metrics", help="print JSON KPI metrics")
    met.add_argument("--state", default=None)
    met.add_argument("--db", default=None)
    met.set_defaults(func=cmd_metrics)

    st = sub.add_parser("selftest", help="run fast invariant checks")
    st.add_argument("--state", default=None)
    st.add_argument("--db", default=None)
    st.set_defaults(func=cmd_selftest)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
