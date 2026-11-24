from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .optimizer import Plan
from .sql import ColRef, Statement


@dataclass
class ExecutionResult:
    rows: List[Dict[str, Any]]
    row_count: int
    scanned_rows: int = 0
    fetched_rows: int = 0
    joined_rows: int = 0
    notes: List[str] = field(default_factory=list)


class Executor:
    def __init__(self, database: Any):
        self.db = database

    def execute(self, stmt: Statement, plan: Plan) -> ExecutionResult:
        if plan.family == "merchant_orders_join":
            return self._execute_main_join(stmt, plan)
        if plan.family == "merchant_orders_aggregate":
            return self._execute_aggregate(stmt, plan)
        if plan.family == "order_primary_key":
            return self._execute_pk(stmt, plan)
        return self._execute_generic(stmt, plan)

    def _merchant_id(self, stmt: Statement) -> int:
        values = stmt.predicate_values("orders", "merchant_id")
        if not values:
            raise ValueError("缺少 orders.merchant_id 谓词")
        return int(values[0])

    def _order_rows_by_index(self, merchant_id: int) -> tuple:
        index = self.db.indexes["idx_orders_merchant"]
        positions = list(index.entries.get(merchant_id, []))
        rows = [self.db.orders.rows[pos] for pos in positions]
        return rows, len(positions), len(positions)

    def _order_rows_by_seq(self, merchant_id: int) -> tuple:
        scanned = len(self.db.orders.rows)
        rows = [row for row in self.db.orders.rows if row.get("merchant_id") == merchant_id]
        return rows, scanned, len(rows)

    def _execute_main_join(self, stmt: Statement, plan: Plan) -> ExecutionResult:
        merchant_id = self._merchant_id(stmt)
        if plan.access_path.startswith("IndexScan"):
            order_rows, scanned, fetched = self._order_rows_by_index(merchant_id)
        else:
            order_rows, scanned, fetched = self._order_rows_by_seq(merchant_id)

        merchant_table = self.db.merchants.rows
        merchant_positions = self.db.indexes["merchants_pkey"].entries

        joined: List[Dict[str, Any]] = []
        if plan.join_strategy.startswith("NestedLoop"):
            # Deliberately perform one primary-key lookup per order row.
            for order in order_rows:
                position = merchant_positions[order["merchant_id"]]
                merchant = merchant_table[position]
                joined.append(self._joined_row(order, merchant))
        elif plan.join_strategy == "HashJoin":
            hash_table = {
                merchant["id"]: merchant
                for merchant in merchant_table
            }
            for order in order_rows:
                merchant = hash_table[order["merchant_id"]]
                joined.append(self._joined_row(order, merchant))
        else:
            raise ValueError(f"未知连接策略: {plan.join_strategy}")

        descending = True
        sort_column = "created_ts"
        if stmt.order_by:
            sort_column = stmt.order_by[0].column.name
            descending = stmt.order_by[0].descending
        joined.sort(key=lambda row: row.get(sort_column), reverse=descending)

        limit = stmt.limit if stmt.limit is not None else 20
        limited = joined[:limit]
        projected = [self._project(stmt, row) for row in limited]
        return ExecutionResult(
            rows=projected,
            row_count=len(projected),
            scanned_rows=scanned,
            fetched_rows=fetched,
            joined_rows=len(joined),
            notes=[f"返回最近 {len(projected)} 条；排序前共 {len(joined)} 条订单"],
        )

    @staticmethod
    def _joined_row(order: Dict[str, Any], merchant: Dict[str, Any]) -> Dict[str, Any]:
        row: Dict[str, Any] = {}
        for key, value in order.items():
            row[f"o.{key}"] = value
            row[key] = value
        for key, value in merchant.items():
            row[f"m.{key}"] = value
            # Do not overwrite order columns such as id.
            row.setdefault(key, value)
            row[f"merchant_{key}"] = value
        return row

    def _project(self, stmt: Statement, row: Dict[str, Any]) -> Dict[str, Any]:
        if len(stmt.items) == 1 and stmt.items[0].raw == "*":
            return {k: v for k, v in row.items() if "." not in k and not k.startswith("merchant_")}
        result = {}
        for item in stmt.items:
            if item.aggregate:
                continue
            col = item.column
            if col is None:
                continue
            key = col.canonical()
            output_name = item.alias or col.name
            result[output_name] = row.get(key, row.get(col.name))
        return result

    def _execute_aggregate(self, stmt: Statement, plan: Plan) -> ExecutionResult:
        merchant_id = self._merchant_id(stmt)
        if plan.access_path.startswith("IndexScan"):
            order_rows, scanned, fetched = self._order_rows_by_index(merchant_id)
        else:
            order_rows, scanned, fetched = self._order_rows_by_seq(merchant_id)

        groups: Dict[Any, Dict[str, Any]] = {}
        for order in order_rows:
            key = tuple(order[col.name] for col in stmt.group_by) if stmt.group_by else (order["status"],)
            group = groups.setdefault(key, {
                "merchant_id": merchant_id,
                "status": order["status"],
                "cnt": 0,
                "revenue": 0.0,
            })
            group["cnt"] += 1
            group["revenue"] += float(order["amount"])

        rows = list(groups.values())
        for row in rows:
            row["revenue"] = round(row["revenue"], 2)
        rows.sort(key=lambda r: (-r["cnt"], r["status"]))
        return ExecutionResult(
            rows=rows,
            row_count=len(rows),
            scanned_rows=scanned,
            fetched_rows=fetched,
            joined_rows=len(order_rows),
        )

    def _execute_pk(self, stmt: Statement, plan: Plan) -> ExecutionResult:
        values = stmt.predicate_values("orders", "id")
        order_id = int(values[0])
        if plan.signature == "pk_point":
            position = self.db.indexes["orders_pkey"].entries[order_id]
            row = self.db.orders.rows[position]
            scanned, fetched = 1, 1
        else:
            scanned = len(self.db.orders.rows)
            matches = [row for row in self.db.orders.rows if row["id"] == order_id]
            row = matches[0]
            fetched = 1
        projected = [self._project(stmt, row)] if stmt.items else [row]
        return ExecutionResult(
            rows=projected,
            row_count=1,
            scanned_rows=scanned,
            fetched_rows=fetched,
        )

    def _execute_generic(self, stmt: Statement, plan: Plan) -> ExecutionResult:
        rows = list(self.db.orders.rows[: stmt.limit or 100])
        return ExecutionResult(rows=rows, row_count=len(rows), scanned_rows=len(self.db.orders.rows))
