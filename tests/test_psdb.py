from __future__ import annotations

import unittest

from psdb.benchmark import run_benchmark_run
from psdb.cache import CachedPlan, PlanCache
from psdb.database import build_demo_database
from psdb.engine import QueryEngine
from psdb.optimizer import Optimizer
from psdb.sql import parse_sql
from psdb.stats import collect_stats
from psdb.workload import MAIN_SQL_TEMPLATE


def make_engine(merchants=80, orders=8000, cache_entries=6, strategy="adaptive"):
    db = build_demo_database(
        seed=42,
        merchant_count=merchants,
        total_orders=orders,
        alpha=1.5,
    )
    collect_stats(db, version=1, note="test")
    engine = QueryEngine(db, strategy=strategy, max_entries=cache_entries)
    return db, engine


class ParserTests(unittest.TestCase):
    def test_literals_normalize_to_same_key(self):
        sql1 = MAIN_SQL_TEMPLATE.format(merchant_id=7)
        sql2 = MAIN_SQL_TEMPLATE.format(merchant_id=999)
        s1 = parse_sql(sql1)
        s2 = parse_sql(sql2)
        self.assertEqual(s1.normalized, s2.normalized)
        self.assertIn("?", s1.normalized)
        self.assertEqual(s1.predicate_values("orders", "merchant_id"), [7])
        self.assertEqual(s2.predicate_values("orders", "merchant_id"), [999])


