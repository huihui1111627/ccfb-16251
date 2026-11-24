from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .database import Database, build_demo_database, make_burst_orders
from .engine import ExecutionTrace, QueryEngine
from .stats import collect_stats
from .workload import data_profile, event_schedule, generate_requests


@dataclass
class BenchmarkRun:
    report: Dict[str, Any]
    db: Database
    engines: Dict[str, QueryEngine]


def percentile(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * p
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def latency_distribution(traces: List[ExecutionTrace], attribute: str) -> Dict[str, float]:
    values = [float(getattr(trace, attribute)) for trace in traces]
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "mean": round(statistics.fmean(values), 4),
        "p50": round(percentile(values, 0.50), 4),
        "p75": round(percentile(values, 0.75), 4),
        "p90": round(percentile(values, 0.90), 4),
        "p95": round(percentile(values, 0.95), 4),
        "p99": round(percentile(values, 0.99), 4),
        "max": round(max(values), 4),
    }


def _status_counts(traces: List[ExecutionTrace]) -> Dict[str, int]:
    return dict(Counter(trace.cache_status for trace in traces))


def _signature_counts(traces: List[ExecutionTrace]) -> Dict[str, int]:
    return dict(Counter(trace.plan_signature for trace in traces))


def strategy_summary(engine: QueryEngine, traces: List[ExecutionTrace]) -> Dict[str, Any]:
    optimal_matches = [t for t in traces if t.chosen_is_optimal]
    return {
        "strategy": engine.strategy,
        "calls": len(traces),
        "predicted_e2e_ms": latency_distribution(traces, "predicted_total_ms"),
        "cost_execution_ms": latency_distribution(traces, "cost_ms"),
        "actual_total_wall_ms": latency_distribution(traces, "total_wall_ms"),
        "actual_executor_wall_ms": latency_distribution(traces, "exec_wall_ms"),
        "optimizer_call_ms": latency_distribution(traces, "optimizer_ms"),
        "parse_ms": latency_distribution(traces, "parse_ms"),
        "cache_status_counts": _status_counts(traces),
        "plan_signature_counts": _signature_counts(traces),
        "optimal_plan_match_rate": round(
            len(optimal_matches) / max(1, len(traces)), 4
        ),
        "wrong_plan_calls": sum(1 for t in traces if t.chosen_is_optimal is False),
        "engine_metrics": engine.metrics_dict(),
    }


