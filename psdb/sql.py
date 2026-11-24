from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple


@dataclass(frozen=True)
class ColRef:
    name: str
    table: Optional[str] = None

    def canonical(self) -> str:
        return f"{self.table}.{self.name}" if self.table else self.name


@dataclass
class SelectItem:
    raw: str
    alias: Optional[str] = None
    aggregate: Optional[str] = None
    column: Optional[ColRef] = None

    def canonical(self) -> str:
        if self.aggregate:
            inner = "*" if self.column is None else self.column.canonical()
            text = f"{self.aggregate}({inner})"
        else:
            text = self.raw
        if self.alias:
            text += f" as {self.alias}"
        return text


@dataclass(frozen=True)
class Predicate:
    column: ColRef
    op: str
    value: Any
    placeholder: bool = False

    def canonical(self) -> str:
        return f"{self.column.canonical()} {self.op} ?"


@dataclass(frozen=True)
class JoinClause:
    table: str
    alias: Optional[str]
    left: ColRef
    right: ColRef
    join_type: str = "inner"

    def canonical(self) -> str:
        name = f"{self.table}" + (f" {self.alias}" if self.alias else "")
        return (
            f"{self.join_type} join {name} on "
            f"{self.left.canonical()} = {self.right.canonical()}"
        )


@dataclass(frozen=True)
class OrderItem:
    column: ColRef
    descending: bool = False

    def canonical(self) -> str:
        return self.column.canonical() + (" desc" if self.descending else " asc")


@dataclass
class Statement:
    items: List[SelectItem]
    from_table: str
    from_alias: Optional[str]
    predicates: List[Predicate] = field(default_factory=list)
    joins: List[JoinClause] = field(default_factory=list)
    group_by: List[ColRef] = field(default_factory=list)
    order_by: List[OrderItem] = field(default_factory=list)
    limit: Optional[int] = None
    params: List[Any] = field(default_factory=list)
    normalized: str = ""

    @property
    def has_aggregate(self) -> bool:
        return any(item.aggregate for item in self.items) or bool(self.group_by)

    def table_alias_map(self) -> dict:
        result = {}
        if self.from_alias:
            result[self.from_alias] = self.from_table
        result[self.from_table] = self.from_table
        for join in self.joins:
            result[join.table] = join.table
            if join.alias:
                result[join.alias] = join.table
        return result

    def predicate_values(self, table_or_alias: str, column: str) -> List[Any]:
        alias_map = self.table_alias_map()
        wanted_table = alias_map.get(table_or_alias, table_or_alias)
        values = []
        for pred in self.predicates:
            if pred.column.table:
                pred_table = alias_map.get(pred.column.table, pred.column.table)
            else:
                # Unqualified predicates belong to the FROM table in this dialect.
                pred_table = self.from_table
            if pred_table == wanted_table and pred.column.name == column:
                values.append(pred.value)
        return values


