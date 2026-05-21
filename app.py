#!/usr/bin/env python3
"""号码段网页工具 —— Flask 后端"""

from __future__ import annotations

import csv
import io
import json
import logging
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

from flask import Flask, Response, jsonify, render_template, request, session

# Configure module-level logger
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("phone_tool")

DATA_FILE = Path(__file__).parent / "data" / "phone_segments.csv"
ISSUED_DB_FILE = Path(__file__).parent / "data" / "issued_numbers.db"
ABSTRACT_API_KEY = os.getenv("ABSTRACT_API_KEY", "").strip()
NUMVERIFY_API_KEY = os.getenv("NUMVERIFY_API_KEY", "").strip()
MAX_GENERATE_COUNT = 5000000
MAX_BULK_EXPORT_COUNT = 5000000

# 非大陆地区（不加 86 前缀）
NON_MAINLAND_REGIONS: frozenset[str] = frozenset({"韩国", "香港", "意大利"})

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "phone-tool-secret-key-change-in-prod")

LOGIN_PASSWORD = "xiaozhangnb"

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

        # 迁移 generation_batches：移除 commercial_unique 列（如果存在）
        # SQLite 不直接支持 DROP COLUMN（3.35 以下），使用重建表方式
        batch_cols = {row[1] for row in conn.execute("PRAGMA table_info(generation_batches)")}
        if "commercial_unique" in batch_cols:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS generation_batches_new (
                    batch_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    requested_count INTEGER NOT NULL,
                    generated_count INTEGER NOT NULL,
                    mode TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    provinces_json TEXT NOT NULL,
                    operators_json TEXT NOT NULL,
                    suffix_digits INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active'
                )
            """)
            conn.execute("""
                INSERT INTO generation_batches_new
                    (batch_id, created_at, requested_count, generated_count,
                     mode, provider, provinces_json, operators_json, suffix_digits, status)
                SELECT batch_id, created_at, requested_count, generated_count,
                       mode, provider, provinces_json, operators_json, suffix_digits, status
                FROM generation_batches
            """)
            conn.execute("DROP TABLE generation_batches")
            conn.execute("ALTER TABLE generation_batches_new RENAME TO generation_batches")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_batches_created_at ON generation_batches(created_at)")

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

    # 按省份名排序（直辖市置顶，非大陆地区置底）
    priority = ["北京", "上海", "天津", "重庆"]
    mainland_rest = sorted(
        k for k in segments_by_province
        if k not in priority and k not in NON_MAINLAND_REGIONS
    )
    non_mainland = [k for k in NON_MAINLAND_REGIONS if k in segments_by_province]
    all_provinces.extend([p for p in priority if p in segments_by_province])
    all_provinces.extend(mainland_rest)
    all_provinces.extend(non_mainland)

    print(f"[数据] 已加载 {sum(len(v) for v in segments_by_province.values()):,} 条号段，"
          f"覆盖 {len(all_provinces)} 个省份")


load_data()
init_issued_db()

# --------------------------------------------------------------------------- #
# 认证
# --------------------------------------------------------------------------- #

def is_authenticated() -> bool:
    return session.get("authenticated") is True


def require_auth():
    """Return a 401 JSON response if not authenticated, else None."""
    if not is_authenticated():
        return jsonify({"error": "未授权，请先登录"}), 401
    return None


@app.post("/api/login")
def api_login():
    body = request.get_json(silent=True) or {}
    password = str(body.get("password", "")).strip()
    if password == LOGIN_PASSWORD:
        session["authenticated"] = True
        return jsonify({"ok": True})
    return jsonify({"error": "密码错误"}), 401


@app.post("/api/logout")
def api_logout():
    session.clear()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #

@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/config")
def api_config():
    auth_err = require_auth()
    if auth_err:
        return auth_err

    available_providers: list[str] = []
    if ABSTRACT_API_KEY:
        available_providers.append("abstract")
    if NUMVERIFY_API_KEY:
        available_providers.append("numverify")

    # Build cities_by_province: { province: sorted list of unique cities }
    cities_by_province: dict[str, list[str]] = {}
    for prov, segs in segments_by_province.items():
        cities = sorted({seg["city"] for seg in segs if seg["city"]})
        if cities:
            cities_by_province[prov] = cities

    # Build segment_counts_by_city: { city: number of segments }
    segment_counts_by_city: dict[str, int] = defaultdict(int)
    for segs in segments_by_province.values():
        for seg in segs:
            city = seg.get("city", "")
            if city:
                segment_counts_by_city[city] += 1

    return jsonify({
        "provinces": all_provinces,
        "operators": sorted(all_operators),
        "api_enabled": bool(available_providers),
        "available_providers": available_providers,
        "segment_counts": {prov: len(segs) for prov, segs in segments_by_province.items()},
        "cities_by_province": cities_by_province,
        "segment_counts_by_city": dict(segment_counts_by_city),
    })


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
    auth_err = require_auth()
    if auth_err:
        return auth_err
    return jsonify(all_provinces)


@app.get("/api/operators")
def api_operators():
    auth_err = require_auth()
    if auth_err:
        return auth_err
    return jsonify(sorted(all_operators))


@app.post("/api/generate")
def api_generate():
    auth_err = require_auth()
    if auth_err:
        return auth_err
    body = request.get_json(silent=True) or {}

    provinces: list[str] = body.get("provinces", [])
    operators: list[str] = body.get("operators", [])
    cities: list[str] = body.get("cities", [])
    count: int = max(1, int(body.get("count", 10)))
    unique: bool = bool(body.get("unique", True))
    suffix_digits: int = int(body.get("suffix_digits", 4))  # 7位号段补几位
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
            if cities and seg["city"] not in cities:
                continue
            candidates.append({**seg, "province": prov})

    if not candidates:
        return jsonify({"error": "无匹配号段，请调整筛选条件"}), 400

    results: list[dict] = []
    seen: set[str] = set()
    max_attempts = max(count * 50, 2000)
    created_at = datetime.now(timezone.utc).isoformat()
    batch_id = f"B{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}{uuid4().hex[:8]}"

    logger.info(
        "[generate] batch=%s requested=%d mode=%s provinces=%s cities=%s suffix_digits=%d",
        batch_id, count, mode, provinces, cities, suffix_digits,
    )

    # Open a single connection for the entire generation loop so we can check
    # issued_numbers globally (across all users/browsers/requests) before
    # accepting each candidate number.
    try:
        gen_conn = sqlite3.connect(ISSUED_DB_FILE, timeout=30)
    except sqlite3.OperationalError as exc:
        if "database is locked" in str(exc).lower():
            logger.error("[generate] batch=%s DB locked on connect", batch_id)
            return jsonify({"error": "数据库繁忙，请稍后重试"}), 503
        raise
    try:
        gen_conn.execute("PRAGMA journal_mode=WAL")
        gen_conn.execute("PRAGMA busy_timeout=30000")

        # Log current issued_numbers count before generation
        try:
            issued_count_before = gen_conn.execute(
                "SELECT COUNT(*) FROM issued_numbers"
            ).fetchone()[0]
            logger.info(
                "[generate] batch=%s issued_numbers table has %d rows before generation",
                batch_id, issued_count_before,
            )
        except sqlite3.OperationalError as exc:
            logger.warning("[generate] batch=%s could not query issued_numbers: %s", batch_id, exc)

        global_dedup_hits = 0
        for _ in range(max_attempts):
            if len(results) >= count:
                break
            seg_info = random.choice(candidates)
            suffix = str(random.randint(0, 10 ** suffix_digits - 1)).zfill(suffix_digits)
            phone = seg_info["segment"] + suffix

            # In-request dedup (fast, in-memory)
            if unique and phone in seen:
                continue

            # Global dedup: skip numbers already issued in any previous request
            existing = gen_conn.execute(
                "SELECT 1 FROM issued_numbers WHERE phone = ?", (phone,)
            ).fetchone()
            if existing:
                global_dedup_hits += 1
                continue

            seen.add(phone)
            # 仅大陆号码加 86 前缀；非大陆地区（韩国、香港、意大利）不加前缀
            phone_with_prefix = (
                phone if seg_info["province"] in NON_MAINLAND_REGIONS else "86" + phone
            )
            results.append({
                "phone":    phone_with_prefix,
                "province": seg_info["province"],
                "city":     seg_info["city"],
                "operator": seg_info["operator"],
            })

        logger.info(
            "[generate] batch=%s generated=%d global_dedup_hits=%d seen_set_size=%d",
            batch_id, len(results), global_dedup_hits, len(seen),
        )

        # Post-generation dedup verification: catch any duplicates that slipped
        # through (e.g. race conditions between concurrent requests where
        # INSERT OR IGNORE would silently drop the row but the number was
        # already appended to results).
        pre_dedup_count = len(results)
        seen_phones: set[str] = set()
        deduped_results: list[dict] = []
        for r in results:
            if r["phone"] not in seen_phones:
                seen_phones.add(r["phone"])
                deduped_results.append(r)
        if len(deduped_results) != pre_dedup_count:
            duplicate_phones = [
                r["phone"] for r in results if results.count(r) > 1
            ]
            logger.warning(
                "[generate] batch=%s DUPLICATES FOUND in results: pre_dedup=%d "
                "post_dedup=%d duplicate_phones=%s",
                batch_id, pre_dedup_count, len(deduped_results), duplicate_phones,
            )
            results = deduped_results
        else:
            logger.info(
                "[generate] batch=%s no duplicates found in results (count=%d)",
                batch_id, len(results),
            )

        logger.info(
            "[generate] batch=%s final results count=%d",
            batch_id, len(results),
        )

        # Persist batch metadata, issued numbers, and batch-number mapping
        try:
            gen_conn.execute(
                """
                INSERT INTO generation_batches(
                    batch_id, created_at, requested_count, generated_count,
                    mode, provider, provinces_json, operators_json,
                    suffix_digits, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
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
                ),
            )

            if results:
                # Insert into global issued_numbers for permanent cross-request dedup
                logger.info(
                    "[generate] batch=%s inserting %d rows into issued_numbers",
                    batch_id, len(results),
                )
                gen_conn.executemany(
                    """
                    INSERT OR IGNORE INTO issued_numbers(phone, province, city, operator, created_at, batch_id)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            row["phone"][2:] if row["province"] not in NON_MAINLAND_REGIONS else row["phone"],
                            row["province"],
                            row["city"],
                            row["operator"],
                            created_at,
                            batch_id,
                        )
                        for row in results
                    ],
                )
                gen_conn.executemany(
                    """
                    INSERT OR IGNORE INTO batch_numbers(batch_id, phone, province, city, operator, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            batch_id,
                            # 大陆号码去掉 86 前缀存储；非大陆号码原样存储
                            row["phone"][2:] if row["province"] not in NON_MAINLAND_REGIONS else row["phone"],
                            row["province"],
                            row["city"],
                            row["operator"],
                            created_at,
                        )
                        for row in results
                    ],
                )
            gen_conn.commit()
            logger.info("[generate] batch=%s committed to DB successfully", batch_id)
        except sqlite3.OperationalError as exc:
            if "database is locked" in str(exc).lower():
                logger.error("[generate] batch=%s DB locked during INSERT: %s", batch_id, exc)
                return jsonify({"error": "数据库繁忙，请稍后重试"}), 503
            logger.error("[generate] batch=%s DB error during INSERT: %s", batch_id, exc)
            raise
    finally:
        gen_conn.close()

    validated = 0
    fallback_used = False

    if mode == "online" and validate_limit > 0:
        for row in results[:validate_limit]:
            # 大陆号码去掉 86 前缀后发给 API；非大陆号码原样发送
            raw_phone = row["phone"][2:] if row.get("province") not in NON_MAINLAND_REGIONS else row["phone"]
            if current_provider == "abstract":
                validated_result = validate_phone_with_abstract(raw_phone)
                if validated_result.get("note") == "api_error" and "numverify" in available_providers:
                    validated_result = validate_phone_with_numverify(raw_phone)
                    fallback_used = True
            elif current_provider == "numverify":
                validated_result = validate_phone_with_numverify(raw_phone)
                if validated_result.get("note") == "api_error" and "abstract" in available_providers:
                    validated_result = validate_phone_with_abstract(raw_phone)
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
    auth_err = require_auth()
    if auth_err:
        return auth_err
    body = request.get_json(silent=True) or {}

    provinces: list[str] = body.get("provinces", [])
    operators: list[str] = body.get("operators", [])
    cities: list[str] = body.get("cities", [])
    total_count: int = max(1, int(body.get("total_count", 100000)))
    batch_size: int = max(1, int(body.get("batch_size", MAX_GENERATE_COUNT)))
    suffix_digits: int = int(body.get("suffix_digits", 4))
    unique: bool = bool(body.get("unique", True))
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
            if cities and seg["city"] not in cities:
                continue
            candidates.append({**seg, "province": prov})

    if not candidates:
        return jsonify({"error": "无匹配号段，请调整筛选条件"}), 400

    # global_seen tracks numbers issued within this streaming request (in-memory,
    # cross-batch within the same bulk export).  The issued_numbers table provides
    # permanent cross-request dedup.
    global_seen: set[str] = set()

    def make_csv_line(row: dict[str, str]) -> str:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([row["phone"], row["province"], row["city"], row["operator"]])
        return buf.getvalue()

    def csv_stream():
        yield "\ufeffphone,province,city,operator\n"

        remaining = total_count
        # Reuse one DB connection for the entire streaming export so that numbers
        # inserted in earlier batches are visible when checking later batches.
        bulk_conn = sqlite3.connect(ISSUED_DB_FILE, timeout=30)
        try:
            bulk_conn.execute("PRAGMA journal_mode=WAL")
            bulk_conn.execute("PRAGMA busy_timeout=30000")

            # Log issued_numbers count at start of bulk export
            try:
                issued_count_before = bulk_conn.execute(
                    "SELECT COUNT(*) FROM issued_numbers"
                ).fetchone()[0]
                logger.info(
                    "[bulk-export] total_count=%d issued_numbers table has %d rows at start",
                    total_count, issued_count_before,
                )
            except sqlite3.OperationalError as exc:
                logger.warning("[bulk-export] could not query issued_numbers: %s", exc)

            while remaining > 0:
                request_count = min(batch_size, remaining)
                results: list[dict] = []
                seen_batch: set[str] = set()
                max_attempts = max(request_count * 50, 2000)
                created_at = datetime.now(timezone.utc).isoformat()
                batch_id = f"B{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}{uuid4().hex[:8]}"

                logger.info(
                    "[bulk-export] batch=%s request_count=%d remaining=%d",
                    batch_id, request_count, remaining,
                )

                global_dedup_hits = 0
                for _ in range(max_attempts):
                    if len(results) >= request_count:
                        break

                    seg_info = random.choice(candidates)
                    suffix = str(random.randint(0, 10 ** suffix_digits - 1)).zfill(suffix_digits)
                    phone = seg_info["segment"] + suffix

                    # In-request dedup (fast, in-memory)
                    if unique and (phone in seen_batch or phone in global_seen):
                        continue

                    # Global dedup: skip numbers already issued in any previous request
                    existing = bulk_conn.execute(
                        "SELECT 1 FROM issued_numbers WHERE phone = ?", (phone,)
                    ).fetchone()
                    if existing:
                        global_dedup_hits += 1
                        continue

                    seen_batch.add(phone)
                    global_seen.add(phone)
                    # 仅大陆号码加 86 前缀；非大陆地区（韩国、香港、意大利）不加前缀
                    phone_with_prefix = (
                        phone if seg_info["province"] in NON_MAINLAND_REGIONS else "86" + phone
                    )
                    row = {
                        "phone": phone_with_prefix,
                        "province": seg_info["province"],
                        "city": seg_info["city"],
                        "operator": seg_info["operator"],
                    }
                    results.append(row)

                logger.info(
                    "[bulk-export] batch=%s generated=%d global_dedup_hits=%d "
                    "seen_batch_size=%d global_seen_size=%d",
                    batch_id, len(results), global_dedup_hits,
                    len(seen_batch), len(global_seen),
                )

                # Post-generation dedup verification: catch any duplicates that
                # slipped through (e.g. race conditions between concurrent
                # requests where INSERT OR IGNORE would silently drop the row
                # but the number was already appended to results).
                pre_dedup_count = len(results)
                seen_phones_batch: set[str] = set()
                deduped_results: list[dict] = []
                for r in results:
                    if r["phone"] not in seen_phones_batch:
                        seen_phones_batch.add(r["phone"])
                        deduped_results.append(r)
                if len(deduped_results) != pre_dedup_count:
                    duplicate_phones = [
                        r["phone"] for r in results if results.count(r) > 1
                    ]
                    logger.warning(
                        "[bulk-export] batch=%s DUPLICATES FOUND in results: "
                        "pre_dedup=%d post_dedup=%d duplicate_phones=%s",
                        batch_id, pre_dedup_count, len(deduped_results),
                        duplicate_phones,
                    )
                    results = deduped_results
                else:
                    logger.info(
                        "[bulk-export] batch=%s no duplicates found in results (count=%d)",
                        batch_id, len(results),
                    )

                logger.info(
                    "[bulk-export] batch=%s final results count=%d",
                    batch_id, len(results),
                )

                try:
                    bulk_conn.execute(
                        """
                        INSERT INTO generation_batches(
                            batch_id, created_at, requested_count, generated_count,
                            mode, provider, provinces_json, operators_json,
                            suffix_digits, status
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
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
                        ),
                    )

                    if results:
                        # Insert into global issued_numbers for permanent cross-request dedup
                        logger.info(
                            "[bulk-export] batch=%s inserting %d rows into issued_numbers",
                            batch_id, len(results),
                        )
                        bulk_conn.executemany(
                            """
                            INSERT OR IGNORE INTO issued_numbers(phone, province, city, operator, created_at, batch_id)
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            [
                                (
                                    row["phone"][2:] if row["province"] not in NON_MAINLAND_REGIONS else row["phone"],
                                    row["province"],
                                    row["city"],
                                    row["operator"],
                                    created_at,
                                    batch_id,
                                )
                                for row in results
                            ],
                        )
                        bulk_conn.executemany(
                            """
                            INSERT OR IGNORE INTO batch_numbers(batch_id, phone, province, city, operator, created_at)
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            [
                                (
                                    batch_id,
                                    # 大陆号码去掉 86 前缀存储；非大陆号码原样存储
                                    row["phone"][2:] if row["province"] not in NON_MAINLAND_REGIONS else row["phone"],
                                    row["province"],
                                    row["city"],
                                    row["operator"],
                                    created_at,
                                )
                                for row in results
                            ],
                        )
                    bulk_conn.commit()
                    logger.info("[bulk-export] batch=%s committed to DB successfully", batch_id)
                except sqlite3.OperationalError as exc:
                    if "database is locked" in str(exc).lower():
                        logger.error(
                            "[bulk-export] batch=%s DB locked during INSERT: %s", batch_id, exc
                        )
                        # Yield nothing further; the client will receive a truncated
                        # CSV — the HTTP headers are already sent at this point.
                        return
                    logger.error(
                        "[bulk-export] batch=%s DB error during INSERT: %s", batch_id, exc
                    )
                    raise

                for row in results:
                    yield make_csv_line(row)

                if not results:
                    break
                remaining -= len(results)
        finally:
            bulk_conn.close()

    filename = f"bulk_generated_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}.csv"
    return Response(
        csv_stream(),
        mimetype="text/csv;charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/batches")
def api_batches():
    auth_err = require_auth()
    if auth_err:
        return auth_err
    limit = max(1, min(int(request.args.get("limit", 20)), 200))
    conn = sqlite3.connect(ISSUED_DB_FILE)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT batch_id, created_at, requested_count, generated_count,
                   mode, provider, suffix_digits, status
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
    auth_err = require_auth()
    if auth_err:
        return auth_err
    conn = sqlite3.connect(ISSUED_DB_FILE)
    conn.row_factory = sqlite3.Row
    try:
        batch = conn.execute(
            """
            SELECT batch_id, created_at, requested_count, generated_count,
                   mode, provider, provinces_json, operators_json,
                   suffix_digits, status
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
    auth_err = require_auth()
    if auth_err:
        return auth_err
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
    auth_err = require_auth()
    if auth_err:
        return auth_err
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

    # 返回 CSV；大陆号码加 86 前缀，非大陆号码原样输出
    csv_lines = ["phone,province,city,operator"]
    csv_lines.extend(
        (
            f"{n['phone']},{n['province']},{n['city']},{n['operator']}"
            if n["province"] in NON_MAINLAND_REGIONS
            else f"86{n['phone']},{n['province']},{n['city']},{n['operator']}"
        )
        for n in numbers
    )
    csv_content = "\n".join(csv_lines)

    return Response(
        "\ufeff" + csv_content,
        mimetype="text/csv;charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="batch_{batch_id}.csv"'
        },
    )


