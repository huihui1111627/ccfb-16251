from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .sql import Statement
from .stats import CatalogStats


@dataclass
class Plan:
    signature: str
    statement_key: str
    family: str
    label: str
    access_path: str
    join_strategy: str
    selectivity: float
    estimated_rows: float
    cost_ms: float
    cost_breakdown: Dict[str, float]
    explain: List[str]
    tier_id: Optional[str] = None
    tier_label: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "signature": self.signature,
            "statement_key": self.statement_key,
            "family": self.family,
            "label": self.label,
            "access_path": self.access_path,
            "join_strategy": self.join_strategy,
            "selectivity": self.selectivity,
            "estimated_rows": round(self.estimated_rows, 2),
            "cost_ms": round(self.cost_ms, 4),
            "cost_breakdown": {k: round(v, 4) for k, v in self.cost_breakdown.items()},
            "explain": self.explain,
            "tier_id": self.tier_id,
            "tier_label": self.tier_label,
        }


@dataclass
class Tier:
    tier_id: str
    label: str
    lower_selectivity: float
    upper_selectivity: float
    plan_signature: str
    representative_selectivity: float
    plan: Plan
    probe_sources: List[str] = field(default_factory=list)

    def contains(self, selectivity: float) -> bool:
        return self.lower_selectivity <= selectivity < self.upper_selectivity

    def to_dict(self, cached: bool = True, cache_key: Optional[str] = None) -> dict:
        data = {
            "tier_id": self.tier_id,
            "label": self.label,
            "lower_selectivity": self.lower_selectivity,
            "upper_selectivity": min(1.0, self.upper_selectivity),
            "lower_pct": self.lower_selectivity * 100.0,
            "upper_pct": min(1.0, self.upper_selectivity) * 100.0,
            "plan_signature": self.plan_signature,
            "representative_selectivity": self.representative_selectivity,
            "representative_pct": self.representative_selectivity * 100.0,
            "plan": self.plan.to_dict(),
            "probe_sources": self.probe_sources,
            "cached": cached,
            "cache_key": cache_key,
        }
        return data


class CostModel:
    """Transparent cost constants. Units are modeled milliseconds."""

    index_seek_ms = 0.35
    index_entry_ms = 0.0011
    heap_fetch_ms = 0.0105
    nl_lookup_ms = 0.0038
    hash_build_per_row_ms = 0.00050
    hash_probe_per_row_ms = 0.00070
    seq_scan_per_row_ms = 0.000065
    seq_filter_per_output_ms = 0.00035
    aggregate_per_row_ms = 0.00115
    sort_compare_ms = 0.000018
    pk_seek_ms = 0.22
    pk_fetch_ms = 0.08

    def sort_ms(self, rows: float) -> float:
        rows = max(1.0, rows)
        return self.sort_compare_ms * rows * math.log2(rows)


