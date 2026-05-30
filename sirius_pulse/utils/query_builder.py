"""动态 SQL 查询构建器。

提供流式 API 构建 WHERE 子句，消除重复的条件拼接代码。
"""
from __future__ import annotations

from typing import Any

__all__ = ["QueryBuilder"]


class QueryBuilder:
    """动态 SQL 查询构建器。

    使用示例::

        builder = QueryBuilder()
        sql, params = (
            builder.where("session_id = ?", session_id)
                   .where("persona_name = ?", persona_name)
                   .build_select("token_usage", limit=100)
        )
    """

    def __init__(self) -> None:
        self._clauses: list[str] = []
        self._params: list[object] = []

    def where(self, condition: str, value: Any) -> QueryBuilder:
        """添加 WHERE 条件。

        Parameters
        ----------
        condition:
            SQL 条件表达式，如 "session_id = ?"。
        value:
            条件对应的参数值。
        """
        self._clauses.append(condition)
        self._params.append(value)
        return self

    def where_optional(self, condition: str, value: Any | None) -> QueryBuilder:
        """仅当 value 不为 None 时添加 WHERE 条件。"""
        if value is not None:
            self._clauses.append(condition)
            self._params.append(value)
        return self

    def build_where(self) -> tuple[str, list[object]]:
        """构建 WHERE 子句。

        Returns
        -------
        tuple[str, list[object]]:
            (where_clause, params) - where_clause 可能为空字符串。
        """
        if not self._clauses:
            return "", []
        return " WHERE " + " AND ".join(self._clauses), list(self._params)

    def build_select(
        self,
        table: str,
        columns: str = "*",
        *,
        order_by: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> tuple[str, list[object]]:
        """构建 SELECT 查询。

        Parameters
        ----------
        table:
            表名。
        columns:
            查询列，默认 "*"。
        order_by:
            排序子句，如 "timestamp DESC"。
        limit:
            返回行数限制。
        offset:
            偏移量。

        Returns
        -------
        tuple[str, list[object]]:
            (sql, params)
        """
        where_clause, params = self.build_where()
        sql = f"SELECT {columns} FROM {table}{where_clause}"

        if order_by:
            sql += f" ORDER BY {order_by}"
        if limit is not None:
            sql += f" LIMIT {limit}"
        if offset is not None:
            sql += f" OFFSET {offset}"

        return sql, params

    def build_delete(self, table: str) -> tuple[str, list[object]]:
        """构建 DELETE 查询。

        Returns
        -------
        tuple[str, list[object]]:
            (sql, params)
        """
        where_clause, params = self.build_where()
        sql = f"DELETE FROM {table}{where_clause}"
        return sql, params

    def build_count(self, table: str) -> tuple[str, list[object]]:
        """构建 COUNT 查询。

        Returns
        -------
        tuple[str, list[object]]:
            (sql, params)
        """
        where_clause, params = self.build_where()
        sql = f"SELECT COUNT(*) as cnt FROM {table}{where_clause}"
        return sql, params

    def reset(self) -> QueryBuilder:
        """重置构建器状态，允许复用。"""
        self._clauses.clear()
        self._params.clear()
        return self