class OptimizerCacheTests(unittest.TestCase):
    def test_small_and_large_merchants_get_different_optimal_plans(self):
        _db, engine = make_engine()
        small = engine.execute_sql(MAIN_SQL_TEMPLATE.format(merchant_id=80), seq=1)
        large = engine.execute_sql(MAIN_SQL_TEMPLATE.format(merchant_id=1), seq=2)

        self.assertEqual(small.plan_signature, "index_nl")
        self.assertEqual(large.plan_signature, "seq_hash")
        self.assertNotEqual(small.tier_id, large.tier_id)
        self.assertTrue(small.is_parameter_sensitive)

        runtime = next(iter(engine.runtimes.values()))
        self.assertGreaterEqual(len(runtime.tiers), 2)
        winners = {tier.plan_signature for tier in runtime.tiers}
        self.assertIn("index_nl", winners)
        self.assertIn("seq_hash", winners)

    def test_second_similar_call_is_cache_hit(self):
        _db, engine = make_engine()
        engine.execute_sql(MAIN_SQL_TEMPLATE.format(merchant_id=79), seq=1)
        second = engine.execute_sql(MAIN_SQL_TEMPLATE.format(merchant_id=80), seq=2)
        self.assertEqual(second.cache_status, "HIT")
        self.assertEqual(second.tier_id, "t1")

    def test_classic_single_plan_reuses_first_small_plan_for_large_merchant(self):
        _db, engine = make_engine(strategy="single")
        first = engine.execute_sql(MAIN_SQL_TEMPLATE.format(merchant_id=80), seq=1)
        second = engine.execute_sql(MAIN_SQL_TEMPLATE.format(merchant_id=1), seq=2)
        self.assertEqual(first.plan_signature, "index_nl")
        self.assertEqual(second.cache_status, "SINGLE_HIT")
        self.assertEqual(second.plan_signature, "index_nl")
        optimal = Optimizer(engine.stats).optimize(
            parse_sql(MAIN_SQL_TEMPLATE.format(merchant_id=1))
        ).signature
        self.assertEqual(optimal, "seq_hash")
        self.assertNotEqual(second.plan_signature, optimal)

    def test_policy_can_force_one_plan_and_disable_cache(self):
        _db, engine = make_engine()
        key = parse_sql(MAIN_SQL_TEMPLATE.format(merchant_id=1)).normalized
        engine.policies.set(key, "pinned", "seq_hash")
        forced = engine.execute_sql(MAIN_SQL_TEMPLATE.format(merchant_id=80), seq=1)
        self.assertEqual(forced.plan_signature, "seq_hash")
        self.assertIn("PINNED", forced.cache_status)

        engine.policies.set(key, "disabled")
        disabled = engine.execute_sql(MAIN_SQL_TEMPLATE.format(merchant_id=80), seq=2)
        self.assertEqual(disabled.cache_status, "BYPASS")
        self.assertEqual(engine.cache.metrics.bypasses, 1)

    def test_lru_eviction_respects_capacity(self):
        cache = PlanCache(max_entries=2)

        def entry(index, pinned=False):
            return CachedPlan(
                cache_key=f"k{index}",
                statement_key="s",
                tier_id=str(index),
                plan=None,  # type: ignore[arg-type]
                pinned=pinned,
            )

        cache.put(entry(1))
        cache.put(entry(2))
        cache.get("k1", 1)  # k2 becomes LRU
        cache.put(entry(3))
        self.assertEqual(cache.size(), 2)
        self.assertIsNone(cache.peek("k2"))
        self.assertEqual(cache.metrics.evictions, 1)
        self.assertIsNotNone(cache.peek("k1"))

    def test_pinned_entries_are_protected_from_lru_eviction(self):
        cache = PlanCache(max_entries=2)
        cache.put(CachedPlan("p", "s", "p", None, pinned=True))  # type: ignore[arg-type]
        cache.put(CachedPlan("a", "s", "a", None))  # type: ignore[arg-type]
        inserted = cache.put(CachedPlan("b", "s", "b", None))  # type: ignore[arg-type]
        self.assertTrue(inserted)
        self.assertIsNotNone(cache.peek("p"))
        self.assertIsNone(cache.peek("a"))

    def test_stats_update_invalidates_related_cache(self):
        db, engine = make_engine()
        engine.execute_sql(MAIN_SQL_TEMPLATE.format(merchant_id=1), seq=1)
        self.assertGreater(engine.cache.size(), 0)
        collect_stats(db, note="new stats")
        invalidated = engine.on_stats_updated()
        self.assertGreater(invalidated, 0)
        self.assertEqual(engine.cache.size(), 0)
        runtime = next(iter(engine.runtimes.values()))
        self.assertTrue(runtime.needs_rebuild)
        self.assertGreater(len(runtime.tiers), 0)

    def test_restart_keeps_last_tier_picture_but_clears_cache(self):
        _db, engine = make_engine()
        engine.execute_sql(MAIN_SQL_TEMPLATE.format(merchant_id=1), seq=1)
        runtime = next(iter(engine.runtimes.values()))
        tier_count = len(runtime.tiers)
        removed = engine.restart()
        self.assertGreater(removed, 0)
        self.assertEqual(engine.cache.size(), 0)
        self.assertEqual(len(runtime.tiers), tier_count)
        self.assertTrue(runtime.needs_rebuild)
        trace = engine.execute_sql(MAIN_SQL_TEMPLATE.format(merchant_id=1), seq=2)
        self.assertFalse(runtime.needs_rebuild)
        self.assertIn(trace.cache_status, {"HIT", "TIER_REBUILD"})

    def test_executor_returns_limited_sorted_rows(self):
        _db, engine = make_engine()
        sql = MAIN_SQL_TEMPLATE.format(merchant_id=1).replace("LIMIT 20", "LIMIT 5")
        trace = engine.execute_sql(sql, seq=1, sample_rows=True)
        self.assertEqual(trace.result_rows, 5)
        dates = [row["created_at"] for row in trace.sample_rows]
        self.assertEqual(dates, sorted(dates, reverse=True))


class BenchmarkSmokeTests(unittest.TestCase):
    def test_tiny_benchmark_has_three_strategies_and_events(self):
        run = run_benchmark_run(
            calls=18,
            max_cache_entries=4,
            merchant_count=40,
            total_orders=3000,
            burst_size=300,
        )
        report = run.report
        self.assertEqual(set(report["traces"]), {"adaptive", "single", "reopt"})
        for traces in report["traces"].values():
            self.assertEqual(len(traces), 18)
        types = [event["type"] for event in report["events"]]
        self.assertIn("restart", types)
        self.assertIn("stats_update", types)
        self.assertGreaterEqual(len(report["statements"]), 2)


if __name__ == "__main__":
    unittest.main()