class Optimizer:
    def __init__(self, stats: CatalogStats, cost_model: Optional[CostModel] = None):
        self.stats = stats
        self.cost = cost_model or CostModel()

    @staticmethod
    def family(stmt: Statement) -> str:
        joined_tables = {j.table for j in stmt.joins}
        if "merchants" in joined_tables:
            return "merchant_orders_join"
        if stmt.has_aggregate:
            return "merchant_orders_aggregate"
        if stmt.from_table == "orders":
            return "order_primary_key"
        return "generic"

    def _merchant_value(self, stmt: Statement) -> Any:
        values = stmt.predicate_values("orders", "merchant_id")
        return values[0] if values else None

    def estimate_selectivity(self, stmt: Statement) -> float:
        family = self.family(stmt)
        orders_count = max(1, self.stats.tables["orders"].row_count)
        if family in {"merchant_orders_join", "merchant_orders_aggregate"}:
            value = self._merchant_value(stmt)
            if value is None:
                return 1.0
            return self.stats.selectivity("orders", "merchant_id", value)
        if family == "order_primary_key":
            values = stmt.predicate_values("orders", "id")
            if values:
                return self.stats.selectivity("orders", "id", values[0])
        return 1.0 / max(1, orders_count)

    def optimize(self, stmt: Statement, forced_signature: Optional[str] = None) -> Plan:
        selectivity = self.estimate_selectivity(stmt)
        return self.optimize_for_selectivity(
            stmt, selectivity, forced_signature=forced_signature
        )

    def optimize_for_selectivity(
        self,
        stmt: Statement,
        selectivity: float,
        forced_signature: Optional[str] = None,
    ) -> Plan:
        family = self.family(stmt)
        selectivity = max(1e-12, min(1.0, selectivity))
        if family == "merchant_orders_join":
            candidates = self._main_join_plans(stmt, selectivity)
        elif family == "merchant_orders_aggregate":
            candidates = self._aggregate_plans(stmt, selectivity)
        elif family == "order_primary_key":
            candidates = self._pk_plans(stmt, selectivity)
        else:
            candidates = self._generic_plans(stmt, selectivity)

        if forced_signature:
            if forced_signature not in candidates:
                raise ValueError(
                    f"计划 {forced_signature!r} 不适用于语句 {stmt.normalized[:80]}"
                )
            return candidates[forced_signature]
        return min(candidates.values(), key=lambda p: p.cost_ms)

    def _counts(self, selectivity: float) -> Tuple[int, float, float]:
        orders = self.stats.tables["orders"].row_count
        merchants = self.stats.tables.get("merchants")
        merchant_count = float(merchants.row_count if merchants else 1)
        rows = max(1.0, selectivity * orders)
        return orders, merchant_count, rows

    def _main_join_plans(self, stmt: Statement, selectivity: float) -> Dict[str, Plan]:
        n_orders, n_merchants, rows = self._counts(selectivity)
        c = self.cost
        sort_cost = c.sort_ms(rows)

        index_scan_cost = (
            c.index_seek_ms
            + c.index_entry_ms * rows
            + c.heap_fetch_ms * rows
        )
        seq_scan_cost = c.seq_scan_per_row_ms * n_orders + c.seq_filter_per_output_ms * rows

        nl_cost = c.nl_lookup_ms * rows
        hash_cost = (
            c.hash_build_per_row_ms * n_merchants
            + c.hash_probe_per_row_ms * rows
        )

        common_breakdown_sort = {"sort_top_n": sort_cost}

        index_nl = Plan(
            signature="index_nl",
            statement_key=stmt.normalized,
            family="merchant_orders_join",
            label="merchant_id 索引扫描 + 嵌套循环回 merchants 主键查询",
            access_path="IndexScan(idx_orders_merchant)",
            join_strategy="NestedLoop + PK",
            selectivity=selectivity,
            estimated_rows=rows,
            cost_ms=index_scan_cost + nl_cost + sort_cost,
            cost_breakdown={
                "btree_seek": c.index_seek_ms,
                "index_entries": c.index_entry_ms * rows,
                "heap_fetches": c.heap_fetch_ms * rows,
                "nested_loop_lookups": nl_cost,
                **common_breakdown_sort,
            },
            explain=[
                f"Index Scan using idx_orders_merchant on orders (~{rows:.0f} rows)",
                "Bitmap/heap fetch each matching order row",
                "Nested Loop: merchants primary-key lookup per order",
                "Sort all merchant orders by created_ts DESC, then LIMIT 20",
            ],
        )

        index_hash = Plan(
            signature="index_hash",
            statement_key=stmt.normalized,
            family="merchant_orders_join",
            label="merchant_id 索引扫描 + merchants 哈希连接",
            access_path="IndexScan(idx_orders_merchant)",
            join_strategy="HashJoin",
            selectivity=selectivity,
            estimated_rows=rows,
            cost_ms=index_scan_cost + hash_cost + sort_cost,
            cost_breakdown={
                "btree_seek": c.index_seek_ms,
                "index_entries": c.index_entry_ms * rows,
                "heap_fetches": c.heap_fetch_ms * rows,
                "hash_build_merchants": c.hash_build_per_row_ms * n_merchants,
                "hash_probe_orders": c.hash_probe_per_row_ms * rows,
                **common_breakdown_sort,
            },
            explain=[
                f"Index Scan using idx_orders_merchant on orders (~{rows:.0f} rows)",
                "Heap fetch matching order rows",
                f"Hash: build merchants hash table (~{n_merchants:.0f} rows)",
                "Hash Join while probing fetched orders",
                "Sort by created_ts DESC and apply LIMIT",
            ],
        )

        seq_hash = Plan(
            signature="seq_hash",
            statement_key=stmt.normalized,
            family="merchant_orders_join",
            label="orders 全表顺序扫描 + merchants 哈希连接",
            access_path="SeqScan(orders)",
            join_strategy="HashJoin",
            selectivity=selectivity,
            estimated_rows=rows,
            cost_ms=seq_scan_cost + hash_cost + sort_cost,
            cost_breakdown={
                "sequential_scan_orders": c.seq_scan_per_row_ms * n_orders,
                "filter_matching_rows": c.seq_filter_per_output_ms * rows,
                "hash_build_merchants": c.hash_build_per_row_ms * n_merchants,
                "hash_probe_orders": c.hash_probe_per_row_ms * rows,
                **common_breakdown_sort,
            },
            explain=[
                f"Seq Scan on orders (~{n_orders:,} rows), filter merchant_id",
                "Sequential I/O avoids many random heap fetches",
                f"Hash: build merchants hash table (~{n_merchants:.0f} rows)",
                "Hash Join matching orders",
                "Sort by created_ts DESC and apply LIMIT",
            ],
        )

        seq_nl = Plan(
            signature="seq_nl",
            statement_key=stmt.normalized,
            family="merchant_orders_join",
            label="orders 全表扫描 + 嵌套循环 merchants 主键查询",
            access_path="SeqScan(orders)",
            join_strategy="NestedLoop + PK",
            selectivity=selectivity,
            estimated_rows=rows,
            cost_ms=seq_scan_cost + nl_cost + sort_cost,
            cost_breakdown={
                "sequential_scan_orders": c.seq_scan_per_row_ms * n_orders,
                "filter_matching_rows": c.seq_filter_per_output_ms * rows,
                "nested_loop_lookups": nl_cost,
                **common_breakdown_sort,
            },
            explain=[
                "Seq Scan on orders with merchant_id filter",
                "For every matching order, Nested Loop into merchants PK",
                "Sort and LIMIT",
            ],
        )
        return {
            "index_nl": index_nl,
            "index_hash": index_hash,
            "seq_hash": seq_hash,
            "seq_nl": seq_nl,
        }

    def _aggregate_plans(self, stmt: Statement, selectivity: float) -> Dict[str, Plan]:
        n_orders, _, rows = self._counts(selectivity)
        c = self.cost
        index_cost = c.index_seek_ms + (c.index_entry_ms + c.heap_fetch_ms) * rows
        seq_cost = c.seq_scan_per_row_ms * n_orders + c.seq_filter_per_output_ms * rows
        agg_cost = c.aggregate_per_row_ms * rows
        return {
            "index_agg": Plan(
                signature="index_agg",
                statement_key=stmt.normalized,
                family="merchant_orders_aggregate",
                label="merchant_id 索引扫描后哈希聚合",
                access_path="IndexScan(idx_orders_merchant)",
                join_strategy="HashAggregate",
                selectivity=selectivity,
                estimated_rows=rows,
                cost_ms=index_cost + agg_cost,
                cost_breakdown={
                    "index_and_heap": index_cost,
                    "aggregate": agg_cost,
                },
                explain=[
                    f"Index Scan idx_orders_merchant (~{rows:.0f} rows)",
                    "Fetch rows and HashAggregate by status",
                ],
            ),
            "seq_agg": Plan(
                signature="seq_agg",
                statement_key=stmt.normalized,
                family="merchant_orders_aggregate",
                label="orders 全表扫描后哈希聚合",
                access_path="SeqScan(orders)",
                join_strategy="HashAggregate",
                selectivity=selectivity,
                estimated_rows=rows,
                cost_ms=seq_cost + agg_cost,
                cost_breakdown={
                    "sequential_scan": c.seq_scan_per_row_ms * n_orders,
                    "filter": c.seq_filter_per_output_ms * rows,
                    "aggregate": agg_cost,
                },
                explain=[
                    f"Seq Scan orders (~{n_orders:,} rows)",
                    "Filter merchant_id then HashAggregate by status",
                ],
            ),
        }

    def _pk_plans(self, stmt: Statement, selectivity: float) -> Dict[str, Plan]:
        n_orders, _, rows = self._counts(selectivity)
        c = self.cost
        return {
            "pk_point": Plan(
                signature="pk_point",
                statement_key=stmt.normalized,
                family="order_primary_key",
                label="orders 主键唯一索引点查",
                access_path="Unique IndexScan(orders_pkey)",
                join_strategy="none",
                selectivity=selectivity,
                estimated_rows=rows,
                cost_ms=c.pk_seek_ms + c.pk_fetch_ms,
                cost_breakdown={"pk_seek": c.pk_seek_ms, "heap_fetch": c.pk_fetch_ms},
                explain=["Unique index seek on orders_pkey", "Fetch one heap row"],
            ),
            "seq_scan": Plan(
                signature="seq_scan",
                statement_key=stmt.normalized,
                family="order_primary_key",
                label="orders 全表扫描",
                access_path="SeqScan(orders)",
                join_strategy="none",
                selectivity=selectivity,
                estimated_rows=rows,
                cost_ms=c.seq_scan_per_row_ms * n_orders,
                cost_breakdown={"sequential_scan": c.seq_scan_per_row_ms * n_orders},
                explain=[f"Seq Scan all {n_orders:,} orders", "Filter id = ?"],
            ),
        }

    def _generic_plans(self, stmt: Statement, selectivity: float) -> Dict[str, Plan]:
        n_orders = self.stats.tables["orders"].row_count
        rows = max(1.0, selectivity * n_orders)
        return {
            "seq_scan": Plan(
                signature="seq_scan",
                statement_key=stmt.normalized,
                family="generic",
                label="全表扫描",
                access_path="SeqScan",
                join_strategy="none",
                selectivity=selectivity,
                estimated_rows=rows,
                cost_ms=0.000065 * n_orders,
                cost_breakdown={"sequential_scan": 0.000065 * n_orders},
                explain=["Seq Scan"],
            )
        }

    def cheap_costs(self, stmt: Statement, selectivity: float) -> Dict[str, float]:
        """Numeric candidate costs without building explain-plan objects."""
        family = self.family(stmt)
        s = max(1e-12, min(1.0, selectivity))
        n_orders = self.stats.tables[stmt.from_table].row_count
        n = max(1.0, s * n_orders)
        c = self.cost
        if family == "merchant_orders_join":
            n_merchants = float(self.stats.tables["merchants"].row_count)
            index_access = c.index_seek_ms + (c.index_entry_ms + c.heap_fetch_ms) * n
            seq_access = c.seq_scan_per_row_ms * n_orders + c.seq_filter_per_output_ms * n
            nl = c.nl_lookup_ms * n
            hash_join = c.hash_build_per_row_ms * n_merchants + c.hash_probe_per_row_ms * n
            return {
                "index_nl": index_access + nl,
                "index_hash": index_access + hash_join,
                "seq_hash": seq_access + hash_join,
                "seq_nl": seq_access + nl,
            }
        if family == "merchant_orders_aggregate":
            index_access = c.index_seek_ms + (c.index_entry_ms + c.heap_fetch_ms) * n
            seq_access = c.seq_scan_per_row_ms * n_orders + c.seq_filter_per_output_ms * n
            return {
                "index_agg": index_access + c.aggregate_per_row_ms * n,
                "seq_agg": seq_access + c.aggregate_per_row_ms * n,
            }
        if family == "order_primary_key":
            pk_cost = c.pk_seek_ms + c.pk_fetch_ms
            return {
                "pk_point": pk_cost,
                "seq_scan": c.seq_scan_per_row_ms * n_orders,
            }
        return {"seq_scan": c.seq_scan_per_row_ms * n_orders}

    def cheap_winner(self, stmt: Statement, selectivity: float) -> str:
        costs = self.cheap_costs(stmt, selectivity)
        return min(costs, key=costs.get)  # type: ignore[arg-type]

    def analyze_sensitivity(self, stmt: Statement) -> Dict[str, Any]:
        """Check whether histogram regions imply different optimal plans."""
        family = self.family(stmt)
        if family == "merchant_orders_join":
            hist = self.stats.columns[("orders", "merchant_id")].histogram
        elif family == "merchant_orders_aggregate":
            hist = self.stats.columns[("orders", "merchant_id")].histogram
        elif family == "order_primary_key":
            hist = self.stats.columns[("orders", "id")].histogram
        else:
            hist = None

        probe_results = []
        winners = set()
        if hist:
            for probe in hist.probe_values():
                s = max(1e-12, min(1.0, float(probe["selectivity"])))
                winner = self.cheap_winner(stmt, s)
                probe_cost = self.cheap_costs(stmt, s)[winner]
                winners.add(winner)
                probe_results.append({**probe, "winner": winner, "cost_ms": probe_cost})

        # A dense log sweep is used to obtain stable tier boundaries. Histogram
        # probes above are the formal parameter-sensitivity evidence.
        n_orders = max(1, self.stats.tables[stmt.from_table].row_count)
        min_s = 0.5 / n_orders
        grid = [
            math.exp(math.log(min_s) + (math.log(1.0) - math.log(min_s)) * i / 119)
            for i in range(120)
        ]
        grid_winners = [self.cheap_winner(stmt, s) for s in grid]
        sweep_winners = set(grid_winners)
        sensitive = len(winners | sweep_winners) > 1
        return {
            "sensitive": sensitive,
            "histogram_winners": sorted(winners),
            "sweep_winners": sorted(sweep_winners),
            "probes": probe_results,
            "grid": grid,
            "grid_winners": grid_winners,
            "min_selectivity": min_s,
        }

    def build_tiers(self, stmt: Statement, max_tiers: int = 4) -> Tuple[List[Tier], Dict[str, Any]]:
        analysis = self.analyze_sensitivity(stmt)
        grid: List[float] = analysis["grid"]
        winners: List[str] = analysis["grid_winners"]
        min_s: float = analysis["min_selectivity"]

        segments: List[Tuple[int, int, str]] = []
        start = 0
        for i in range(1, len(winners) + 1):
            if i == len(winners) or winners[i] != winners[start]:
                segments.append((start, i - 1, winners[start]))
                if i < len(winners):
                    start = i

        boundaries: List[float] = []
        for seg_index in range(len(segments) - 1):
            left_idx = segments[seg_index][1]
            right_idx = segments[seg_index + 1][0]
            lo = grid[left_idx]
            hi = grid[right_idx]
            left_winner = winners[left_idx]
            # Refine crossover in log selectivity space.
            for _ in range(28):
                mid = math.sqrt(lo * hi)
                mid_winner = self.cheap_winner(stmt, mid)
                if mid_winner == left_winner:
                    lo = mid
                else:
                    hi = mid
            boundaries.append(math.sqrt(lo * hi))

        raw_ranges: List[Tuple[float, float, str]] = []
        previous = 0.0
        for index, boundary in enumerate(boundaries):
            signature = segments[index][2]
            raw_ranges.append((previous, boundary, signature))
            previous = boundary
        raw_ranges.append((previous, 1.0, segments[-1][2]))

        # If a model ever produces more regions than allowed, keep the broadest
        # low/mid/high regions by merging adjacent same plans and truncating.
        merged: List[Tuple[float, float, str]] = []
        for lo, hi, sig in raw_ranges:
            if merged and merged[-1][2] == sig:
                merged[-1] = (merged[-1][0], hi, sig)
            else:
                merged.append((lo, hi, sig))
        if len(merged) > max_tiers:
            # Preserve first and last; merge surplus middle bands upward.
            first = merged[0]
            last = merged[-1]
            middle = merged[1:-1]
            kept_middle = middle[: max_tiers - 2]
            merged = [first] + kept_middle + [last]

        tiers: List[Tier] = []
        hist = None
        if self.family(stmt) in {"merchant_orders_join", "merchant_orders_aggregate"}:
            hist = self.stats.columns[("orders", "merchant_id")].histogram

        for idx, (lo, hi, signature) in enumerate(merged):
            lower_for_rep = max(lo if lo > 0 else min_s, min_s)
            upper_for_rep = max(hi, lower_for_rep * 1.0001)
            representative = math.sqrt(lower_for_rep * upper_for_rep) if hi < 1 else min(
                0.25, math.sqrt(lower_for_rep * max(lower_for_rep * 2, hi))
            )
            representative = min(max(representative, lower_for_rep), hi if hi > lo else 1.0)
            if hi >= 1.0:
                representative = max(representative, min(0.2, math.sqrt(lower_for_rep * 0.5)))
            plan = self.optimize_for_selectivity(stmt, representative)
            plan.tier_id = f"t{idx + 1}"
            tier = Tier(
                tier_id=f"t{idx + 1}",
                label=self._tier_label(idx, len(merged), lo, hi),
                lower_selectivity=lo,
                upper_selectivity=hi,
                plan_signature=signature,
                representative_selectivity=representative,
                plan=plan,
            )
            plan.tier_label = tier.label
            if hist:
                tier.probe_sources = [
                    p["source"] for p in analysis["probes"] if p.get("winner") == signature
                ][:6]
            tiers.append(tier)

        analysis.pop("grid", None)
        analysis.pop("grid_winners", None)
        return tiers, analysis

    @staticmethod
    def _tier_label(index: int, total: int, lo: float, hi: float) -> str:
        if total == 1:
            return "统一档"
        if index == 0:
            return "小商户 / 低选择率档"
        if index == total - 1:
            return "大商户 / 高选择率档"
        return f"中等商户 / 中选择率档 {index}"
