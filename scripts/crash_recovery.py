"""Crash-recovery evidence: kill the controller mid-run, restart, verify.

Usage:
    python scripts/crash_recovery.py [--scenario scenarios/public_02_incidents.yaml]
                                     [--port 8711] [--kill-after 20]

The script starts its own simulator, kills the controller process without
cleanup after a few seconds, restarts it on the same state directory, waits for
the scenario to finish, and writes reports/crash_recovery.md with the checks.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def env() -> dict[str, str]:
    out = dict(os.environ)
    out["PYTHONPATH"] = str(ROOT)
    out["PYTHONUTF8"] = "1"
    return out


def http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def wait_ready(url: str, timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if http_ok(url + "/health"):
            return
        time.sleep(0.5)
    raise RuntimeError(f"simulator did not become ready at {url}")


def spawn(args: list[str], log: Path) -> subprocess.Popen:
    handle = open(log, "w", encoding="utf-8")
    return subprocess.Popen(args, cwd=ROOT, env=env(), stdout=handle,
                            stderr=subprocess.STDOUT, text=True)


def world(db: Path) -> dict:
    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='world'").fetchone()
        return json.loads(row[0]) if row else {}
    finally:
        conn.close()


def checks(db: Path) -> dict:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        q = lambda sql: conn.execute(sql).fetchall()
        return {
            "decisions": q("SELECT COUNT(*) n FROM decisions")[0]["n"],
            "applied_configs": q(
                "SELECT COUNT(*) n FROM config_versions WHERE proposal_id IS NOT NULL")[0]["n"],
            "duplicate_applies": [dict(r) for r in q(
                "SELECT proposal_id,COUNT(*) n FROM config_versions "
                "WHERE proposal_id IS NOT NULL GROUP BY proposal_id HAVING n>1")],
            "duplicate_approval_requests": [dict(r) for r in q(
                "SELECT proposal_id,COUNT(*) n FROM approvals "
                "GROUP BY proposal_id HAVING n>1")],
            "stuck_submitting": q(
                "SELECT COUNT(*) n FROM proposals WHERE status='submitting'")[0]["n"],
            "malformed_events": q(
                "SELECT COALESCE(SUM(malformed),0) n FROM ingestion_metrics")[0]["n"],
        }
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="scenarios/public_02_incidents.yaml")
    ap.add_argument("--port", type=int, default=8711)
    ap.add_argument("--kill-after", type=float, default=20.0)
    ap.add_argument("--run-dir", default="runs/crash_recovery")
    ap.add_argument("--state", default="state/crash_recovery")
    ap.add_argument("--out", default="reports/crash_recovery.md")
    args = ap.parse_args()

    scenario = (ROOT / args.scenario).resolve()
    run_dir = ROOT / args.run_dir
    state = ROOT / args.state
    out = ROOT / args.out
    url = f"http://127.0.0.1:{args.port}"
    for path in (run_dir, state):
        shutil.rmtree(path, ignore_errors=True)
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    out.parent.mkdir(parents=True, exist_ok=True)

    if http_ok(url + "/health"):
        print(f"port {args.port} is already in use", file=sys.stderr)
        return 1

    sim = spawn([sys.executable, "-m", "sim", "serve", "--scenario", str(scenario),
                 "--run-dir", str(run_dir), "--port", str(args.port)], state / "sim.log")
    lines = [f"# Crash-recovery evidence\n",
             f"- scenario: `{args.scenario}`",
             f"- simulator: `{run_dir}`",
             f"- controller state: `{state}`\n"]
    try:
        wait_ready(url, 120)
        db = state / "controller.db"
        first = spawn([sys.executable, "-m", "controller", "run", "--url", url,
                       "--state", str(state), "--quiet"], state / "controller-first.log")
        deadline = time.time() + args.kill_after
        while time.time() < deadline and first.poll() is None:
            time.sleep(0.25)
        if first.poll() is not None:
            raise RuntimeError("controller finished before the crash point; lower the scenario size")
        before = world(db)
        end = int(before.get("end_minute") or 0)
        seen = int(before.get("seen_minute") or 0)
        if end and seen >= end:
            raise RuntimeError("controller reached the end before the crash point")
        first.kill()
        first.wait(timeout=30)
        lines.append(f"- crashed at sim minute {seen} of {end}")
        time.sleep(1.0)

        second = spawn([sys.executable, "-m", "controller", "run", "--url", url,
                        "--state", str(state), "--quiet"], state / "controller-second.log")
        rc = second.wait(timeout=1800)
        after = world(db)
        result = checks(db)
        lines += [
            f"- restart exit code: {rc}",
            f"- final sim minute: {after.get('seen_minute')} of {after.get('end_minute')}",
            f"- decisions recorded: {result['decisions']}",
            f"- controller-applied config versions: {result['applied_configs']}",
            f"- duplicate applies (same proposal id): {len(result['duplicate_applies'])}",
            f"- duplicate approval requests (same proposal id): "
            f"{len(result['duplicate_approval_requests'])}",
            f"- proposals stuck in `submitting`: {result['stuck_submitting']}",
            f"- malformed ingested events: {result['malformed_events']}\n",
        ]
        ok = (rc == 0 and after.get("seen_minute") == after.get("end_minute")
              and not result["duplicate_applies"]
              and not result["duplicate_approval_requests"]
              and result["stuck_submitting"] == 0
              and result["malformed_events"] == 0)
        lines.append("**RESULT: PASS**" if ok else "**RESULT: FAIL**")
        print("\n".join(lines))
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return 0 if ok else 1
    finally:
        sim.terminate()
        try:
            sim.wait(timeout=10)
        except subprocess.TimeoutExpired:
            sim.kill()


if __name__ == "__main__":
    raise SystemExit(main())
