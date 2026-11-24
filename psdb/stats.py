from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple


@dataclass
class Bucket:
    """An equi-depth-ish histogram bucket over a scalar column."""

    lower: Any
    upper: Any
    row_count: int
    ndv: int

    def to_dict(self) -> dict:
        return {
            "lower": self.lower,
            "upper": self.upper,
            "row_count": self.row_count,
            "ndv": self.ndv,
        }


@dataclass
class Histogram:
    table: str
    column: str
    row_count: int
    null_count: int = 0
    mcv: Dict[Any, int] = field(default_factory=dict)
    buckets: List[Bucket] = field(default_factory=list)
    kind: str = "mcv-plus-equidepth"

    @property
    def non_null_rows(self) -> int:
        return max(0, self.row_count - self.null_count)

    def equality_rows(self, value: Any) -> float:
        """Estimate the number of rows returned by ``column = value``."""
        if value is None:
            return float(self.null_count)
        if value in self.mcv:
            return float(self.mcv[value])

        for bucket in self.buckets:
            if bucket.lower <= value <= bucket.upper:
                # Uniform distribution inside a bucket is the classic estimate.
                distinct = max(1, bucket.ndv)
                return max(1.0, bucket.row_count / distinct)

        # Values just outside collected endpoints are rare, but not zero.
        return max(1.0, self.non_null_rows / max(1, self.row_count))

    def selectivity(self, value: Any) -> float:
        if self.row_count <= 0:
            return 0.0
        return min(1.0, self.equality_rows(value) / self.row_count)

    def probe_values(self) -> List[Dict[str, Any]]:
        """Representative values/ranges used for parameter sensitivity detection."""
        probes: List[Dict[str, Any]] = []
        for value, count in sorted(self.mcv.items(), key=lambda kv: -kv[1]):
            probes.append({
                "source": "MCV",
                "value": value,
                "selectivity": count / max(1, self.row_count),
            })
        for index, bucket in enumerate(self.buckets):
            # Bucket endpoints and its uniform average expose different ranges.
            avg_sel = (bucket.row_count / max(1, bucket.ndv)) / max(1, self.row_count)
            probes.append({
                "source": f"bucket-{index + 1}-avg",
                "value": bucket.lower,
                "selectivity": avg_sel,
                "bucket": index + 1,
                "lower": bucket.lower,
                "upper": bucket.upper,
            })
        return probes

    def to_dict(self) -> dict:
        return {
            "table": self.table,
            "column": self.column,
            "row_count": self.row_count,
            "null_count": self.null_count,
            "kind": self.kind,
            "mcv": [
                {"value": value, "rows": rows, "selectivity": rows / max(1, self.row_count)}
                for value, rows in sorted(self.mcv.items(), key=lambda kv: -kv[1])[:20]
            ],
            "buckets": [b.to_dict() for b in self.buckets],
        }


@dataclass
class ColumnStats:
    table: str
    name: str
    ndv: int
    null_count: int
    histogram: Optional[Histogram] = None

    def to_dict(self) -> dict:
        return {
            "table": self.table,
            "name": self.name,
            "ndv": self.ndv,
            "null_count": self.null_count,
            "histogram": self.histogram.to_dict() if self.histogram else None,
        }


@dataclass
class TableStats:
    name: str
    row_count: int
    page_count: int
    row_width_bytes: int = 96

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "row_count": self.row_count,
            "page_count": self.page_count,
            "row_width_bytes": self.row_width_bytes,
        }


