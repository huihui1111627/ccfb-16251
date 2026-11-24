from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from .benchmark import BenchmarkRun
from .database import make_burst_orders
from .sql import parse_sql
from .stats import collect_stats
from .workload import AGG_SQL_TEMPLATE, MAIN_SQL_TEMPLATE, POINT_SQL_TEMPLATE

WEB_DIR = Path(__file__).parent / "web"


class DashboardState:
    def __init__(self, run: BenchmarkRun):
        self.run = run
        self.engine = run.engines["adaptive"]
        self.live_events = []

    def snapshot(self) -> Dict[str, Any]:
        report = self.run.report
        # Live engine state supersedes the state captured at benchmark finish.
        report["statements"] = self.engine.statements_state()
        report["cache"] = self.engine.cache.to_dict()
        report["policies"] = self.engine.policies.all()
        report["recent_calls"] = [trace.to_dict() for trace in self.engine.recent]
        report["live_metrics"] = self.engine.metrics_dict()
        report["live_events"] = list(self.live_events[-30:])
        return report

    def find_statement_key(self, statement_id: str) -> Optional[str]:
        for key, runtime in self.engine.runtimes.items():
            if runtime.statement_id == statement_id:
                return key
        policy = self.engine.policies.policies.get(statement_id)
        return statement_id if policy else None

    def call_query(self, body: Dict[str, Any]) -> Dict[str, Any]:
        if body.get("sql"):
            sql = str(body["sql"])
        else:
            kind = body.get("kind", "main")
            merchant_id = int(body.get("merchant_id") or 1)
            if kind == "point":
                order_id = int(body.get("order_id") or 1)
                sql = POINT_SQL_TEMPLATE.format(order_id=order_id)
            elif kind == "agg":
                sql = AGG_SQL_TEMPLATE.format(merchant_id=merchant_id)
            else:
                sql = MAIN_SQL_TEMPLATE.format(merchant_id=merchant_id)

        trace = self.engine.execute_sql(
            sql,
            seq=self.engine.call_count + 1,
            sample_rows=True,
        )
        return {"trace": trace.to_dict()}

    def set_policy(self, body: Dict[str, Any]) -> Dict[str, Any]:
        statement_id = body.get("statement_id") or body.get("statement_key")
        key = self.find_statement_key(str(statement_id))
        if key is None:
            raise KeyError(f"未知语句: {statement_id}")
        mode = body.get("mode", "auto")
        forced = body.get("forced_signature")
        if mode == "pin":
            mode = "pinned"
        if mode == "pinned" and not forced:
            forced = body.get("plan_signature")
        policy = self.engine.policies.set(
            key,
            mode=mode,
            forced_signature=forced,
            call_seq=self.engine.call_count,
        )
        # Policy changes invalidate old adaptive/pinned entries for this statement.
        removed = self.engine.cache.invalidate_statement(key)
        runtime = self.engine.runtimes.get(key)
        if runtime is not None and mode == "auto":
            runtime.needs_rebuild = True
        return {"policy": policy.to_dict(), "removed_entries": removed}

    def restart(self) -> Dict[str, Any]:
        removed = self.engine.restart()
        event = {"title": "手动重启", "detail": f"清空 {removed} 条计划缓存", "kind": "restart"}
        self.live_events.append(event)
        return {"removed_entries": removed, "event": event}

    def set_cache_limit(self, max_entries: int) -> Dict[str, Any]:
        evicted = self.engine.cache.set_limit(max_entries)
        return {"max_entries": self.engine.cache.max_entries, "evicted": evicted}

    def refresh_stats(self, body: Dict[str, Any]) -> Dict[str, Any]:
        merchant_id = body.get("merchant_id")
        burst_size = int(body.get("burst_size") or 0)
        inserted = 0
        if merchant_id and burst_size > 0:
            merchant_id = int(merchant_id)
            rows = make_burst_orders(self.run.db, merchant_id, burst_size, seed=4242)
            inserted = self.run.db.insert_order_rows(rows)
            note = f"手动插入爆单 {inserted} 行后更新统计"
        else:
            note = "手动采集统计信息"
        collect_stats(self.run.db, note=note)
        invalidated = {}
        for name, engine in self.run.engines.items():
            invalidated[name] = engine.on_stats_updated()
        event = {
            "title": "统计信息更新",
            "detail": note + f"；失效计划 {invalidated}",
            "kind": "stats_update",
            "invalidated": invalidated,
            "inserted": inserted,
        }
        self.live_events.append(event)
        return event


