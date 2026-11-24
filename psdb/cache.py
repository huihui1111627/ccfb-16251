from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from .optimizer import Plan


@dataclass
class CachedPlan:
    cache_key: str
    statement_key: str
    tier_id: str
    plan: Plan
    created_seq: int = 0
    last_used_seq: int = 0
    uses: int = 0
    pinned: bool = False
    tables: Tuple[str, ...] = ("orders", "merchants")

    def touch(self, call_seq: int) -> None:
        self.uses += 1
        self.last_used_seq = call_seq

    def to_dict(self) -> dict:
        return {
            "cache_key": self.cache_key,
            "statement_key": self.statement_key,
            "tier_id": self.tier_id,
            "signature": self.plan.signature,
            "label": self.plan.label,
            "created_seq": self.created_seq,
            "last_used_seq": self.last_used_seq,
            "uses": self.uses,
            "pinned": self.pinned,
            "tables": list(self.tables),
            "plan": self.plan.to_dict(),
        }


@dataclass
class CacheMetrics:
    hits: int = 0
    misses: int = 0
    inserts: int = 0
    evictions: int = 0
    invalidations: int = 0
    restarts: int = 0
    bypasses: int = 0

    def to_dict(self) -> dict:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "inserts": self.inserts,
            "evictions": self.evictions,
            "invalidations": self.invalidations,
            "restarts": self.restarts,
            "bypasses": self.bypasses,
            "hit_ratio": (
                self.hits / max(1, self.hits + self.misses + self.bypasses)
            ),
        }


class PlanCache:
    """LRU cache keyed by normalized SQL text plus an optional selectivity tier."""

    def __init__(self, max_entries: int = 8):
        self.max_entries = max_entries
        self.entries: "OrderedDict[str, CachedPlan]" = OrderedDict()
        self.metrics = CacheMetrics()

    @staticmethod
    def make_key(statement_key: str, tier_id: str = "default") -> str:
        return f"{statement_key}||{tier_id}"

    def get(self, cache_key: str, call_seq: int) -> Optional[CachedPlan]:
        cached = self.entries.get(cache_key)
        if cached is None:
            self.metrics.misses += 1
            return None
        cached.touch(call_seq)
        self.entries.move_to_end(cache_key)
        self.metrics.hits += 1
        return cached

    def peek(self, cache_key: str) -> Optional[CachedPlan]:
        return self.entries.get(cache_key)

    def put(self, cached_plan: CachedPlan) -> bool:
        key = cached_plan.cache_key
        if key in self.entries:
            old = self.entries[key]
            cached_plan.uses = old.uses
            cached_plan.created_seq = old.created_seq
            self.entries[key] = cached_plan
            self.entries.move_to_end(key)
            return True

        # Manual pinned plans are protected. Evict ordinary LRU entries first.
        while len(self.entries) >= self.max_entries:
            victim_key = self._least_recently_used_unpinned()
            if victim_key is None:
                return False
            self.entries.pop(victim_key)
            self.metrics.evictions += 1

        self.entries[key] = cached_plan
        self.metrics.inserts += 1
        return True

    def _least_recently_used_unpinned(self) -> Optional[str]:
        for key, entry in self.entries.items():
            if not entry.pinned:
                return key
        return None

    def set_limit(self, max_entries: int) -> List[str]:
        self.max_entries = max(1, int(max_entries))
        evicted = []
        while len(self.entries) > self.max_entries:
            victim = self._least_recently_used_unpinned()
            if victim is None:
                break
            self.entries.pop(victim)
            self.metrics.evictions += 1
            evicted.append(victim)
        return evicted

    def clear(self, reason: str = "restart") -> int:
        count = len(self.entries)
        self.entries.clear()
        if reason == "restart":
            self.metrics.restarts += 1
        return count

    def invalidate_statement(self, statement_key: str) -> int:
        keys = [key for key, entry in self.entries.items() if entry.statement_key == statement_key]
        for key in keys:
            self.entries.pop(key, None)
        if keys:
            self.metrics.invalidations += len(keys)
        return len(keys)

    def invalidate_for_tables(self, tables: Set[str]) -> int:
        keys = [
            key for key, entry in self.entries.items()
            if set(entry.tables) & set(tables)
        ]
        for key in keys:
            self.entries.pop(key, None)
        if keys:
            self.metrics.invalidations += len(keys)
        return len(keys)

    def size(self) -> int:
        return len(self.entries)

    def to_dict(self) -> dict:
        return {
            "max_entries": self.max_entries,
            "size": len(self.entries),
            "metrics": self.metrics.to_dict(),
            "entries": [entry.to_dict() for entry in reversed(self.entries.values())],
        }


@dataclass
class StatementPolicy:
    statement_key: str
    mode: str = "auto"  # auto | pinned | disabled
    forced_signature: Optional[str] = None
    updated_seq: int = 0

    def to_dict(self) -> dict:
        return {
            "statement_key": self.statement_key,
            "mode": self.mode,
            "forced_signature": self.forced_signature,
            "updated_seq": self.updated_seq,
        }


class PolicyStore:
    """Manual intervention survives restart; plan entries themselves do not."""

    def __init__(self) -> None:
        self.policies: Dict[str, StatementPolicy] = {}

    def get(self, statement_key: str) -> StatementPolicy:
        return self.policies.get(statement_key, StatementPolicy(statement_key))

    def set(
        self,
        statement_key: str,
        mode: str,
        forced_signature: Optional[str] = None,
        call_seq: int = 0,
    ) -> StatementPolicy:
        policy = StatementPolicy(
            statement_key=statement_key,
            mode=mode,
            forced_signature=forced_signature if mode == "pinned" else None,
            updated_seq=call_seq,
        )
        self.policies[statement_key] = policy
        return policy

    def all(self) -> Dict[str, dict]:
        return {key: policy.to_dict() for key, policy in self.policies.items()}
