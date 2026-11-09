"""HTTP client for the documented simulator API.

The controller has no other source of simulator information: no imports from
``sim``, no scenario files, no run-directory reads.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Iterator

import httpx

log = logging.getLogger("controller.api")


class SimError(RuntimeError):
    def __init__(self, status: int, code: str, detail: Any = None):
        super().__init__(f"{status} {code}: {detail}")
        self.status = status
        self.code = code
        self.detail = detail


class SimClient:
    def __init__(self, base_url: str, timeout: float = 180.0, max_retries: int = 6):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout,
                                    headers={"accept": "application/json"})
        self.request_count = 0

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, url: str, **kw) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                self.request_count += 1
                r = self._client.request(method, url, **kw)
                if r.status_code >= 500:
                    raise SimError(r.status_code, "server_error", r.text[:500])
                if r.status_code >= 400:
                    try:
                        body = r.json()
                    except ValueError:
                        body = {"error": r.text[:500], "detail": None}
                    raise SimError(r.status_code, body.get("error", "http_error"),
                                   body.get("detail"))
                return r
            except SimError:
                raise
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last = e
                if attempt + 1 >= self.max_retries:
                    break
                delay = min(8.0, 0.5 * (2 ** attempt))
                log.warning("simulator request failed; retrying",
                            extra={"url": url, "attempt": attempt + 1, "delay_s": delay,
                                   "error": str(e)})
                time.sleep(delay)
        raise SimError(0, "unavailable", str(last))

    def wait_ready(self, timeout_s: float = 120.0) -> None:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                self._request("GET", "/health")
                return
            except SimError as e:
                if e.status not in (0, 500):
                    raise
                time.sleep(1.0)
        raise SimError(0, "not_ready", f"simulator not healthy after {timeout_s}s")

    # -- reads ---------------------------------------------------------------
    def state(self) -> dict:
        return self._request("GET", "/sim/state").json()

    def workflows(self) -> list[dict]:
        return self._request("GET", "/workflows").json()

    def workflow(self, wid: str) -> dict:
        return self._request("GET", f"/workflows/{wid}").json()

    def config(self, wid: str) -> dict:
        return self._request("GET", f"/workflows/{wid}/config").json()

    def config_history(self, wid: str) -> list[dict]:
        return self._request("GET", f"/workflows/{wid}/config/history").json()

    def list_chunks(self, kind: str = "events", after: int = -1) -> list[dict]:
        return self._request("GET", "/events/chunks",
                             params={"kind": kind, "after": after}).json()

    def iter_chunk(self, n: int) -> Iterator[dict]:
        last: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                self.request_count += 1
                with self._client.stream("GET", f"/events/chunks/{n}") as r:
                    if r.status_code >= 500:
                        raise SimError(r.status_code, "server_error", "chunk read failed")
                    if r.status_code >= 400:
                        r.read()
                        try:
                            body = r.json()
                        except ValueError:
                            body = {"error": "http_error", "detail": None}
                        raise SimError(r.status_code, body.get("error", "http_error"), body.get("detail"))
                    for line in r.iter_lines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            yield json.loads(line)
                        except ValueError as e:
                            yield {"__malformed__": True, "error": str(e),
                                   "raw_head": line[:200]}
                    return
            except SimError:
                raise
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last = e
                if attempt + 1 >= self.max_retries:
                    break
                delay = min(8.0, 0.5 * (2 ** attempt))
                log.warning("chunk read failed; retrying",
                            extra={"chunk": n, "attempt": attempt + 1, "delay_s": delay,
                                   "error": str(e)})
                time.sleep(delay)
        raise SimError(0, "unavailable", f"chunk {n}: {last}")

    # -- time ----------------------------------------------------------------
    def advance(self, minutes: int) -> dict:
        return self._request("POST", "/sim/advance", json={"minutes": minutes}).json()

    # -- governance ----------------------------------------------------------
    def request_approval(self, body: dict) -> dict:
        return self._request("POST", "/approvals", json=body).json()

    def get_approval(self, aid: str) -> dict:
        return self._request("GET", f"/approvals/{aid}").json()

    def list_approvals(self, workflow_id: str | None = None,
                      status: str | None = None) -> list[dict]:
        params = {}
        if workflow_id:
            params["workflow_id"] = workflow_id
        if status:
            params["status"] = status
        return self._request("GET", "/approvals", params=params).json()

    def apply(self, wid: str, approval_id: str) -> dict:
        return self._request("POST", f"/workflows/{wid}/apply",
                             json={"approval_id": approval_id}).json()

    def auto_apply(self, wid: str, body: dict) -> dict:
        return self._request("PUT", f"/workflows/{wid}/config", json=body).json()

    # -- credits -------------------------------------------------------------
    def suggest_fixes(self, wid: str, problem: str) -> dict:
        return self._request("POST", "/llm/suggest-fixes",
                             json={"workflow_id": wid, "problem": problem}).json()

    def run_validation(self, wid: str, prompt_version: str, n: int) -> dict:
        return self._request("POST", "/validation/runs",
                             json={"workflow_id": wid, "prompt_version": prompt_version,
                                   "n": int(n)}).json()
