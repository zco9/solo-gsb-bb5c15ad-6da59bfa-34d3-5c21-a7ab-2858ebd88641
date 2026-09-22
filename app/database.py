"""SQLite 持久化层：建表、站点与分析结果读写。

数据库路径默认 ./intermod.db，可用环境变量 IMD_DB_PATH 覆盖。
所有多行写入都在单个事务中完成：任何一步失败即整体回滚（整批拒绝）。
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

from .schemas import (
    AnalysisMeta,
    AnalysisResult,
    GuardBand,
    ProductOut,
    SiteCreate,
    SiteOut,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT    NOT NULL UNIQUE,
    scan_start_hz     INTEGER NOT NULL CHECK (scan_start_hz > 0),
    scan_end_hz       INTEGER NOT NULL CHECK (scan_end_hz > scan_start_hz),
    guard_interval_hz INTEGER NOT NULL CHECK (guard_interval_hz >= 0),
    created_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS carriers (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    freq_hz INTEGER NOT NULL CHECK (freq_hz > 0),
    UNIQUE (site_id, freq_hz)
);

CREATE TABLE IF NOT EXISTS guard_bands (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id  INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    start_hz INTEGER NOT NULL CHECK (start_hz > 0),
    end_hz   INTEGER NOT NULL CHECK (end_hz > start_hz)
);

CREATE TABLE IF NOT EXISTS analyses (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id       INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    product_count INTEGER NOT NULL,
    result_json   TEXT    NOT NULL
);
"""


def db_path() -> str:
    return os.environ.get("IMD_DB_PATH", "intermod.db")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """短连接上下文：正常结束提交，抛异常回滚。"""
    con = sqlite3.connect(db_path())
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db() -> None:
    """启动时初始化数据库（幂等建表）。"""
    path = db_path()
    if path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with connect() as con:
        con.executescript(SCHEMA)


# ---------------------------------------------------------------- sites

def insert_site(payload: SiteCreate) -> int:
    """单事务写入站点 + 载频 + 保护频段，返回站点 id。

    重名抛出 sqlite3.IntegrityError（由路由层映射为 409）。
    """
    with connect() as con:
        cur = con.execute(
            "INSERT INTO sites (name, scan_start_hz, scan_end_hz, guard_interval_hz)"
            " VALUES (?, ?, ?, ?)",
            (payload.name, payload.scan_start_hz, payload.scan_end_hz, payload.guard_interval_hz),
        )
        site_id = cur.lastrowid
        con.executemany(
            "INSERT INTO carriers (site_id, freq_hz) VALUES (?, ?)",
            [(site_id, f) for f in payload.carriers_hz],
        )
        con.executemany(
            "INSERT INTO guard_bands (site_id, start_hz, end_hz) VALUES (?, ?, ?)",
            [(site_id, b.start_hz, b.end_hz) for b in payload.guard_bands],
        )
    return site_id


def _row_to_site(con: sqlite3.Connection, row: sqlite3.Row) -> SiteOut:
    carriers = con.execute(
        "SELECT freq_hz FROM carriers WHERE site_id = ? ORDER BY freq_hz", (row["id"],)
    ).fetchall()
    bands = con.execute(
        "SELECT start_hz, end_hz FROM guard_bands WHERE site_id = ? ORDER BY start_hz, end_hz",
        (row["id"],),
    ).fetchall()
    return SiteOut(
        id=row["id"],
        name=row["name"],
        scan_start_hz=row["scan_start_hz"],
        scan_end_hz=row["scan_end_hz"],
        guard_interval_hz=row["guard_interval_hz"],
        carriers_hz=[r["freq_hz"] for r in carriers],
        guard_bands=[GuardBand(start_hz=r["start_hz"], end_hz=r["end_hz"]) for r in bands],
        created_at=row["created_at"],
    )


def fetch_site(site_id: int) -> SiteOut | None:
    with connect() as con:
        row = con.execute("SELECT * FROM sites WHERE id = ?", (site_id,)).fetchone()
        if row is None:
            return None
        return _row_to_site(con, row)


def list_sites() -> list[SiteOut]:
    with connect() as con:
        rows = con.execute("SELECT * FROM sites ORDER BY id").fetchall()
        return [_row_to_site(con, row) for row in rows]


# ---------------------------------------------------------------- analyses

def insert_analysis(site_id: int, products: list[ProductOut]) -> tuple[int, str]:
    """持久化一次分析结果，返回 (analysis_id, created_at)。"""
    result_json = json.dumps([p.model_dump() for p in products])
    with connect() as con:
        cur = con.execute(
            "INSERT INTO analyses (site_id, product_count, result_json) VALUES (?, ?, ?)",
            (site_id, len(products), result_json),
        )
        created_at = con.execute(
            "SELECT created_at FROM analyses WHERE id = ?", (cur.lastrowid,)
        ).fetchone()["created_at"]
    return cur.lastrowid, created_at


def _row_to_result(row: sqlite3.Row) -> AnalysisResult:
    products = [ProductOut(**d) for d in json.loads(row["result_json"])]
    return AnalysisResult(
        analysis_id=row["id"],
        site_id=row["site_id"],
        created_at=row["created_at"],
        product_count=row["product_count"],
        products=products,
    )


def fetch_analysis(analysis_id: int) -> AnalysisResult | None:
    with connect() as con:
        row = con.execute("SELECT * FROM analyses WHERE id = ?", (analysis_id,)).fetchone()
        return _row_to_result(row) if row else None


def fetch_latest_analysis(site_id: int) -> AnalysisResult | None:
    with connect() as con:
        row = con.execute(
            "SELECT * FROM analyses WHERE site_id = ? ORDER BY id DESC LIMIT 1", (site_id,)
        ).fetchone()
        return _row_to_result(row) if row else None


def list_analyses(site_id: int) -> list[AnalysisMeta]:
    with connect() as con:
        rows = con.execute(
            "SELECT id, site_id, created_at, product_count FROM analyses"
            " WHERE site_id = ? ORDER BY id",
            (site_id,),
        ).fetchall()
    return [
        AnalysisMeta(
            analysis_id=r["id"],
            site_id=r["site_id"],
            created_at=r["created_at"],
            product_count=r["product_count"],
        )
        for r in rows
    ]


# ---------------------------------------------------------------- 跨站点批量查询

def fetch_existing_site_ids(site_ids: list[int]) -> set[int]:
    """返回给定 id 中实际存在的站点 id 集合（单次查询）。"""
    if not site_ids:
        return set()
    placeholders = ",".join("?" for _ in site_ids)
    with connect() as con:
        rows = con.execute(
            f"SELECT id FROM sites WHERE id IN ({placeholders})", site_ids
        ).fetchall()
    return {row["id"] for row in rows}


def fetch_latest_analyses_for_sites(site_ids: list[int]) -> dict[int, AnalysisResult]:
    """批量读取多个站点各自最近一次分析（每个站点取分析 id 最大的一条）。

    返回 {site_id: AnalysisResult}；无分析记录的站点不出现在结果中。
    """
    if not site_ids:
        return {}
    placeholders = ",".join("?" for _ in site_ids)
    with connect() as con:
        rows = con.execute(
            "SELECT a.* FROM analyses a"
            " JOIN (SELECT site_id, MAX(id) AS max_id FROM analyses"
            f"      WHERE site_id IN ({placeholders}) GROUP BY site_id) latest"
            "  ON a.id = latest.max_id",
            site_ids,
        ).fetchall()
    return {row["site_id"]: _row_to_result(row) for row in rows}
