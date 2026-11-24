from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional


@dataclass
class Table:
    name: str
    column_names: List[str]
    rows: List[Dict[str, Any]] = field(default_factory=list)
    row_width_bytes: int = 96


@dataclass
class Index:
    name: str
    table_name: str
    columns: List[str]
    unique: bool = False
    entries: Dict[Any, Any] = field(default_factory=dict)

    def add(self, key: Any, row_position: int) -> None:
        if self.unique:
            self.entries[key] = row_position
        else:
            self.entries.setdefault(key, []).append(row_position)


class Database:
    def __init__(self, merchants: Table, orders: Table, indexes: Dict[str, Index]):
        self.tables = {"merchants": merchants, "orders": orders}
        self.indexes = indexes
        self.catalog_stats: Any = None
        self.stats_version = 0

    @property
    def merchants(self) -> Table:
        return self.tables["merchants"]

    @property
    def orders(self) -> Table:
        return self.tables["orders"]

    def rebuild_indexes(self) -> None:
        for index in self.indexes.values():
            index.entries.clear()
            table = self.tables[index.table_name]
            for position, row in enumerate(table.rows):
                key = row[index.columns[0]] if len(index.columns) == 1 else tuple(
                    row[column] for column in index.columns
                )
                index.add(key, position)

    def insert_order_rows(self, rows: List[Dict[str, Any]]) -> int:
        start_id = max((row["id"] for row in self.orders.rows), default=0) + 1
        inserted = 0
        for row in rows:
            row["id"] = start_id + inserted
            position = len(self.orders.rows)
            self.orders.rows.append(row)
            for index in self.indexes.values():
                if index.table_name != "orders":
                    continue
                key = row[index.columns[0]]
                index.add(key, position)
            inserted += 1
        return inserted


def power_law_sizes(
    merchant_count: int = 800,
    total_orders: int = 160_000,
    alpha: float = 1.5,
) -> List[int]:
    raw = [(index + 1) ** (-alpha) for index in range(merchant_count)]
    scale = total_orders / sum(raw)
    sizes = [max(1, int(scale * weight)) for weight in raw]
    # Correct rounding drift while preserving the power-law shape.
    drift = total_orders - sum(sizes)
    sizes[0] += drift
    return sizes


def build_demo_database(
    seed: int = 16251,
    merchant_count: int = 800,
    total_orders: int = 160_000,
    alpha: float = 1.5,
) -> Database:
    rng = random.Random(seed)
    sizes = power_law_sizes(merchant_count, total_orders, alpha)

    merchants = Table(
        name="merchants",
        column_names=["id", "name", "tier", "industry", "region"],
        row_width_bytes=160,
    )
    for merchant_id, order_count in enumerate(sizes, start=1):
        if order_count >= 2000:
            tier = "KA"
        elif order_count >= 200:
            tier = "growth"
        elif order_count >= 30:
            tier = "active"
        else:
            tier = "long_tail"
        merchants.rows.append({
            "id": merchant_id,
            "name": f"商户-{merchant_id:04d}",
            "tier": tier,
            "industry": rng.choice(["餐饮", "零售", "电商", "服务", "数码", "服饰"]),
            "region": rng.choice(["华东", "华北", "华南", "西南", "华中"]),
            "order_count_seed": order_count,
        })

    base_time = datetime(2026, 11, 25, 10, 0, 0)
    order_records: List[Dict[str, Any]] = []
    statuses = (
        ["completed"] * 58
        + ["paid"] * 18
        + ["shipped"] * 10
        + ["cancelled"] * 8
        + ["refunded"] * 6
    )
    max_age_minutes = 180 * 24 * 60

    for merchant_id, count in enumerate(sizes, start=1):
        for _ in range(count):
            age_minutes = rng.randint(0, max_age_minutes)
            created = base_time - timedelta(minutes=age_minutes)
            # Mostly small orders, with a long tail of larger amounts.
            amount = round(8 + rng.lognormvariate(4.1, 0.85), 2)
            order_records.append({
                "merchant_id": merchant_id,
                "amount": amount,
                "status": rng.choice(statuses),
                "created_ts": max_age_minutes - age_minutes,
                "created_at": created.strftime("%Y-%m-%d %H:%M:%S"),
            })

    # Physical heap order is interleaved, as in a real busy order table.
    rng.shuffle(order_records)
    orders = Table(
        name="orders",
        column_names=["id", "merchant_id", "amount", "status", "created_ts", "created_at"],
        row_width_bytes=112,
    )
    for order_id, record in enumerate(order_records, start=1):
        record["id"] = order_id
        orders.rows.append(record)

    indexes = {
        "orders_pkey": Index("orders_pkey", "orders", ["id"], unique=True),
        "idx_orders_merchant": Index(
            "idx_orders_merchant", "orders", ["merchant_id"], unique=False
        ),
        "merchants_pkey": Index("merchants_pkey", "merchants", ["id"], unique=True),
    }
    db = Database(merchants=merchants, orders=orders, indexes=indexes)
    db.rebuild_indexes()
    return db


def make_burst_orders(
    db: Database,
    merchant_id: int,
    count: int,
    seed: int = 991,
) -> List[Dict[str, Any]]:
    rng = random.Random(seed + merchant_id)
    base_time = datetime(2026, 11, 25, 10, 0, 0)
    max_age_minutes = 180 * 24 * 60
    rows = []
    for _ in range(count):
        age_minutes = rng.randint(0, 60 * 24 * 14)
        created = base_time - timedelta(minutes=age_minutes)
        rows.append({
            "merchant_id": merchant_id,
            "amount": round(8 + rng.lognormvariate(4.4, 0.9), 2),
            "status": rng.choice(["completed", "paid", "shipped", "refunded"]),
            "created_ts": max_age_minutes - age_minutes,
            "created_at": created.strftime("%Y-%m-%d %H:%M:%S"),
        })
    return rows