_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<number>\d+(?:\.\d+)?)
  | (?P<string>'(?:''|[^'])*')
  | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<placeholder>\?)
  | (?P<op><=|>=|!=|=|<|>)
  | (?P<punct>[,*.()])
    """,
    re.VERBOSE,
)

_KEYWORDS = {
    "select", "from", "where", "join", "inner", "left", "right", "outer",
    "on", "and", "or", "order", "group", "by", "limit", "as", "desc", "asc",
    "count", "sum", "avg", "min", "max",
}
_TABLE_STOP = {
    "where", "join", "inner", "left", "right", "order", "group", "limit", "as",
}


class ParseError(ValueError):
    pass


class Parser:
    def __init__(self, sql: str, supplied_params: Optional[List[Any]] = None):
        self.sql = sql.strip().rstrip(";")
        self.tokens: List[Tuple[str, str]] = []
        self.pos = 0
        self.supplied_params = list(supplied_params or [])
        self.supplied_pos = 0
        self._tokenize()

    def _tokenize(self) -> None:
        pos = 0
        while pos < len(self.sql):
            match = _TOKEN_RE.match(self.sql, pos)
            if not match:
                raise ParseError(f"无法解析 SQL 片段: {self.sql[pos:pos + 20]!r}")
            kind = match.lastgroup
            value = match.group()
            if kind != "ws":
                if kind == "ident":
                    value = value.lower()
                self.tokens.append((kind, value))
            pos = match.end()

    def peek(self, offset: int = 0) -> Optional[Tuple[str, str]]:
        index = self.pos + offset
        return self.tokens[index] if index < len(self.tokens) else None

    def next_token(self) -> Tuple[str, str]:
        token = self.peek()
        if token is None:
            raise ParseError("SQL 意外结束")
        self.pos += 1
        return token

    def expect(self, value: str) -> None:
        kind, token_value = self.next_token()
        if token_value.lower() != value.lower():
            raise ParseError(f"期望 {value!r}，得到 {token_value!r}")

    def consume_keyword_if(self, value: str) -> bool:
        token = self.peek()
        if token and token[1].lower() == value.lower():
            self.pos += 1
            return True
        return False

    def parse(self) -> Statement:
        self.expect("select")
        items = self.parse_select_list()
        self.expect("from")
        from_table, from_alias = self.parse_table_ref()

        joins: List[JoinClause] = []
        while self.peek() and self.peek()[1].lower() in {"join", "inner", "left", "right"}:
            join_type = "inner"
            token = self.peek()[1].lower()
            if token in {"inner", "left", "right"}:
                self.next_token()
                if token in {"left", "right"}:
                    self.consume_keyword_if("outer")
                    join_type = token
                else:
                    join_type = "inner"
            self.consume_keyword_if("join")
            table, alias = self.parse_table_ref()
            self.expect("on")
            left = self.parse_col_ref()
            op_token = self.next_token()
            if op_token[1] != "=":
                raise ParseError("目前连接条件只支持等值连接")
            right = self.parse_col_ref()
            joins.append(JoinClause(table, alias, left, right, join_type))

        predicates: List[Predicate] = []
        if self.consume_keyword_if("where"):
            predicates = self.parse_predicates()

        group_by: List[ColRef] = []
        if self.peek() and self.peek()[1].lower() == "group":
            self.next_token()
            self.expect("by")
            group_by.append(self.parse_col_ref())
            while self.consume_punct_if(","):
                group_by.append(self.parse_col_ref())

        order_by: List[OrderItem] = []
        if self.peek() and self.peek()[1].lower() == "order":
            self.next_token()
            self.expect("by")
            while True:
                column = self.parse_col_ref()
                descending = False
                token = self.peek()
                if token and token[1].lower() in {"desc", "asc"}:
                    direction = self.next_token()[1].lower()
                    descending = direction == "desc"
                order_by.append(OrderItem(column, descending))
                if not self.consume_punct_if(","):
                    break

        limit = None
        if self.consume_keyword_if("limit"):
            kind, value = self.next_token()
            if kind == "placeholder":
                limit = int(self._take_supplied_param())
            else:
                limit = int(float(value))

        if self.pos != len(self.tokens):
            raise ParseError(f"SQL 末尾有未解析 token: {self.peek()}")

        params = [p.value for p in predicates]
        statement = Statement(
            items=items,
            from_table=from_table,
            from_alias=from_alias,
            predicates=predicates,
            joins=joins,
            group_by=group_by,
            order_by=order_by,
            limit=limit,
            params=params,
        )
        statement.normalized = self.canonical(statement)
        return statement

    def _take_supplied_param(self) -> Any:
        if self.supplied_pos >= len(self.supplied_params):
            raise ParseError("占位符 ? 缺少绑定参数")
        value = self.supplied_params[self.supplied_pos]
        self.supplied_pos += 1
        return value

    def consume_punct_if(self, value: str) -> bool:
        token = self.peek()
        if token and token[0] == "punct" and token[1] == value:
            self.pos += 1
            return True
        return False

    def parse_select_list(self) -> List[SelectItem]:
        items = [self.parse_select_item()]
        while self.consume_punct_if(","):
            items.append(self.parse_select_item())
        return items

    def parse_select_item(self) -> SelectItem:
        token = self.peek()
        if token and token[0] == "punct" and token[1] == "*":
            self.next_token()
            return SelectItem(raw="*")

        # Aggregate function, e.g. count(*), sum(o.amount)
        second = self.peek(1)
        if token and token[0] == "ident" and second and second[1] == "(":
            func = self.next_token()[1].lower()
            self.next_token()  # (
            column = None
            if self.peek() and self.peek()[1] == "*":
                self.next_token()
            else:
                column = self.parse_col_ref()
            if not self.consume_punct_if(")"):
                raise ParseError("聚合函数缺少右括号")
            alias = self.parse_optional_alias()
            return SelectItem(
                raw=f"{func}({column.canonical() if column else '*'})",
                alias=alias,
                aggregate=func,
                column=column,
            )

        column = self.parse_col_ref()
        alias = self.parse_optional_alias()
        return SelectItem(raw=column.canonical(), alias=alias, column=column)

    def parse_optional_alias(self) -> Optional[str]:
        if self.consume_keyword_if("as"):
            kind, value = self.next_token()
            return value
        token = self.peek()
        if token and token[0] == "ident" and token[1].lower() not in _TABLE_STOP and token[1].lower() not in {"desc", "asc"}:
            # An alias is followed by comma/from/join/order/group/limit.
            following = self.peek(1)
            if following is None or following[1] in {",", "from", "join", "order", "group", "limit"} or following[1].lower() in {"inner", "left", "right", "where"}:
                self.next_token()
                return token[1]
        return None

    def parse_table_ref(self) -> Tuple[str, Optional[str]]:
        kind, table = self.next_token()
        if kind != "ident":
            raise ParseError("期望表名")
        alias = None
        token = self.peek()
        if token and token[0] == "ident" and token[1].lower() not in _TABLE_STOP:
            # Do not swallow join keywords or direction words.
            if token[1].lower() not in {"desc", "asc", "on", "using"}:
                self.next_token()
                alias = token[1]
        return table, alias

    def parse_col_ref(self) -> ColRef:
        kind, first = self.next_token()
        if kind != "ident":
            raise ParseError(f"期望列名，得到 {first!r}")
        if self.peek() and self.peek()[1] == ".":
            self.next_token()
            kind2, column = self.next_token()
            if kind2 != "ident":
                raise ParseError("点号后期望列名")
            return ColRef(column, first)
        return ColRef(first)

    def parse_predicates(self) -> List[Predicate]:
        predicates = [self.parse_predicate()]
        while True:
            token = self.peek()
            if token and token[1].lower() == "and":
                self.next_token()
                predicates.append(self.parse_predicate())
            else:
                break
        return predicates

    def parse_predicate(self) -> Predicate:
        column = self.parse_col_ref()
        op_kind, op = self.next_token()
        if op_kind != "op":
            raise ParseError(f"期望比较运算符，得到 {op!r}")
        kind, value = self.next_token()
        placeholder = kind == "placeholder"
        if placeholder:
            parsed_value = self._take_supplied_param()
        elif kind == "number":
            parsed_value = float(value) if "." in value else int(value)
        elif kind == "string":
            parsed_value = value[1:-1].replace("''", "'")
        elif kind == "ident" and self.peek() and self.peek()[1] == ".":
            # Predicate is a column-column join-like condition (rare in WHERE).
            self.pos -= 1
            parsed_value = self.parse_col_ref()
            return Predicate(column, op, parsed_value, False)
        else:
            raise ParseError(f"谓词右侧必须是字面量或 ?，得到 {value!r}")
        return Predicate(column, op, parsed_value, placeholder)

    @staticmethod
    def canonical(stmt: Statement) -> str:
        parts = ["select", ", ".join(item.canonical() for item in stmt.items)]
        from_text = f"from {stmt.from_table}"
        if stmt.from_alias:
            from_text += f" {stmt.from_alias}"
        parts.append(from_text)
        for join in stmt.joins:
            parts.append(join.canonical())
        if stmt.predicates:
            parts.append("where " + " and ".join(p.canonical() for p in stmt.predicates))
        if stmt.group_by:
            parts.append("group by " + ", ".join(c.canonical() for c in stmt.group_by))
        if stmt.order_by:
            parts.append("order by " + ", ".join(o.canonical() for o in stmt.order_by))
        if stmt.limit is not None:
            parts.append("limit ?")
        return " ".join(parts)


def parse_sql(sql: str, params: Optional[List[Any]] = None) -> Statement:
    return Parser(sql, params).parse()


def normalize_sql(sql: str) -> str:
    return parse_sql(sql).normalized
