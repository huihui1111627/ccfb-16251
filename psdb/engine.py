from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple
from collections import deque

from .cache import CachedPlan, PlanCache, PolicyStore, StatementPolicy
from .executor import Executor
from .optimizer import Optimizer, Plan, Tier
from .sql import Statement, parse_sql
from .stats import CatalogStats, collect_stats


@dataclass
class StatementRuntime:
    statement_key: str
    statement_id: str
    normalized: str
    sample_sql: str
    family: str
    tiers: List[Tier]
    sensitivity: Dict[str, Any]
    first_seen_seq: int
    call_count: int = 0
    last_seen_seq: int = 0
    needs_rebuild: bool = False

    def find_tier(self, selectivity: float) -> Tier:
        for tier in self.tiers:
            if tier.contains(selectivity):
                return tier
        if selectivity >= 1.0 and self.tiers:
            return self.tiers[-1]
        return self.tiers[0]

    def to_dict(self, cache: PlanCache, policy: StatementPolicy) -> dict:
        tiers = []
        for tier in self.tiers:
            cache_key = PlanCache.make_key(self.statement_key, tier.tier_id)
            cached = cache.peek(cache_key)
            tiers.append(tier.to_dict(
                cached=cached is not None,
                cache_key=cache_key,
            ))
        probes = self.sensitivity.get("probes", [])
        return {
            "statement_id": self.statement_id,
            "normalized": self.normalized,
            "sample_sql": self.sample_sql,
            "family": self.family,
            "sensitive": self.sensitivity.get("sensitive", False),
            "histogram_winners": self.sensitivity.get("histogram_winners", []),
            "sweep_winners": self.sensitivity.get("sweep_winners", []),
            "probe_count": len(probes),
            "probes": probes[:30],
            "tiers": tiers,
            "policy": policy.to_dict(),
            "first_seen_seq": self.first_seen_seq,
            "last_seen_seq": self.last_seen_seq,
            "call_count": self.call_count,
            "needs_rebuild": self.needs_rebuild,
        }


@dataclass
class ExecutionTrace:
    seq: int
    strategy: str
    sql: str
    statement_key: str
    statement_id: str
    family: str
    cache_status: str
    plan_signature: str
    plan_label: str
    access_path: str
    join_strategy: str
    selectivity: float
    estimated_rows: float
    tier_id: Optional[str]
    tier_label: Optional[str]
    parse_ms: float
    optimizer_ms: float
    exec_wall_ms: float
    total_wall_ms: float
    cost_ms: float
    estimated_cost_ms: float
    predicted_total_ms: float
    scanned_rows: int
    fetched_rows: int
    joined_rows: int
    result_rows: int
    optimal_signature: Optional[str] = None
    is_parameter_sensitive: bool = False
    chosen_is_optimal: Optional[bool] = None
    sample_rows: List[Dict[str, Any]] = field(default_factory=list)
    event: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "strategy": self.strategy,
            "sql": self.sql,
            "statement_key": self.statement_key,
            "statement_id": self.statement_id,
            "family": self.family,
            "cache_status": self.cache_status,
            "plan_signature": self.plan_signature,
            "plan_label": self.plan_label,
            "access_path": self.access_path,
            "join_strategy": self.join_strategy,
            "selectivity": self.selectivity,
            "selectivity_pct": self.selectivity * 100.0,
            "estimated_rows": round(self.estimated_rows, 2),
            "tier_id": self.tier_id,
            "tier_label": self.tier_label,
            "parse_ms": round(self.parse_ms, 4),
            "optimizer_ms": round(self.optimizer_ms, 4),
            "exec_wall_ms": round(self.exec_wall_ms, 4),
            "total_wall_ms": round(self.total_wall_ms, 4),
            "cost_ms": round(self.cost_ms, 4),
            "estimated_cost_ms": round(self.estimated_cost_ms, 4),
            "predicted_total_ms": round(self.predicted_total_ms, 4),
            "scanned_rows": self.scanned_rows,
            "fetched_rows": self.fetched_rows,
            "joined_rows": self.joined_rows,
            "result_rows": self.result_rows,
            "optimal_signature": self.optimal_signature,
            "is_parameter_sensitive": self.is_parameter_sensitive,
            "chosen_is_optimal": self.chosen_is_optimal,
            "sample_rows": self.sample_rows,
            "event": self.event,
        }