class LazyDashboardState:
    """Serves a loading payload until the benchmark has finished."""

    def __init__(self) -> None:
        self.ready: Optional[DashboardState] = None
        self.status = "running"
        self.error: Optional[str] = None

    def bind(self, run: BenchmarkRun) -> DashboardState:
        self.ready = DashboardState(run)
        self.status = "ready"
        return self.ready

    def snapshot(self) -> Dict[str, Any]:
        if self.ready is not None:
            return self.ready.snapshot()
        return {"status": self.status, "error": self.error}

    def __getattr__(self, name: str) -> Any:
        if name in {"call_query", "set_policy", "restart", "set_cache_limit", "refresh_stats"}:
            def missing(*_args: Any, **_kwargs: Any) -> None:
                raise RuntimeError("基准仍在运行，请稍候再试")
            return missing
        raise AttributeError(name)


def create_server(
    run: BenchmarkRun,
    host: str = "127.0.0.1",
    port: int = 8766,
) -> ThreadingHTTPServer:
    return create_server_from_state(DashboardState(run), host, port)


def create_lazy_server(
    lazy_state: LazyDashboardState,
    host: str = "127.0.0.1",
    port: int = 8766,
) -> ThreadingHTTPServer:
    return create_server_from_state(lazy_state, host, port)  # type: ignore[arg-type]


def create_server_from_state(
    state: DashboardState,
    host: str = "127.0.0.1",
    port: int = 8766,
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            # Keep test/CLI output clean; errors still surface via status codes.
            if "/api/" in (self.path or ""):
                super().log_message(fmt, *args)

        def _send_json(self, payload: Any, status: int = 200) -> None:
            raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _read_json(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
                return data if isinstance(data, dict) else {}
            except json.JSONDecodeError:
                return {}

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/api/state":
                self._send_json(state.snapshot())
                return
            if path in {"/", "/index.html"}:
                self._serve_file("index.html", "text/html; charset=utf-8")
                return
            if path in {"/app.js", "/styles.css"}:
                content_type = (
                    "application/javascript; charset=utf-8"
                    if path.endswith(".js")
                    else "text/css; charset=utf-8"
                )
                self._serve_file(path.lstrip("/"), content_type)
                return
            self._send_json({"error": "not found", "path": path}, 404)

        def _serve_file(self, filename: str, content_type: str) -> None:
            file_path = (WEB_DIR / filename).resolve()
            if WEB_DIR.resolve() not in file_path.parents or not file_path.exists():
                self._send_json({"error": "asset not found"}, 404)
                return
            raw = file_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            body = self._read_json()
            try:
                if path == "/api/call":
                    self._send_json(state.call_query(body))
                elif path == "/api/policy":
                    self._send_json(state.set_policy(body))
                elif path == "/api/restart":
                    self._send_json(state.restart())
                elif path == "/api/cache-limit":
                    self._send_json(state.set_cache_limit(int(body.get("max_entries") or 5)))
                elif path == "/api/refresh-stats":
                    self._send_json(state.refresh_stats(body))
                else:
                    self._send_json({"error": "not found", "path": path}, 404)
            except KeyError as exc:
                self._send_json({"error": str(exc)}, 404)
            except Exception as exc:  # pragma: no cover - surfaced to UI
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    server = ThreadingHTTPServer((host, port), Handler)
    server.dashboard_state = state  # type: ignore[attr-defined]
    return server
