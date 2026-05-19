#!/usr/bin/env python3
"""从 CSV 构建本地离线 SQLite 索引库。"""

from __future__ import annotations

import argparse
import csv
import sqlite3
from pathlib import Path


DEFAULT_CSV = Path(__file__).parent / "data" / "phone_segments.csv"
DEFAULT_DB = Path(__file__).parent / "data" / "phone_segments_index.db"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="构建本地离线号码段索引库")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="输入 CSV 路径")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="输出 SQLite 路径")
    parser.add_argument("--batch-size", type=int, default=50000, help="批量写入大小")
    parser.add_argument("--rebuild", action="store_true", help="重建表（先删后建）")
    return parser


def init_db(conn: sqlite3.Connection, rebuild: bool) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-200000")
    conn.execute("PRAGMA mmap_size=268435456")

    if rebuild:
        conn.execute("DROP TABLE IF EXISTS phone_segments")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS phone_segments (
            segment TEXT NOT NULL,
            province TEXT NOT NULL,
            city TEXT NOT NULL,
            operator TEXT NOT NULL,
            type TEXT NOT NULL,
            PRIMARY KEY (segment, province, city, operator)
        )
        """
    )


def create_indexes(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ps_segment ON phone_segments(segment)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ps_province ON phone_segments(province)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ps_city ON phone_segments(city)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ps_operator ON phone_segments(operator)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ps_province_operator ON phone_segments(province, operator)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ps_province_city ON phone_segments(province, city)"
    )


def load_csv_to_db(csv_path: Path, conn: sqlite3.Connection, batch_size: int) -> tuple[int, int]:
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV 文件不存在: {csv_path}")

    required = {"segment", "province", "city", "operator", "type"}
    inserted = 0
    seen_rows = 0

    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"CSV 缺少字段: {', '.join(sorted(missing))}")

        batch: list[tuple[str, str, str, str, str]] = []
        for row in reader:
            seen_rows += 1
            segment = (row.get("segment") or "").strip()
            province = (row.get("province") or "").strip()
            city = (row.get("city") or "").strip()
            operator = (row.get("operator") or "").strip()
            phone_type = (row.get("type") or "").strip()

            if not segment or not province:
                continue

            batch.append((segment, province, city, operator, phone_type))
            if len(batch) >= batch_size:
                before = conn.total_changes
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO phone_segments(segment, province, city, operator, type)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    batch,
                )
                inserted += conn.total_changes - before
                batch.clear()

        if batch:
            before = conn.total_changes
            conn.executemany(
                """
                INSERT OR IGNORE INTO phone_segments(segment, province, city, operator, type)
                VALUES (?, ?, ?, ?, ?)
                """,
                batch,
            )
            inserted += conn.total_changes - before

    return inserted, seen_rows


def main() -> None:
    args = build_parser().parse_args()
    args.db.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(args.db, timeout=60)
    try:
        init_db(conn, rebuild=args.rebuild)
        before_count = conn.execute("SELECT COUNT(*) FROM phone_segments").fetchone()[0]

        conn.execute("BEGIN")
        inserted_changes, seen_rows = load_csv_to_db(args.csv, conn, max(1000, args.batch_size))
        conn.commit()

        create_indexes(conn)
        conn.commit()

        after_count = conn.execute("SELECT COUNT(*) FROM phone_segments").fetchone()[0]
        print(f"[索引] CSV 行数: {seen_rows:,}")
        print(f"[索引] 入库后总行数: {after_count:,}")
        print(f"[索引] 本次新增(估算): {max(0, after_count - before_count):,}")
        print(f"[索引] SQLite 文件: {args.db}")
        print(f"[索引] sqlite3 total_changes: {inserted_changes:,}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