def run_benchmark_run(
    calls: int = 720,
    max_cache_entries: int = 5,
    seed: int = 16251,
    merchant_count: int = 800,
    total_orders: int = 160_000,
    burst_size: int = 12_000,
) -> Dict[str, Any]:
    db = build_demo_database(
        seed=seed,
        merchant_count=merchant_count,
        total_orders=total_orders,
    )
    collect_stats(db, version=1, note="初始统计信息")

    adaptive = QueryEngine(db, "adaptive", max_entries=max_cache_entries)
    single = QueryEngine(db, "single")
    reopt = QueryEngine(db, "reopt")
    engines = {
        "adaptive": adaptive,
        "single": single,
        "reopt": reopt,
    }

    requests = generate_requests(db, call_count=calls)
    schedule = event_schedule(calls, merchant_count)
    restart_after = set(schedule["restart_after"])
    stats_after = schedule["stats_after"]
    events: List[Dict[str, Any]] = []
    traces: Dict[str, List[ExecutionTrace]] = {
        "adaptive": [], "single": [], "reopt": []
    }

    for request in requests:
        seq = request.seq
        # Rotate execution order so wall-clock warming does not always favor one mode.
        if seq % 3 == 1:
            order = ["adaptive", "single", "reopt"]
        elif seq % 3 == 2:
            order = ["single", "reopt", "adaptive"]
        else:
            order = ["reopt", "adaptive", "single"]

        seq_traces: Dict[str, ExecutionTrace] = {}
        for strategy in order:
            engine = engines[strategy]
            trace = engine.execute_sql(
                request.sql,
                seq=seq,
                keep_recent=(strategy == "adaptive"),
                sample_rows=False,
            )
            seq_traces[strategy] = trace
            traces[strategy].append(trace)

        optimal_signature = seq_traces["reopt"].plan_signature
        adaptive_trace = seq_traces["adaptive"]
        for strategy, trace in seq_traces.items():
            trace.optimal_signature = optimal_signature
            trace.chosen_is_optimal = trace.plan_signature == optimal_signature
            if strategy != "adaptive":
                trace.tier_id = adaptive_trace.tier_id
                trace.tier_label = adaptive_trace.tier_label
                trace.is_parameter_sensitive = adaptive_trace.is_parameter_sensitive

        if seq in restart_after:
            before = {name: engine.cache.size() for name, engine in engines.items()}
            for engine in engines.values():
                engine.restart()
            events.append({
                "seq": seq,
                "type": "restart",
                "title": "数据库/计划缓存重启",
                "detail": "三种策略的内存计划缓存同时清空；手动策略保留。",
                "cache_entries_before": before,
                "cache_entries_after": {name: 0 for name in engines},
            })

        if seq == stats_after:
            burst_id = schedule["burst_merchant_id"]
            rows = make_burst_orders(db, burst_id, burst_size)
            db.insert_order_rows(rows)
            collect_stats(db, note="促销爆单后重新收集直方图")
            invalidated = {
                name: engine.on_stats_updated() for name, engine in engines.items()
            }
            events.append({
                "seq": seq,
                "type": "stats_update",
                "title": "统计信息更新",
                "detail": (
                    f"商户 {burst_id} 新插入 {burst_size:,} 笔订单，"
                    "重新收集 MCV/直方图并失效相关计划。"
                ),
                "burst_merchant_id": burst_id,
                "inserted_orders": burst_size,
                "invalidated_plans": invalidated,
                "stats_version": db.stats_version,
            })

    summaries = {
        strategy: strategy_summary(engine, traces[strategy])
        for strategy, engine in engines.items()
    }

    adaptive_pred = summaries["adaptive"]["predicted_e2e_ms"]
    single_pred = summaries["single"]["predicted_e2e_ms"]
    reopt_metrics = summaries["reopt"]["engine_metrics"]
    adaptive_metrics = summaries["adaptive"]["engine_metrics"]
    single_metrics = summaries["single"]["engine_metrics"]

    report = {
        "config": {
            "calls": calls,
            "max_cache_entries": max_cache_entries,
            "seed": seed,
            "burst_size": burst_size,
            "schedule": schedule,
        },
        "data_profile": data_profile(db),
        "events": events,
        "summaries": summaries,
        "comparison": {
            "single_vs_adaptive_p95_slowdown": round(
                single_pred["p95"] / max(0.001, adaptive_pred["p95"]), 2
            ),
            "single_vs_adaptive_mean_slowdown": round(
                single_pred["mean"] / max(0.001, adaptive_pred["mean"]), 2
            ),
            "adaptive_vs_reopt_optimizer_time_saved_pct": round(
                100 * (
                    reopt_metrics["total_optimizer_ms"]
                    - adaptive_metrics["total_optimizer_ms"]
                ) / max(1.0, reopt_metrics["total_optimizer_ms"]),
                1,
            ),
            "cache_entries": {
                "adaptive_current": adaptive_metrics["cache"]["size"],
                "adaptive_max": adaptive_metrics["cache"]["max_entries"],
                "adaptive_inserted": adaptive_metrics["cache"]["metrics"]["inserts"],
                "adaptive_evictions": adaptive_metrics["cache"]["metrics"]["evictions"],
                "single_current": single_metrics["cache"]["size"],
                "reopt_current": 0,
            },
            "optimization_count": {
                "adaptive": adaptive_metrics["optimization_count"],
                "single": single_metrics["optimization_count"],
                "reopt": reopt_metrics["optimization_count"],
            },
            "total_optimizer_ms": {
                "adaptive": adaptive_metrics["total_optimizer_ms"],
                "single": single_metrics["total_optimizer_ms"],
                "reopt": reopt_metrics["total_optimizer_ms"],
            },
        },
        "statements": adaptive.statements_state(),
        "cache": adaptive.cache.to_dict(),
        "policies": adaptive.policies.all(),
        "recent_calls": [trace.to_dict() for trace in adaptive.recent],
        "traces": {
            strategy: [trace.to_dict() for trace in strategy_traces]
            for strategy, strategy_traces in traces.items()
        },
    }
    return BenchmarkRun(report=report, db=db, engines=engines)


def run_benchmark(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return run_benchmark_run(*args, **kwargs).report


def build_live_engine_from_report_config(report: Dict[str, Any]) -> QueryEngine:
    """The server normally keeps the benchmark engine directly; this helper is for tests."""
    config = report["config"]
    db = build_demo_database(seed=config["seed"])
    collect_stats(db, version=1, note="初始统计信息")
    return QueryEngine(db, max_entries=config["max_cache_entries"])