@dataclass
class CatalogStats:
    version: int
    tables: Dict[str, TableStats]
    columns: Dict[Tuple[str, str], ColumnStats]
    collected_at_call: int = 0
    note: str = ""

    def selectivity(self, table: str, column: str, value: Any) -> float:
        col = self.columns.get((table, column))
        if col is None or col.histogram is None:
            table_stats = self.tables.get(table)
            rows = table_stats.row_count if table_stats else 1
            return min(1.0, 1.0 / max(1, col.ndv if col else rows))
        return col.histogram.selectivity(value)

    def equality_rows(self, table: str, column: str, value: Any) -> float:
        table_stats = self.tables[table]
        return self.selectivity(table, column, value) * table_stats.row_count

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "collected_at_call": self.collected_at_call,
            "note": self.note,
            "tables": {name: stats.to_dict() for name, stats in self.tables.items()},
            "columns": {
                f"{table}.{column}": stats.to_dict()
                for (table, column), stats in self.columns.items()
            },
        }


def _pages_for_rows(row_count: int, row_width_bytes: int = 96, page_bytes: int = 8192) -> int:
    rows_per_page = max(1, page_bytes // row_width_bytes)
    return max(1, math.ceil(row_count / rows_per_page))


def build_histogram(
    table: str,
    column: str,
    values: Sequence[Any],
    mcv_count: int = 60,
    bucket_count: int = 24,
) -> Histogram:
    non_null = [v for v in values if v is not None]
    row_count = len(values)
    null_count = row_count - len(non_null)
    frequencies: Dict[Any, int] = {}
    for value in non_null:
        frequencies[value] = frequencies.get(value, 0) + 1

    ordered = sorted(frequencies.items(), key=lambda item: (-item[1], item[0]))
    mcv = {value: count for value, count in ordered[:mcv_count]}
    remaining_values = [value for value, _count in ordered if value not in mcv]

    buckets: List[Bucket] = []
    if remaining_values:
        # Expand only the non-MCV values in sorted order.  Each bucket owns a
        # share of the remaining frequency mass, giving an equi-depth histogram.
        sorted_values = sorted(remaining_values)
        remaining_rows = sum(frequencies[v] for v in sorted_values)
        per_bucket = max(1, math.ceil(remaining_rows / max(1, bucket_count)))

        current_values: List[Any] = []
        current_rows = 0
        for value in sorted_values:
            count = frequencies[value]
            if current_values and current_rows + count > per_bucket and len(buckets) < bucket_count - 1:
                buckets.append(Bucket(
                    lower=current_values[0],
                    upper=current_values[-1],
                    row_count=current_rows,
                    ndv=len(current_values),
                ))
                current_values = []
                current_rows = 0
            current_values.append(value)
            current_rows += count
        if current_values:
            buckets.append(Bucket(
                lower=current_values[0],
                upper=current_values[-1],
                row_count=current_rows,
                ndv=len(current_values),
            ))

    return Histogram(
        table=table,
        column=column,
        row_count=row_count,
        null_count=null_count,
        mcv=mcv,
        buckets=buckets,
    )


def collect_stats(database: Any, version: Optional[int] = None, note: str = "") -> CatalogStats:
    """Collect table and histogram statistics from the in-memory database."""
    tables: Dict[str, TableStats] = {}
    columns: Dict[Tuple[str, str], ColumnStats] = {}

    for table_name, table in database.tables.items():
        row_count = len(table.rows)
        width = getattr(table, "row_width_bytes", 96)
        tables[table_name] = TableStats(
            name=table_name,
            row_count=row_count,
            page_count=_pages_for_rows(row_count, width),
            row_width_bytes=width,
        )
        for column_name in table.column_names:
            values = [row.get(column_name) for row in table.rows]
            ndv = len({v for v in values if v is not None})
            null_count = sum(1 for v in values if v is None)
            histogram = None
            if table_name in {"orders", "merchants"} and column_name in {
                "merchant_id", "id", "status", "created_ts"
            }:
                histogram = build_histogram(table_name, column_name, values)
            columns[(table_name, column_name)] = ColumnStats(
                table=table_name,
                name=column_name,
                ndv=ndv,
                null_count=null_count,
                histogram=histogram,
            )

    next_version = version if version is not None else database.stats_version + 1
    stats = CatalogStats(version=next_version, tables=tables, columns=columns, note=note)
    database.catalog_stats = stats
    database.stats_version = next_version
    return stats
