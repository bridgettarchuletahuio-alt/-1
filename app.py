#!/usr/bin/env python3
"""号码段网页工具 —— Flask 后端"""

from __future__ import annotations

import csv
import io
import json
import os
import random
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from flask import Flask, Response, jsonify, render_template, request

DATA_FILE = Path(__file__).parent / "data" / "phone_segments.csv"
ISSUED_DB_FILE = Path(__file__).parent / "data" / "issued_numbers.db"
ABSTRACT_API_KEY = os.getenv("ABSTRACT_API_KEY", "").strip()
NUMVERIFY_API_KEY = os.getenv("NUMVERIFY_API_KEY", "").strip()
MAX_GENERATE_COUNT = 100000
MAX_BULK_EXPORT_COUNT = 5000000

app = Flask(__name__)

# --------------------------------------------------------------------------- #
# 数据加载（启动时一次性读入内存）
# --------------------------------------------------------------------------- #

# segments_by_province: { province: [ {segment, city, operator}, ... ] }
segments_by_province: dict[str, list[dict]] = defaultdict(list)
# 所有省份（有序）
all_provinces: list[str] = []
# 所有运营商
all_operators: set[str] = set()


def init_issued_db() -> None:
    ISSUED_DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(ISSUED_DB_FILE, timeout=30)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS issued_numbers (
                phone TEXT PRIMARY KEY,
                province TEXT NOT NULL,
                city TEXT NOT NULL,
                operator TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_issued_created_at ON issued_numbers(created_at)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS generation_batches (
                batch_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                requested_count INTEGER NOT NULL,
                generated_count INTEGER NOT NULL,
                mode TEXT NOT NULL,
                provider TEXT NOT NULL,
                provinces_json TEXT NOT NULL,
                operators_json TEXT NOT NULL,
                suffix_digits INTEGER NOT NULL,
                commercial_unique INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'active'
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_batches_created_at ON generation_batches(created_at)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS batch_numbers (
                batch_id TEXT NOT NULL,
                phone TEXT NOT NULL,
                province TEXT NOT NULL,
                city TEXT NOT NULL,
                operator TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (batch_id, phone),
                FOREIGN KEY (batch_id) REFERENCES generation_batches(batch_id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_batch_numbers_batch ON batch_numbers(batch_id)")

        # 兼容旧版本 issued_numbers：增加 batch_id 列（如果不存在）
        columns = {row[1] for row in conn.execute("PRAGMA table_info(issued_numbers)")}
        if "batch_id" not in columns:
            conn.execute("ALTER TABLE issued_numbers ADD COLUMN batch_id TEXT")
        conn.commit()
    finally:
        conn.close()


def load_data() -> None:
    with DATA_FILE.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            seg = row.get("segment", "").strip()
            prov = row.get("province", "").strip()
            city = row.get("city", "").strip()
            op = row.get("operator", "").strip()
            if seg and prov:
                segments_by_province[prov].append(
                    {"segment": seg, "city": city, "operator": op}
                )
                all_operators.add(op)

    # 按省份名排序（直辖市置顶）
    priority = ["北京", "上海", "天津", "重庆"]
    rest = sorted(k for k in segments_by_province if k not in priority)
    all_provinces.extend([p for p in priority if p in segments_by_province])
    all_provinces.extend(rest)

    print(f"[数据] 已加载 {sum(len(v) for v in segments_by_province.values()):,} 条号段，"
          f"覆盖 {len(all_provinces)} 个省份")


load_data()
init_issued_db()

# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #

@app.get("/")
def index():
    available_providers: list[str] = []
    if ABSTRACT_API_KEY:
        available_providers.append("abstract")
    if NUMVERIFY_API_KEY:
        available_providers.append("numverify")

    return render_template(
        "index.html",
        provinces=all_provinces,
        operators=sorted(all_operators),
        api_enabled=bool(available_providers),
        available_providers=available_providers,
    )


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def validate_phone_with_abstract(phone: str) -> dict[str, str | bool]:
    """使用 AbstractAPI 对号码做在线校验。"""
    if not ABSTRACT_API_KEY:
        return {
            "valid": False,
            "country": "",
            "country_code": "",
            "line_type": "",
            "carrier": "",
            "note": "missing_api_key",
        }

    query = urllib.parse.urlencode({"api_key": ABSTRACT_API_KEY, "phone": f"+{phone}"})
    url = f"https://phonevalidation.abstractapi.com/v1/?{query}"
    try:
        data = fetch_json(url)
    except (urllib.error.URLError, TimeoutError, ValueError):
        return {
            "valid": False,
            "country": "",
            "country_code": "",
            "line_type": "",
            "carrier": "",
            "note": "api_error",
            "provider": "abstract",
        }

    return {
        "valid": bool(data.get("valid")),
        "country": (data.get("country") or {}).get("name", ""),
        "country_code": (data.get("country") or {}).get("code", ""),
        "line_type": (data.get("type") or ""),
        "carrier": (data.get("carrier") or ""),
        "note": "ok",
        "provider": "abstract",
    }


def validate_phone_with_numverify(phone: str) -> dict[str, str | bool]:
    """使用 Numverify 对号码做在线校验。"""
    if not NUMVERIFY_API_KEY:
        return {
            "valid": False,
            "country": "",
            "country_code": "",
            "line_type": "",
            "carrier": "",
            "note": "missing_api_key",
            "provider": "numverify",
        }

    query = urllib.parse.urlencode({"access_key": NUMVERIFY_API_KEY, "number": f"+{phone}"})
    url = f"http://apilayer.net/api/validate?{query}"

    try:
        data = fetch_json(url)
    except (urllib.error.URLError, TimeoutError, ValueError):
        return {
            "valid": False,
            "country": "",
            "country_code": "",
            "line_type": "",
            "carrier": "",
            "note": "api_error",
            "provider": "numverify",
        }

    if data.get("success") is False:
        return {
            "valid": False,
            "country": "",
            "country_code": "",
            "line_type": "",
            "carrier": "",
            "note": "api_error",
            "provider": "numverify",
        }

    return {
        "valid": bool(data.get("valid")),
        "country": (data.get("country_name") or ""),
        "country_code": (data.get("country_code") or ""),
        "line_type": (data.get("line_type") or ""),
        "carrier": (data.get("carrier") or ""),
        "note": "ok",
        "provider": "numverify",
    }


@app.get("/api/provinces")
def api_provinces():
    return jsonify(all_provinces)


@app.get("/api/operators")
def api_operators():
    return jsonify(sorted(all_operators))


@app.post("/api/generate")
def api_generate():
    body = request.get_json(silent=True) or {}

    provinces: list[str] = body.get("provinces", [])
    operators: list[str] = body.get("operators", [])
    count: int = max(1, int(body.get("count", 10)))
    unique: bool = bool(body.get("unique", True))
    suffix_digits: int = int(body.get("suffix_digits", 4))  # 7位号段补几位
    commercial_unique: bool = bool(body.get("commercial_unique", True))
    mode: str = str(body.get("mode", "offline")).strip().lower()
    validate_limit: int = max(0, int(body.get("validate_limit", 100)))
    provider: str = str(body.get("provider", "auto")).strip().lower()

    available_providers: list[str] = []
    if ABSTRACT_API_KEY:
        available_providers.append("abstract")
    if NUMVERIFY_API_KEY:
        available_providers.append("numverify")

    if mode not in {"offline", "online"}:
        return jsonify({"error": "mode 仅支持 offline 或 online"}), 400
    if provider not in {"auto", "abstract", "numverify"}:
        return jsonify({"error": "provider 仅支持 auto / abstract / numverify"}), 400
    if mode == "online" and not available_providers:
        return jsonify({"error": "未配置在线 API Key（ABSTRACT_API_KEY / NUMVERIFY_API_KEY）"}), 400
    if count > MAX_GENERATE_COUNT:
        return jsonify({"error": f"单次最多生成 {MAX_GENERATE_COUNT:,} 条，请分批生成"}), 400

    invalid_provinces = [p for p in provinces if p not in segments_by_province]
    if invalid_provinces:
        return jsonify({
            "error": f"不支持的地区: {', '.join(invalid_provinces)}。当前仅支持中国省级地区（含港澳台）。"
        }), 400

    current_provider = provider
    if mode == "online" and provider == "auto":
        if "abstract" in available_providers:
            current_provider = "abstract"
        elif "numverify" in available_providers:
            current_provider = "numverify"

    # 收集候选号段
    candidates: list[dict] = []
    for prov in (provinces if provinces else all_provinces):
        for seg in segments_by_province.get(prov, []):
            if operators and seg["operator"] not in operators:
                continue
            candidates.append({**seg, "province": prov})

    if not candidates:
        return jsonify({"error": "无匹配号段，请调整筛选条件"}), 400

    results: list[dict] = []
    seen: set[str] = set()
    max_attempts = max(count * 50, 2000)
    db_conn: sqlite3.Connection | None = None
    db_cur: sqlite3.Cursor | None = None
    created_at = datetime.now(timezone.utc).isoformat()
    batch_id = f"B{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}{uuid4().hex[:8]}"

    if commercial_unique:
        db_conn = sqlite3.connect(ISSUED_DB_FILE, timeout=30)
        db_conn.execute("PRAGMA busy_timeout=30000")
        db_cur = db_conn.cursor()

    try:
        for _ in range(max_attempts):
            if len(results) >= count:
                break
            seg_info = random.choice(candidates)
            suffix = str(random.randint(0, 10 ** suffix_digits - 1)).zfill(suffix_digits)
            phone = seg_info["segment"] + suffix

            if unique and phone in seen:
                continue

            if commercial_unique and db_cur is not None:
                try:
                    db_cur.execute(
                        """
                        INSERT INTO issued_numbers(phone, province, city, operator, created_at, batch_id)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            phone,
                            seg_info["province"],
                            seg_info["city"],
                            seg_info["operator"],
                            created_at,
                            batch_id,
                        ),
                    )
                except sqlite3.IntegrityError:
                    # 历史上已生成过，直接跳过
                    continue
                except sqlite3.OperationalError as exc:
                    if "database is locked" in str(exc).lower():
                        return jsonify({"error": "数据库繁忙，请稍后重试"}), 503
                    raise

            seen.add(phone)
            results.append({
                "phone":    phone,
                "province": seg_info["province"],
                "city":     seg_info["city"],
                "operator": seg_info["operator"],
            })
    finally:
        if db_conn is not None:
            db_conn.commit()
            db_conn.close()

    # 批次记录：无论是否开启商用防重都写入，便于追溯与导出
    conn = sqlite3.connect(ISSUED_DB_FILE, timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute(
            """
            INSERT INTO generation_batches(
                batch_id, created_at, requested_count, generated_count,
                mode, provider, provinces_json, operators_json,
                suffix_digits, commercial_unique, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
            """,
            (
                batch_id,
                created_at,
                count,
                len(results),
                mode,
                current_provider if mode == "online" else "offline",
                json.dumps(provinces, ensure_ascii=False),
                json.dumps(operators, ensure_ascii=False),
                suffix_digits,
                1 if commercial_unique else 0,
            ),
        )

        if results:
            conn.executemany(
                """
                INSERT OR IGNORE INTO batch_numbers(batch_id, phone, province, city, operator, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        batch_id,
                        row["phone"],
                        row["province"],
                        row["city"],
                        row["operator"],
                        created_at,
                    )
                    for row in results
                ],
            )
        conn.commit()
    except sqlite3.OperationalError as exc:
        if "database is locked" in str(exc).lower():
            return jsonify({"error": "数据库繁忙，请稍后重试"}), 503
        raise
    finally:
        conn.close()

    validated = 0
    fallback_used = False

    if mode == "online" and validate_limit > 0:
        for row in results[:validate_limit]:
            if current_provider == "abstract":
                validated_result = validate_phone_with_abstract(row["phone"])
                if validated_result.get("note") == "api_error" and "numverify" in available_providers:
                    validated_result = validate_phone_with_numverify(row["phone"])
                    fallback_used = True
            elif current_provider == "numverify":
                validated_result = validate_phone_with_numverify(row["phone"])
                if validated_result.get("note") == "api_error" and "abstract" in available_providers:
                    validated_result = validate_phone_with_abstract(row["phone"])
                    fallback_used = True
            else:
                validated_result = {
                    "valid": False,
                    "country": "",
                    "country_code": "",
                    "line_type": "",
                    "carrier": "",
                    "note": "provider_unavailable",
                    "provider": "",
                }
            row.update(validated_result)
            validated += 1

    return jsonify({
        "count": len(results),
        "requested": count,
        "commercial_unique": commercial_unique,
        "max_attempts": max_attempts,
        "batch_id": batch_id,
        "results": results,
        "mode": mode,
        "api_enabled": bool(available_providers),
        "available_providers": available_providers,
        "provider": current_provider if mode == "online" else "",
        "fallback_used": fallback_used,
        "validated": validated,
    })


@app.post("/api/generate/bulk-export")
def api_generate_bulk_export():
    body = request.get_json(silent=True) or {}

    provinces: list[str] = body.get("provinces", [])
    operators: list[str] = body.get("operators", [])
    total_count: int = max(1, int(body.get("total_count", 100000)))
    batch_size: int = max(1, int(body.get("batch_size", MAX_GENERATE_COUNT)))
    suffix_digits: int = int(body.get("suffix_digits", 4))
    unique: bool = bool(body.get("unique", True))
    commercial_unique: bool = bool(body.get("commercial_unique", True))
    mode: str = str(body.get("mode", "offline")).strip().lower()

    if mode != "offline":
        return jsonify({"error": "分批导出仅支持离线模式"}), 400
    if total_count > MAX_BULK_EXPORT_COUNT:
        return jsonify({"error": f"单次分批导出最多 {MAX_BULK_EXPORT_COUNT:,} 条"}), 400
    if batch_size > MAX_GENERATE_COUNT:
        return jsonify({"error": f"每批最多 {MAX_GENERATE_COUNT:,} 条"}), 400

    invalid_provinces = [p for p in provinces if p not in segments_by_province]
    if invalid_provinces:
        return jsonify({
            "error": f"不支持的地区: {', '.join(invalid_provinces)}。当前仅支持中国省级地区（含港澳台）。"
        }), 400

    candidates: list[dict] = []
    for prov in (provinces if provinces else all_provinces):
        for seg in segments_by_province.get(prov, []):
            if operators and seg["operator"] not in operators:
                continue
            candidates.append({**seg, "province": prov})

    if not candidates:
        return jsonify({"error": "无匹配号段，请调整筛选条件"}), 400

    global_seen: set[str] = set()

    def make_csv_line(row: dict[str, str]) -> str:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([row["phone"], row["province"], row["city"], row["operator"]])
        return buf.getvalue()

    def csv_stream():
        yield "\ufeffphone,province,city,operator\n"

        remaining = total_count
        while remaining > 0:
            request_count = min(batch_size, remaining)
            results: list[dict] = []
            seen_batch: set[str] = set()
            max_attempts = max(request_count * 50, 2000)
            created_at = datetime.now(timezone.utc).isoformat()
            batch_id = f"B{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}{uuid4().hex[:8]}"

            db_conn: sqlite3.Connection | None = None
            db_cur: sqlite3.Cursor | None = None
            if commercial_unique:
                db_conn = sqlite3.connect(ISSUED_DB_FILE, timeout=30)
                db_conn.execute("PRAGMA busy_timeout=30000")
                db_cur = db_conn.cursor()

            try:
                for _ in range(max_attempts):
                    if len(results) >= request_count:
                        break

                    seg_info = random.choice(candidates)
                    suffix = str(random.randint(0, 10 ** suffix_digits - 1)).zfill(suffix_digits)
                    phone = seg_info["segment"] + suffix

                    if unique and (phone in seen_batch or phone in global_seen):
                        continue

                    if commercial_unique and db_cur is not None:
                        try:
                            db_cur.execute(
                                """
                                INSERT INTO issued_numbers(phone, province, city, operator, created_at, batch_id)
                                VALUES (?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    phone,
                                    seg_info["province"],
                                    seg_info["city"],
                                    seg_info["operator"],
                                    created_at,
                                    batch_id,
                                ),
                            )
                        except sqlite3.IntegrityError:
                            continue
                        except sqlite3.OperationalError:
                            continue

                    seen_batch.add(phone)
                    global_seen.add(phone)
                    row = {
                        "phone": phone,
                        "province": seg_info["province"],
                        "city": seg_info["city"],
                        "operator": seg_info["operator"],
                    }
                    results.append(row)
            finally:
                if db_conn is not None:
                    db_conn.commit()
                    db_conn.close()

            conn = sqlite3.connect(ISSUED_DB_FILE, timeout=30)
            try:
                conn.execute("PRAGMA busy_timeout=30000")
                conn.execute(
                    """
                    INSERT INTO generation_batches(
                        batch_id, created_at, requested_count, generated_count,
                        mode, provider, provinces_json, operators_json,
                        suffix_digits, commercial_unique, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
                    """,
                    (
                        batch_id,
                        created_at,
                        request_count,
                        len(results),
                        "offline",
                        "offline",
                        json.dumps(provinces, ensure_ascii=False),
                        json.dumps(operators, ensure_ascii=False),
                        suffix_digits,
                        1 if commercial_unique else 0,
                    ),
                )

                if results:
                    conn.executemany(
                        """
                        INSERT OR IGNORE INTO batch_numbers(batch_id, phone, province, city, operator, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                batch_id,
                                row["phone"],
                                row["province"],
                                row["city"],
                                row["operator"],
                                created_at,
                            )
                            for row in results
                        ],
                    )
                conn.commit()
            finally:
                conn.close()

            for row in results:
                yield make_csv_line(row)

            if not results:
                break
            remaining -= len(results)

    filename = f"bulk_generated_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}.csv"
    return Response(
        csv_stream(),
        mimetype="text/csv;charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/batches")
def api_batches():
    limit = max(1, min(int(request.args.get("limit", 20)), 200))
    conn = sqlite3.connect(ISSUED_DB_FILE)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT batch_id, created_at, requested_count, generated_count,
                   mode, provider, suffix_digits, commercial_unique, status
            FROM generation_batches
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()

    return jsonify([dict(r) for r in rows])


@app.get("/api/batches/<batch_id>")
def api_batch_detail(batch_id: str):
    conn = sqlite3.connect(ISSUED_DB_FILE)
    conn.row_factory = sqlite3.Row
    try:
        batch = conn.execute(
            """
            SELECT batch_id, created_at, requested_count, generated_count,
                   mode, provider, provinces_json, operators_json,
                   suffix_digits, commercial_unique, status
            FROM generation_batches
            WHERE batch_id = ?
            """,
            (batch_id,),
        ).fetchone()
        if not batch:
            return jsonify({"error": "批次不存在"}), 404

        numbers = conn.execute(
            """
            SELECT phone, province, city, operator, created_at
            FROM batch_numbers
            WHERE batch_id = ?
            ORDER BY phone ASC
            """,
            (batch_id,),
        ).fetchall()
    finally:
        conn.close()

    b = dict(batch)
    b["provinces"] = json.loads(b.pop("provinces_json") or "[]")
    b["operators"] = json.loads(b.pop("operators_json") or "[]")
    b["numbers"] = [dict(r) for r in numbers]
    return jsonify(b)


@app.post("/api/batches/<batch_id>/void")
def api_batch_void(batch_id: str):
    conn = sqlite3.connect(ISSUED_DB_FILE)
    try:
        cur = conn.execute(
            "UPDATE generation_batches SET status = 'voided' WHERE batch_id = ?",
            (batch_id,),
        )
        conn.commit()
        if cur.rowcount == 0:
            return jsonify({"error": "批次不存在"}), 404
    finally:
        conn.close()
    return jsonify({"ok": True, "batch_id": batch_id, "status": "voided"})


@app.get("/api/batches/<batch_id>/export")
def api_batch_export(batch_id: str):
    conn = sqlite3.connect(ISSUED_DB_FILE)
    conn.row_factory = sqlite3.Row
    try:
        batch = conn.execute(
            "SELECT batch_id FROM generation_batches WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        if not batch:
            return jsonify({"error": "批次不存在"}), 404

        numbers = conn.execute(
            "SELECT phone, province, city, operator FROM batch_numbers WHERE batch_id = ? ORDER BY phone ASC",
            (batch_id,),
        ).fetchall()
    finally:
        conn.close()

    # 返回 CSV
    csv_lines = ["phone,province,city,operator"]
    csv_lines.extend(
        f"{n['phone']},{n['province']},{n['city']},{n['operator']}" for n in numbers
    )
    csv_content = "\n".join(csv_lines)

    return Response(
        "\ufeff" + csv_content,
        mimetype="text/csv;charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="batch_{batch_id}.csv"'
        },
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