@app.get("/api/debug/issued-numbers")
def api_debug_issued_numbers():
    """Debug endpoint: inspect the issued_numbers table state."""
    auth_err = require_auth()
    if auth_err:
        return auth_err

    conn = sqlite3.connect(ISSUED_DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        # Total count
        total_count = conn.execute("SELECT COUNT(*) FROM issued_numbers").fetchone()[0]

        # Count by province (top 30)
        by_province = [
            dict(r)
            for r in conn.execute(
                """
                SELECT province, COUNT(*) AS cnt
                FROM issued_numbers
                GROUP BY province
                ORDER BY cnt DESC
                LIMIT 30
                """
            ).fetchall()
        ]

        # Count by city (top 30)
        by_city = [
            dict(r)
            for r in conn.execute(
                """
                SELECT city, COUNT(*) AS cnt
                FROM issued_numbers
                GROUP BY city
                ORDER BY cnt DESC
                LIMIT 30
                """
            ).fetchall()
        ]

        # Most recent 100 issued numbers
        recent = [
            dict(r)
            for r in conn.execute(
                """
                SELECT phone, province, city, operator, created_at, batch_id
                FROM issued_numbers
                ORDER BY created_at DESC
                LIMIT 100
                """
            ).fetchall()
        ]

        # DB file size on disk
        try:
            db_size_bytes = ISSUED_DB_FILE.stat().st_size
        except OSError:
            db_size_bytes = -1

    except sqlite3.OperationalError as exc:
        logger.error("[debug/issued-numbers] DB error: %s", exc)
        return jsonify({"error": f"数据库查询失败: {exc}"}), 500
    finally:
        conn.close()

    logger.info("[debug/issued-numbers] queried: total=%d", total_count)

    return jsonify({
        "total_count": total_count,
        "db_file": str(ISSUED_DB_FILE),
        "db_size_bytes": db_size_bytes,
        "by_province": by_province,
        "by_city": by_city,
        "recent_100": recent,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
