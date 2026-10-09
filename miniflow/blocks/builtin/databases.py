"""Database servers: postgres and mysql.

These need a driver package that MiniFlow doesn't install (the core is standard-library
only): ``pip install psycopg[binary]`` for ``postgres`` and ``pip install pymysql`` for
``mysql``. Without it the block fails with exactly that instruction. Use ``%s`` for values
in a query (``WHERE id > %s``) and give them in ``parameters``; never paste values into the
query text.
"""

from __future__ import annotations

import importlib
from typing import Any

from miniflow.blocks import Setting, block


def _settings(port: int) -> list[Setting]:
    return [
        Setting("query", str, help="One SQL statement; use %s for values"),
        Setting("database", str),
        Setting("user", str),
        Setting("password", str, default="", secret=True, help="{{ env.DB_PASSWORD }}"),
        Setting("host", str, default="localhost"),
        Setting("port", int, default=port),
        Setting("parameters", (list, dict), default=None, help="Values for %s (or %(name)s)"),
        Setting("many", list, default=None, help="Run once per item, e.g. insert {{ rows }}"),
        Setting("timeout", float, default=10.0, help="Seconds to wait for the connection"),
    ]


def _driver(module: str, package: str, block_name: str) -> Any:
    try:
        return importlib.import_module(module)
    except ImportError:
        raise RuntimeError(
            f"the {block_name} block needs the {module} package: pip install {package}"
        ) from None


def _run(conn: Any, query: str, parameters: Any, many: list[Any] | None) -> Any:
    with conn.cursor() as cur:
        if many is not None:
            cur.executemany(query, many)
        else:
            cur.execute(query, parameters)
        if cur.description is None:
            return {"changed": cur.rowcount}
        names = [d.name if hasattr(d, "name") else d[0] for d in cur.description]
        return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


@block("postgres", settings=_settings(5432))
def postgres(
    query: str,
    database: str,
    user: str,
    password: str = "",
    host: str = "localhost",
    port: int = 5432,
    parameters: Any = None,
    many: list[Any] | None = None,
    timeout: float = 10.0,
) -> Any:
    """Run a SQL statement on a PostgreSQL server: rows for SELECT, otherwise a change count."""
    driver = _driver("psycopg", "psycopg[binary]", "postgres")
    with driver.connect(
        host=host,
        port=port,
        dbname=database,
        user=user,
        password=password,
        connect_timeout=int(timeout),
    ) as conn:
        return _run(conn, query, parameters, many)  # leaving the block commits


@block("mysql", settings=_settings(3306))
def mysql(
    query: str,
    database: str,
    user: str,
    password: str = "",
    host: str = "localhost",
    port: int = 3306,
    parameters: Any = None,
    many: list[Any] | None = None,
    timeout: float = 10.0,
) -> Any:
    """Run SQL on a MySQL or MariaDB server: rows for SELECT, otherwise a change count."""
    driver = _driver("pymysql", "pymysql", "mysql")
    conn = driver.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        connect_timeout=int(timeout),
    )
    try:
        result = _run(conn, query, parameters, many)
        conn.commit()
        return result
    finally:
        conn.close()
