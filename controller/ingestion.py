"""Idempotent, chunk-transactional event ingestion.

A chunk is parsed into bounded in-memory counters and applied in one SQLite
transaction together with its chunk checkpoint.  A crash therefore either
commits the whole chunk or none of it; replaying a committed chunk is skipped.
Counters, not raw calls, are persisted, which keeps the database bounded at the
load-test volume while preserving every metric the controller needs.
"""
from __future__ import annotations

import json
import logging
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any

from . import db
from .api import SimClient
from .config import SIM_START, SIM_TZ, Settings

log = logging.getLogger("controller.ingest")


def day_hour(ts: str | None, fallback_minute: int = 0) -> tuple[int, int]:
    if ts:
        try:
            t = datetime.fromisoformat(ts)
            if t.tzinfo is None:
                t = t.replace(tzinfo=SIM_TZ)
            minutes = int((t - SIM_START).total_seconds() // 60)
            return minutes // 1440, minutes % 1440 // 60
        except ValueError:
            pass
    return fallback_minute // 1440, (fallback_minute % 1440) // 60


class Bucket(dict):
    """A tiny mutable counter row; keys are created on first use."""

    def add(self, column: str, value: float = 1.0) -> None:
        self[column] = self.get(column, 0.0) + value

    def max(self, column: str, value: int) -> None:
        self[column] = max(self.get(column, 0), value)


class Aggregator:
    def __init__(self) -> None:
        self.variant: dict[tuple, Bucket] = {}
        self.feature_variant: dict[tuple, Bucket] = {}
        self.hour_feature: dict[tuple, Bucket] = {}
        self.did: dict[tuple, Bucket] = {}
        self.prompt: dict[tuple, Bucket] = {}
        self.cohort_hour: dict[tuple, Bucket] = {}
        self.concluded: dict[tuple, Bucket] = {}
        self.person_hour: dict[tuple, Bucket] = {}
        self.lead_daily: dict[tuple, Bucket] = {}
        self.campaigns: dict[str, dict] = {}
        self.config_events: dict[tuple[str, int], dict] = {}
        self.approval_events: dict[str, dict] = {}
        self.leads: list[tuple] = []

    def _b(self, table: dict, key: tuple) -> Bucket:
        b = table.get(key)
        if b is None:
            b = table[key] = Bucket()
        return b

    def add_call(self, ev: dict, features: dict, dph: int, settings: Settings) -> None:
        wf = ev.get("workflow_id") or ""
        cfg = int(ev.get("config_version") or 0)
        variant = ev.get("variant") or {}
        wm = variant.get("welcome_message") or ""
        voice = variant.get("voice") or ""
        cid = variant.get("caller_id") or ""
        prompt = variant.get("prompt_version") or ""
        coh = ev.get("cohort_id") or "default"
        day, hour = day_hour(ev.get("ts"))
        connected = 1 if ev.get("connected") else 0
        engaged = 1 if ev.get("engaged") else 0
        qa = ev.get("qa") or {}
        halluc = 1 if qa.get("hallucination_flag") else 0
        flow = qa.get("flow_adherence")
        flow_sum = float(flow) if isinstance(flow, (int, float)) else 0.0
        flow_n = 1 if isinstance(flow, (int, float)) else 0
        interested = 1 if ev.get("disposition") == "concluded_interested" else 0

        v = self._b(self.variant, (wf, day, hour, coh, cfg, wm, voice, cid, prompt))
        v.add("dials")
        v.add("connected", connected)
        v.add("engaged", engaged)
        v.add("interested", interested)
        v.add("halluc", halluc)
        v.add("flow_sum", flow_sum)
        v.add("flow_n", flow_n)

        for feature, fvalue in features.items():
            for dim, value in (("welcome_message", wm), ("voice", voice)):
                b = self._b(self.feature_variant,
                            (wf, day, cfg, feature, str(fvalue), dim, value))
                b.add("dials")
                b.add("connected", connected)
                b.add("engaged", engaged)
                b.add("halluc", halluc)
            h = self._b(self.hour_feature, (wf, day, feature, str(fvalue), hour))
            h.add("dials")
            h.add("connected", connected)
            h.add("engaged", engaged)

        d = self._b(self.did, (wf, day, cid))
        d.add("dials")
        d.add("connected", connected)
        d.add("engaged", engaged)
        d.add("halluc", halluc)

        p = self._b(self.prompt, (wf, day, prompt))
        p.add("dials")
        p.add("connected", connected)
        p.add("engaged", engaged)
        p.add("halluc", halluc)

        c = self._b(self.cohort_hour, (wf, coh, day, hour))
        c.add("dials")
        c.add("connected", connected)
        c.add("engaged", engaged)

        if engaged:
            self._b(self.concluded, (wf, day)).add("concluded")

        if dph <= settings.person_hour_max_dph:
            person = ev.get("person_id") or ""
            if person:
                ph = self._b(self.person_hour, (wf, person, hour))
                ph.add("dials")
                ph.add("connected", connected)
                ph.add("engaged", engaged)
                ph.max("last_minute", int((day * 1440) + hour * 60))


class Ingestor:
    def __init__(self, conn, client: SimClient, settings: Settings, run_id: str,
                 workflows: dict[str, dict]):
        self.conn = conn
        self.client = client
        self.settings = settings
        self.run_id = run_id
        self.workflows = workflows
        self.lead_cache: OrderedDict[str, dict] = OrderedDict()
        self.cache_limit = 300_000
        self.total_events = 0
        self.total_calls = 0
        self.total_leads = 0
        self.total_duplicates = 0
        self.total_malformed = 0

    # -- public --------------------------------------------------------------
    def last_chunk(self) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(chunk), -1) AS m FROM chunks").fetchone()
        return int(row["m"])

    def ingest_pending(self) -> dict:
        start = time.time()
        stats = {"chunks": 0, "events": 0, "calls": 0, "leads": 0,
                 "duplicates": 0, "malformed": 0}
        last = self.last_chunk()
        for kind in ("history", "events"):
            try:
                chunks = self.client.list_chunks(kind=kind, after=last)
            except Exception as e:
                log.warning("chunk listing failed", extra={"kind": kind, "error": str(e)})
                raise
            for meta in sorted(chunks, key=lambda c: c["chunk"]):
                if meta["chunk"] <= last:
                    continue
                s = self.ingest_chunk(meta)
                for k in ("events", "calls", "leads", "duplicates", "malformed"):
                    stats[k] += s[k]
                stats["chunks"] += 1
                last = meta["chunk"]
        elapsed = (time.time() - start) * 1000
        if stats["chunks"]:
            with db.transaction(self.conn):
                self.conn.execute(
                    "INSERT INTO ingestion_metrics(sim_minute,events,calls,leads,duplicate_events,"
                    "malformed,duration_ms,chunks) VALUES(?,?,?,?,?,?,?,?)",
                    (None, stats["events"], stats["calls"], stats["leads"],
                     stats["duplicates"], stats["malformed"], elapsed, stats["chunks"]))
            log.info("ingested", extra={**stats, "duration_ms": round(elapsed, 1)})
        return stats

    # -- chunk ---------------------------------------------------------------
    def ingest_chunk(self, meta: dict) -> dict:
        n = int(meta["chunk"])
        if self.conn.execute("SELECT 1 FROM chunks WHERE chunk=?", (n,)).fetchone():
            return {"events": 0, "calls": 0, "leads": 0, "duplicates": 0, "malformed": 0}
        acc = Aggregator()
        errors: list[tuple] = []
        pending_calls: list[tuple[dict, dict]] = []
        seen: set[int] = set()
        calls = leads = duplicates = malformed = events = 0
        line_no = 0
        for ev in self.client.iter_chunk(n):
            line_no += 1
            if ev.get("__malformed__"):
                malformed += 1
                errors.append((n, line_no, ev.get("error", "malformed"), ev.get("raw_head")))
                continue
            events += 1
            eid = ev.get("event_id")
            if isinstance(eid, int):
                if eid in seen:
                    duplicates += 1
                    continue
                seen.add(eid)
            typ = ev.get("type")
            if typ == "lead":
                features = ev.get("features") or {}
                wf = ev.get("workflow_id") or ""
                lid = ev.get("lead_id") or ""
                if not lid:
                    malformed += 1
                    errors.append((n, line_no, "lead without lead_id", json.dumps(ev)[:200]))
                    continue
                self._cache_lead(lid, features)
                acc.leads.append((lid, wf, ev.get("campaign_id") or "", ev.get("person_id") or "",
                                  str(features.get("lang")), str(features.get("city_tier")),
                                  str(features.get("age_band")), str(features.get("value_band"))))
                b = acc._b(acc.lead_daily, (wf, day_hour(ev.get("ts"))[0],
                                            str(features.get("lang")), str(features.get("city_tier")),
                                            str(features.get("age_band")), str(features.get("value_band"))))
                b.add("uploaded")
                leads += 1
            elif typ == "call":
                calls += 1
                lid = ev.get("lead_id") or ""
                features = self.lead_cache.get(lid)
                if features is None:
                    pending_calls.append((ev, {}))
                else:
                    self.lead_cache.move_to_end(lid)
                    acc.add_call(ev, features, self.workflows.get(ev.get("workflow_id"), {}).get("dials_per_hour", 0),
                                 self.settings)
            elif typ == "campaign_created":
                cid = ev.get("campaign_id") or ""
                day, _ = day_hour(ev.get("ts"))
                missing = int(ev.get("leads") or 0) - int((ev.get("leads") or 0) > 0)
                acc.campaigns[cid] = {
                    "workflow_id": ev.get("workflow_id") or "",
                    "leads": int(ev.get("leads") or 0),
                    "created_minute": day * 1440,
                    "created_day": day,
                    "config_version_at_creation": int(ev.get("config_version_at_creation") or 0),
                }
            elif typ == "config_changed":
                key = (ev.get("workflow_id") or "", int(ev.get("version") or 0))
                acc.config_events[key] = {
                    "actor": ev.get("actor") or "",
                    "changed_dims": ev.get("changed_dims") or [],
                    "ts": ev.get("ts"),
                    "proposal_id": ev.get("proposal_id"),
                    "approval_id": ev.get("approval_id"),
                }
            elif typ == "approval_decided":
                aid = ev.get("approval_id") or ""
                acc.approval_events[aid] = {
                    "status": ev.get("status") or "",
                    "reasons": ev.get("reasons") or [],
                    "ts": ev.get("ts"),
                    "workflow_id": ev.get("workflow_id"),
                }

        # Calls whose lead was evicted from the small hot cache.
        if pending_calls:
            self._resolve_pending_calls(acc, pending_calls)

        with db.transaction(self.conn):
            if acc.leads:
                self.conn.executemany(
                    "INSERT OR IGNORE INTO lead_features(lead_id,workflow_id,campaign_id,person_id,"
                    "lang,city_tier,age_band,value_band) VALUES(?,?,?,?,?,?,?,?)",
                    acc.leads)
            for cid, c in acc.campaigns.items():
                self.conn.execute(
                    "INSERT INTO campaigns(campaign_id,workflow_id,leads,created_minute,created_day,"
                    "config_version_at_creation) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(campaign_id) DO UPDATE SET leads=excluded.leads,"
                    "created_minute=excluded.created_minute,created_day=excluded.created_day,"
                    "config_version_at_creation=excluded.config_version_at_creation",
                    (cid, c["workflow_id"], c["leads"], c["created_minute"], c["created_day"],
                     c["config_version_at_creation"]))
            for (wf, version), c in acc.config_events.items():
                self.conn.execute(
                    "INSERT INTO config_versions(workflow_id,version,actor,changed_dims_json,"
                    "config_json,applied_ts,proposal_id,approval_id) VALUES(?,?,?,?,'{}',?,?,?) "
                    "ON CONFLICT(workflow_id,version) DO UPDATE SET actor=excluded.actor,"
                    "changed_dims_json=excluded.changed_dims_json,applied_ts=excluded.applied_ts,"
                    "proposal_id=excluded.proposal_id,approval_id=excluded.approval_id",
                    (wf, version, c["actor"], db.json_dumps(c["changed_dims"]), c["ts"],
                     c["proposal_id"], c["approval_id"]))
            for aid, c in acc.approval_events.items():
                self.conn.execute(
                    "INSERT INTO approvals(approval_id,proposal_id,workflow_id,status,created_sim_minute,"
                    "decided_sim_minute,reasons_json) VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(approval_id) DO UPDATE SET status=excluded.status,"
                    "reasons_json=excluded.reasons_json",
                    (aid, "", c.get("workflow_id") or "", c["status"], None, None,
                     db.json_dumps(c["reasons"])))
            self._flush_aggregates(acc)
            if errors:
                self.conn.executemany(
                    "INSERT INTO ingestion_errors(chunk,line_no,error,raw_head) VALUES(?,?,?,?)",
                    errors)
            self.conn.execute(
                "INSERT INTO chunks(chunk,kind,first_event_id,last_event_id,events,processed_at) "
                "VALUES(?,?,?,?,?,?)",
                (n, meta.get("kind", "events"), meta.get("first_event_id"),
                 meta.get("last_event_id"), events,
                 datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")))
        self.total_events += events
        self.total_calls += calls
        self.total_leads += leads
        self.total_duplicates += duplicates
        self.total_malformed += malformed
        return {"events": events, "calls": calls, "leads": leads,
                "duplicates": duplicates, "malformed": malformed}

    # -- helpers -------------------------------------------------------------
    def _cache_lead(self, lid: str, features: dict) -> None:
        self.lead_cache[lid] = {k: str(v) for k, v in (features or {}).items()}
        self.lead_cache.move_to_end(lid)
        if len(self.lead_cache) > self.cache_limit:
            self.lead_cache.popitem(last=False)

    def _resolve_pending_calls(self, acc: Aggregator, pending: list[tuple[dict, dict]]) -> None:
        ids = sorted({ev.get("lead_id") for ev, _ in pending if ev.get("lead_id")})
        found: dict[str, dict] = {}
        for i in range(0, len(ids), 400):
            batch = ids[i:i + 400]
            q = ("SELECT lead_id,lang,city_tier,age_band,value_band FROM lead_features "
                 f"WHERE lead_id IN ({','.join('?' for _ in batch)})")
            for row in self.conn.execute(q, batch):
                found[row["lead_id"]] = {
                    "lang": row["lang"], "city_tier": row["city_tier"],
                    "age_band": row["age_band"], "value_band": row["value_band"],
                }
        for ev, _ in pending:
            feat = found.get(ev.get("lead_id") or "")
            if feat is None:
                # The lead event was never seen (e.g. malformed/partial world).
                # Skip the call rather than inventing features.
                continue
            acc.add_call(ev, feat,
                         self.workflows.get(ev.get("workflow_id"), {}).get("dials_per_hour", 0),
                         self.settings)

    def _flush_aggregates(self, acc: Aggregator) -> None:
        c = self.conn
        db.upsert_many(c, "variant_daily",
                       ["workflow_id", "day", "hour", "cohort_id", "config_version",
                        "welcome_message", "voice", "caller_id", "prompt_version",
                        "dials", "connected", "engaged", "interested", "halluc", "flow_sum", "flow_n"],
                       ((*k, v.get("dials", 0), v.get("connected", 0), v.get("engaged", 0),
                         v.get("interested", 0), v.get("halluc", 0), v.get("flow_sum", 0.0),
                         v.get("flow_n", 0)) for k, v in acc.variant.items()),
                       ["workflow_id", "day", "hour", "cohort_id", "config_version",
                        "welcome_message", "voice", "caller_id", "prompt_version"],
                       ["dials", "connected", "engaged", "interested", "halluc", "flow_sum", "flow_n"])
        db.upsert_many(c, "feature_variant_daily",
                       ["workflow_id", "day", "config_version", "feature", "feature_value",
                        "dim", "variant_value", "dials", "connected", "engaged", "halluc"],
                       ((*k, v.get("dials", 0), v.get("connected", 0), v.get("engaged", 0),
                         v.get("halluc", 0)) for k, v in acc.feature_variant.items()),
                       ["workflow_id", "day", "config_version", "feature", "feature_value",
                        "dim", "variant_value"],
                       ["dials", "connected", "engaged", "halluc"])
        db.upsert_many(c, "hour_feature_daily",
                       ["workflow_id", "day", "feature", "feature_value", "hour",
                        "dials", "connected", "engaged"],
                       ((*k, v.get("dials", 0), v.get("connected", 0), v.get("engaged", 0))
                        for k, v in acc.hour_feature.items()),
                       ["workflow_id", "day", "feature", "feature_value", "hour"],
                       ["dials", "connected", "engaged"])
        db.upsert_many(c, "did_daily",
                       ["workflow_id", "day", "caller_id", "dials", "connected", "engaged", "halluc"],
                       ((*k, v.get("dials", 0), v.get("connected", 0), v.get("engaged", 0),
                         v.get("halluc", 0)) for k, v in acc.did.items()),
                       ["workflow_id", "day", "caller_id"],
                       ["dials", "connected", "engaged", "halluc"])
        db.upsert_many(c, "prompt_daily",
                       ["workflow_id", "day", "prompt_version", "dials", "connected", "engaged", "halluc"],
                       ((*k, v.get("dials", 0), v.get("connected", 0), v.get("engaged", 0),
                         v.get("halluc", 0)) for k, v in acc.prompt.items()),
                       ["workflow_id", "day", "prompt_version"],
                       ["dials", "connected", "engaged", "halluc"])
        db.upsert_many(c, "cohort_hour_daily",
                       ["workflow_id", "cohort_id", "day", "hour", "dials", "connected", "engaged"],
                       ((*k, v.get("dials", 0), v.get("connected", 0), v.get("engaged", 0))
                        for k, v in acc.cohort_hour.items()),
                       ["workflow_id", "cohort_id", "day", "hour"],
                       ["dials", "connected", "engaged"])
        db.upsert_many(c, "concluded_daily",
                       ["workflow_id", "day", "concluded"],
                       ((*k, v.get("concluded", 0)) for k, v in acc.concluded.items()),
                       ["workflow_id", "day"], ["concluded"])
        db.upsert_many(c, "lead_daily",
                       ["workflow_id", "day", "lang", "city_tier", "age_band", "value_band", "uploaded"],
                       ((*k, v.get("uploaded", 0)) for k, v in acc.lead_daily.items()),
                       ["workflow_id", "day", "lang", "city_tier", "age_band", "value_band"],
                       ["uploaded"])
        if acc.person_hour:
            c.executemany(
                "INSERT INTO person_hour(workflow_id,person_id,hour,dials,connected,engaged,last_minute) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(workflow_id,person_id,hour) DO UPDATE SET "
                "dials=dials+excluded.dials,connected=connected+excluded.connected,"
                "engaged=engaged+excluded.engaged,last_minute=MAX(last_minute,excluded.last_minute)",
                ((*k, v.get("dials", 0), v.get("connected", 0), v.get("engaged", 0),
                  v.get("last_minute", 0)) for k, v in acc.person_hour.items()))