class QueryEngine:
    def __init__(
        self,
        database: Any,
        strategy: str = "adaptive",
        max_entries: int = 5,
        policies: Optional[PolicyStore] = None,
    ):
        self.db = database
        self.strategy = strategy
        self.stats: CatalogStats = database.catalog_stats
        self.optimizer = Optimizer(self.stats)
        self.executor = Executor(database)
        cache_limit = max_entries if strategy == "adaptive" else 200
        self.cache = PlanCache(max_entries=cache_limit)
        self.policies = policies or PolicyStore()
        self.runtimes: Dict[str, StatementRuntime] = {}
        self.optimization_count = 0
        self.total_optimizer_ms = 0.0
        self.total_parse_ms = 0.0
        self.total_exec_wall_ms = 0.0
        self.call_count = 0
        self.recent: Deque[ExecutionTrace] = deque(maxlen=80)

    @staticmethod
    def statement_id_for(key: str) -> str:
        return "stmt_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]

    def restart(self) -> int:
        removed = self.cache.clear("restart")
        # Keep the last tier picture for the UI, but require real replanning
        # before this statement's tier plans are cached again.
        for runtime in self.runtimes.values():
            runtime.needs_rebuild = True
        return removed

    def on_stats_updated(self) -> None:
        self.stats = self.db.catalog_stats
        self.optimizer = Optimizer(self.stats)
        invalidated = self.cache.invalidate_for_tables({"orders", "merchants"})
        # Old boundaries remain visible as stale metadata until the next use,
        # at which point histogram tiers and plans are rebuilt.
        for runtime in self.runtimes.values():
            runtime.needs_rebuild = True
        return invalidated

    def execute_sql(
        self,
        sql: str,
        params: Optional[List[Any]] = None,
        seq: Optional[int] = None,
        keep_recent: bool = True,
        sample_rows: bool = False,
        event: Optional[str] = None,
    ) -> ExecutionTrace:
        total_start = time.perf_counter()
        if seq is None:
            seq = self.call_count + 1
        self.call_count += 1

        parse_start = time.perf_counter()
        stmt = parse_sql(sql, params)
        parse_ms = (time.perf_counter() - parse_start) * 1000.0
        self.total_parse_ms += parse_ms
        key = stmt.normalized
        statement_id = self.statement_id_for(key)

        optimizer_start = time.perf_counter()
        tier: Optional[Tier] = None
        tier_id = None
        tier_label = None
        cache_status = "MISS"
        policy = self.policies.get(key)

        if self.strategy == "adaptive":
            runtime = self._ensure_runtime(stmt, sql, seq)
            runtime.call_count += 1
            runtime.last_seen_seq = seq
            selectivity = self.optimizer.estimate_selectivity(stmt)
            tier = runtime.find_tier(selectivity)
            tier_id = tier.tier_id
            tier_label = tier.label

            if policy.mode == "disabled":
                plan = self.optimizer.optimize(stmt)
                cache_status = "BYPASS"
                self.cache.metrics.bypasses += 1
                self.optimization_count += 1
            elif policy.mode == "pinned":
                cache_key = PlanCache.make_key(key, "pinned")
                cached = self.cache.get(cache_key, seq)
                if cached is not None:
                    plan = cached.plan
                    cache_status = "PINNED_HIT"
                else:
                    plan = self.optimizer.optimize(
                        stmt, forced_signature=policy.forced_signature
                    )
                    self.optimization_count += 1
                    pinned = CachedPlan(
                        cache_key=cache_key,
                        statement_key=key,
                        tier_id="pinned",
                        plan=plan,
                        created_seq=seq,
                        last_used_seq=seq,
                        pinned=True,
                        tables=self._tables(stmt),
                    )
                    self.cache.put(pinned)
                    cache_status = "PINNED_MISS"
            else:
                cache_key = PlanCache.make_key(key, tier.tier_id)
                cached = self.cache.get(cache_key, seq)
                if cached is not None:
                    plan = cached.plan
                    cache_status = "HIT"
                else:
                    plan = self.optimizer.optimize_for_selectivity(
                        stmt, selectivity
                    )
                    plan.tier_id = tier.tier_id
                    plan.tier_label = tier.label
                    self.optimization_count += 1
                    entry = CachedPlan(
                        cache_key=cache_key,
                        statement_key=key,
                        tier_id=tier.tier_id,
                        plan=plan,
                        created_seq=seq,
                        last_used_seq=seq,
                        tables=self._tables(stmt),
                    )
                    self.cache.put(entry)
                    cache_status = "TIER_REBUILD"
        elif self.strategy == "single":
            selectivity = self.optimizer.estimate_selectivity(stmt)
            cache_key = PlanCache.make_key(key, "single")
            cached = self.cache.get(cache_key, seq)
            if cached is not None:
                plan = cached.plan
                cache_status = "SINGLE_HIT"
            else:
                plan = self.optimizer.optimize(stmt)
                self.optimization_count += 1
                entry = CachedPlan(
                    cache_key=cache_key,
                    statement_key=key,
                    tier_id="single",
                    plan=plan,
                    created_seq=seq,
                    last_used_seq=seq,
                    tables=self._tables(stmt),
                )
                self.cache.put(entry)
                cache_status = "SINGLE_MISS"
        else:
            selectivity = self.optimizer.estimate_selectivity(stmt)
            plan = self.optimizer.optimize(stmt)
            self.optimization_count += 1
            cache_status = "REOPTIMIZE"

        optimizer_ms = (time.perf_counter() - optimizer_start) * 1000.0
        self.total_optimizer_ms += optimizer_ms

        exec_start = time.perf_counter()
        result = self.executor.execute(stmt, plan)
        exec_wall_ms = (time.perf_counter() - exec_start) * 1000.0
        self.total_exec_wall_ms += exec_wall_ms
        total_wall_ms = (time.perf_counter() - total_start) * 1000.0
        realized_cost_ms = self.realized_cost_ms(plan, result)

        trace = ExecutionTrace(
            seq=seq,
            strategy=self.strategy,
            sql=sql,
            statement_key=key,
            statement_id=statement_id,
            family=Optimizer.family(stmt),
            cache_status=cache_status,
            plan_signature=plan.signature,
            plan_label=plan.label,
            access_path=plan.access_path,
            join_strategy=plan.join_strategy,
            selectivity=selectivity,
            estimated_rows=plan.estimated_rows,
            tier_id=tier_id,
            tier_label=tier_label,
            parse_ms=parse_ms,
            optimizer_ms=optimizer_ms,
            exec_wall_ms=exec_wall_ms,
            total_wall_ms=total_wall_ms,
            cost_ms=realized_cost_ms,
            estimated_cost_ms=plan.cost_ms,
            predicted_total_ms=realized_cost_ms + optimizer_ms + parse_ms,
            scanned_rows=result.scanned_rows,
            fetched_rows=result.fetched_rows,
            joined_rows=result.joined_rows,
            result_rows=result.row_count,
            is_parameter_sensitive=bool(
                self.runtimes[key].sensitivity.get("sensitive")
            ) if key in self.runtimes and self.runtimes[key].tiers else False,
            sample_rows=result.rows[:3] if sample_rows else [],
            event=event,
        )
        if keep_recent:
            self.recent.appendleft(trace)
        return trace

    def realized_cost_ms(self, plan: Plan, result: Any) -> float:
        """Cost the chosen operator shape against actual row counts.

        Cached plans keep their original histogram estimate, but call-latency
        distributions should reflect the number of rows actually processed.
        """
        c = self.optimizer.cost
        family = plan.family
        fetched = float(result.fetched_rows or 0)
        scanned = float(result.scanned_rows or 0)
        joined = float(result.joined_rows or fetched or 0)
        merchant_count = float(self.stats.tables["merchants"].row_count)

        if family == "merchant_orders_join":
            if plan.access_path.startswith("IndexScan"):
                access = c.index_seek_ms + (c.index_entry_ms + c.heap_fetch_ms) * fetched
            else:
                access = c.seq_scan_per_row_ms * scanned + c.seq_filter_per_output_ms * fetched
            if plan.join_strategy.startswith("NestedLoop"):
                join = c.nl_lookup_ms * joined
            else:
                join = c.hash_build_per_row_ms * merchant_count + c.hash_probe_per_row_ms * joined
            return access + join + c.sort_ms(joined)

        if family == "merchant_orders_aggregate":
            if plan.access_path.startswith("IndexScan"):
                access = c.index_seek_ms + (c.index_entry_ms + c.heap_fetch_ms) * fetched
            else:
                access = c.seq_scan_per_row_ms * scanned + c.seq_filter_per_output_ms * fetched
            return access + c.aggregate_per_row_ms * joined

        if family == "order_primary_key":
            if plan.signature == "pk_point":
                return c.pk_seek_ms + c.pk_fetch_ms
            return c.seq_scan_per_row_ms * scanned

        return c.seq_scan_per_row_ms * scanned

    def _ensure_runtime(self, stmt: Statement, sql: str, seq: int) -> StatementRuntime:
        key = stmt.normalized
        runtime = self.runtimes.get(key)
        if runtime is not None and runtime.tiers and not runtime.needs_rebuild:
            return runtime

        tiers, sensitivity = self.optimizer.build_tiers(stmt)
        self.optimization_count += 1
        if runtime is None:
            runtime = StatementRuntime(
                statement_key=key,
                statement_id=self.statement_id_for(key),
                normalized=key,
                sample_sql=sql,
                family=Optimizer.family(stmt),
                tiers=tiers,
                sensitivity=sensitivity,
                first_seen_seq=seq,
            )
            self.runtimes[key] = runtime
        else:
            runtime.tiers = tiers
            runtime.sensitivity = sensitivity
            runtime.sample_sql = sql
            runtime.needs_rebuild = False

        # A sensitive statement gets one cached plan per selectivity tier.
        for tier in tiers:
            cache_key = PlanCache.make_key(key, tier.tier_id)
            entry = CachedPlan(
                cache_key=cache_key,
                statement_key=key,
                tier_id=tier.tier_id,
                plan=tier.plan,
                created_seq=seq,
                last_used_seq=seq,
                tables=self._tables(stmt),
            )
            self.cache.put(entry)
        return runtime

    @staticmethod
    def _tables(stmt: Statement) -> Tuple[str, ...]:
        tables = {stmt.from_table}
        tables.update(join.table for join in stmt.joins)
        return tuple(sorted(tables))

    def statements_state(self) -> List[dict]:
        result = []
        for key, runtime in self.runtimes.items():
            result.append(runtime.to_dict(self.cache, self.policies.get(key)))
        return sorted(result, key=lambda item: item["statement_id"])

    def metrics_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "calls": self.call_count,
            "optimization_count": self.optimization_count,
            "total_optimizer_ms": round(self.total_optimizer_ms, 3),
            "avg_optimizer_ms": round(
                self.total_optimizer_ms / max(1, self.call_count), 4
            ),
            "total_parse_ms": round(self.total_parse_ms, 3),
            "total_exec_wall_ms": round(self.total_exec_wall_ms, 3),
            "cache": self.cache.to_dict(),
        }
