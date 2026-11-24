from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .database import Database


MAIN_SQL_TEMPLATE = """
SELECT o.id, o.amount, o.status, o.created_at, m.name, m.tier, m.region
FROM orders o
JOIN merchants m ON o.merchant_id = m.id
WHERE o.merchant_id = {merchant_id}
ORDER BY o.created_at DESC
LIMIT 20
""".strip()

POINT_SQL_TEMPLATE = """
SELECT id, merchant_id, amount, status, created_at
FROM orders
WHERE id = {order_id}
""".strip()

AGG_SQL_TEMPLATE = """
SELECT merchant_id, status, COUNT(*) AS cnt, SUM(amount) AS revenue
FROM orders
WHERE merchant_id = {merchant_id}
GROUP BY merchant_id, status
""".strip()


@dataclass
class WorkloadRequest:
    seq: int
    kind: str
    sql: str
    merchant_id: Optional[int] = None
    order_id: Optional[int] = None
    scripted: bool = False

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "sql": self.sql,
            "merchant_id": self.merchant_id,
            "order_id": self.order_id,
            "scripted": self.scripted,
        }


def event_schedule(call_count: int, merchant_count: int) -> Dict[str, Any]:
    first_restart = max(2, call_count // 3)
    second_restart = max(first_restart + 2, call_count * 2 // 3)
    stats_after = max(1, int(call_count * 0.78))
    small_id = int(merchant_count * 0.88) + 1
    medium_id = max(2, int(merchant_count * 0.06))
    burst_id = max(2, int(merchant_count * 0.15))
    return {
        "restart_after": [first_restart, second_restart],
        "stats_after": stats_after,
        "scripted": {
            1: ("main", small_id, "开场第一个调用故意落在小商户"),
            first_restart + 1: ("main", 1, "重启后第一个调用落在头部大商户"),
            second_restart + 1: ("main", medium_id, "第二次重启后第一个调用落在中型商户"),
            stats_after + 1: ("main", burst_id, "统计信息更新后查询刚暴涨的商户"),
        },
        "burst_merchant_id": burst_id,
        "small_id": small_id,
        "medium_id": medium_id,
    }


def generate_requests(
    db: Database,
    call_count: int = 720,
    seed: int = 20261125,
    traffic_beta: float = 0.42,
) -> List[WorkloadRequest]:
    rng = random.Random(seed)
    merchant_rows = db.merchants.rows
    merchant_count = len(merchant_rows)
    merchant_ids = [row["id"] for row in merchant_rows]
    sizes = [max(1, row.get("order_count_seed", 1)) for row in merchant_rows]
    traffic_weights = [size ** traffic_beta for size in sizes]
    total_orders = len(db.orders.rows)
    schedule = event_schedule(call_count, merchant_count)
    scripted = schedule["scripted"]

    requests: List[WorkloadRequest] = []
    for seq in range(1, call_count + 1):
        if seq in scripted:
            kind, merchant_id, _reason = scripted[seq]
            requests.append(WorkloadRequest(
                seq=seq,
                kind=kind,
                sql=MAIN_SQL_TEMPLATE.format(merchant_id=merchant_id),
                merchant_id=merchant_id,
                scripted=True,
            ))
            continue

        pick = rng.random()
        if pick < 0.62:
            kind = "merchant_orders"
            merchant_id = rng.choices(merchant_ids, weights=traffic_weights, k=1)[0]
            sql = MAIN_SQL_TEMPLATE.format(merchant_id=merchant_id)
        elif pick < 0.84:
            kind = "order_detail"
            order_id = rng.randint(1, total_orders)
            merchant_id = None
            sql = POINT_SQL_TEMPLATE.format(order_id=order_id)
            requests.append(WorkloadRequest(
                seq=seq, kind=kind, sql=sql, order_id=order_id
            ))
            continue
        else:
            kind = "merchant_status_agg"
            merchant_id = rng.choices(merchant_ids, weights=traffic_weights, k=1)[0]
            sql = AGG_SQL_TEMPLATE.format(merchant_id=merchant_id)

        requests.append(WorkloadRequest(
            seq=seq, kind=kind, sql=sql, merchant_id=merchant_id
        ))
    return requests


def data_profile(db: Database) -> Dict[str, Any]:
    sizes = [row.get("order_count_seed", 0) for row in db.merchants.rows]
    if not sizes:
        counts: Dict[int, int] = {}
        for row in db.orders.rows:
            counts[row["merchant_id"]] = counts.get(row["merchant_id"], 0) + 1
        sizes = list(counts.values())
    ordered = sorted(sizes, reverse=True)
    bins = [1, 5, 10, 30, 100, 300, 1000, 3000, 10_000, 100_000]
    distribution = []
    for index, lower in enumerate(bins[:-1]):
        upper = bins[index + 1]
        count = sum(lower <= value < upper for value in sizes)
        distribution.append({"range": f"{lower}-{upper - 1}", "merchants": count})
    distribution.append({
        "range": f"{bins[-1]}+",
        "merchants": sum(value >= bins[-1] for value in sizes),
    })
    traffic_beta = 0.42
    weights = [value ** traffic_beta for value in sizes]
    weight_total = sum(weights)
    traffic_bins = []
    for index, lower in enumerate(bins[:-1]):
        upper = bins[index + 1]
        weight = sum(w for value, w in zip(sizes, weights) if lower <= value < upper)
        traffic_bins.append({
            "range": f"{lower}-{upper - 1}",
            "traffic_pct": round(100 * weight / max(1, weight_total), 2),
        })

    return {
        "merchant_count": len(sizes),
        "order_count": len(db.orders.rows),
        "max_orders_per_merchant": max(sizes),
        "min_orders_per_merchant": min(sizes),
        "size_ratio": round(max(sizes) / max(1, min(sizes)), 1),
        "power_law_alpha": 1.5,
        "traffic_weight": "order_count^0.42，表达大客户调用更多但长尾仍有真实流量",
        "merchant_size_distribution": distribution,
        "traffic_distribution": traffic_bins,
        "top_merchants": [
            {"merchant_id": i + 1, "orders": ordered[i]}
            for i in range(min(15, len(ordered)))
        ],
    }
