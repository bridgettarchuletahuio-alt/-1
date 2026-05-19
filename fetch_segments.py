#!/usr/bin/env python3
"""
从 phone 离线库批量提取全量号码段数据，写入 data/phone_segments.csv。

用法:
    python3 fetch_segments.py                    # 全量提取（约 50 万条）
    python3 fetch_segments.py --limit 10000      # 仅提取前 N 条（测试用）
    python3 fetch_segments.py --prefix 139       # 仅提取指定前缀
    python3 fetch_segments.py --output my.csv    # 自定义输出路径
    python3 fetch_segments.py --merge            # 与现有 CSV 合并（去重）
"""

from __future__ import annotations

import argparse
import csv
import struct
import sys
from pathlib import Path

# --------------------------------------------------------------------------- #
# phone.dat 格式（来自 phone 库源码）
# 头部: <4si  =>  4字节版本 + 4字节记录数 + 1字节偏移标志
# 每条记录: <iiB  =>  int(号段起始) + int(号段结束) + uint8(内容偏移)
# 内容区: \0 分隔的字段序列: province\0city\0zip\0area_code\0phone_type\0
# --------------------------------------------------------------------------- #

try:
    from phone import Phone as _PhoneLib
    _phone_lib = _PhoneLib()
    DAT_FILE: Path = Path(_phone_lib.dat_file)
    PHONE_FMT: str = _phone_lib.phone_fmt          # "<iiB"
    PHONE_FMT_LEN: int = _phone_lib.phone_fmt_length  # 9
    FIRST_OFFSET: int = _phone_lib.first_phone_record_offset
    RECORD_COUNT: int = _phone_lib.phone_record_count
except ImportError:
    print("错误: 未安装 phone 库，请先执行 pip install phone", file=sys.stderr)
    sys.exit(1)


OUTPUT_FILE = Path(__file__).parent / "data" / "phone_segments.csv"

# 运营商代码 → 中文名称（来自 phone 库源码 get_phone_no_type）
OPERATOR_MAP: dict[int, str] = {
    1: "中国移动",
    2: "中国联通",
    3: "中国电信",
    4: "电信虚拟运营商",
    5: "联通虚拟运营商",
    6: "移动虚拟运营商",
    7: "广电",
    8: "广电虚拟运营商",
}

# 手机号前缀段（7位号段的前3-4位），用于过滤固话/特服号码
MOBILE_PREFIX = {
    "130", "131", "132", "133", "134", "135", "136", "137", "138", "139",
    "145", "146", "147", "148", "149",
    "150", "151", "152", "153", "155", "156", "157", "158", "159",
    "162", "165", "166",
    "170", "171", "172", "173", "174", "175", "176", "177", "178",
    "180", "181", "182", "183", "184", "185", "186", "187", "188", "189",
    "190", "191", "192", "193", "195", "196", "197", "198", "199",
}


def parse_content(buf: bytes, offset: int) -> tuple[str, str, str, str]:
    """
    解析号段内容区，返回 (province, city, zip_code, area_code)。
    内容格式: province|city|zip_code|area_code  以 \\x00 结尾。
    """
    end = buf.find(b"\x00", offset)
    if end == -1:
        end = offset + 64
    content = buf[offset:end].decode("utf-8", errors="replace")
    parts = content.split("|")
    province  = parts[0] if len(parts) > 0 else ""
    city      = parts[1] if len(parts) > 1 else ""
    zip_code  = parts[2] if len(parts) > 2 else ""
    area_code = parts[3] if len(parts) > 3 else ""
    return province, city, zip_code, area_code


def extract_records(
    prefix_filter: str = "",
    limit: int = 0,
    mobile_only: bool = True,
) -> list[dict[str, str]]:
    """
    从 phone.dat 读取全量记录。

    返回字段: segment, province, city, operator, type
    """
    records: list[dict[str, str]] = []

    with DAT_FILE.open("rb") as f:
        buf = f.read()

    total = RECORD_COUNT
    offset = FIRST_OFFSET

    print(f"数据库版本: {_phone_lib.version.decode()}")
    print(f"总记录数:   {total:,}")
    print(f"数据文件:   {DAT_FILE}")
    print(f"开始提取...")

    for i in range(total):
        if limit and len(records) >= limit:
            break

        raw = buf[offset: offset + PHONE_FMT_LEN]
        if len(raw) < PHONE_FMT_LEN:
            break

        phone_start, content_offset, _phone_type_code = struct.unpack(PHONE_FMT, raw)
        offset += PHONE_FMT_LEN

        segment = str(phone_start)

        # 过滤：仅手机号（7位号段前3位在已知前缀集合内）
        if mobile_only and segment[:3] not in MOBILE_PREFIX:
            continue

        # 前缀过滤
        if prefix_filter and not segment.startswith(prefix_filter):
            continue

        # 解析内容（province, city 等）
        try:
            province, city, _zip, _area = parse_content(buf, content_offset)
        except Exception:
            continue

        if not province:
            continue

        operator = OPERATOR_MAP.get(_phone_type_code, "其他")

        records.append({
            "segment":  segment,
            "province": province,
            "city":     city,
            "operator": operator,
            "type":     "移动",
        })

        if (i + 1) % 50000 == 0:
            print(f"  已处理 {i + 1:,} / {total:,} 条，已收录 {len(records):,} 条...")

    print(f"提取完成，共收录 {len(records):,} 条记录。")
    return records


def merge_with_existing(
    new_records: list[dict[str, str]],
    existing_file: Path,
) -> list[dict[str, str]]:
    """与现有 CSV 合并，以 segment 为主键去重（新数据优先）。"""
    if not existing_file.exists():
        return new_records

    existing: dict[str, dict[str, str]] = {}
    with existing_file.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            seg = row.get("segment", "").strip()
            if seg:
                existing[seg] = {k: v.strip() for k, v in row.items()}

    # 新数据覆盖旧数据
    for rec in new_records:
        existing[rec["segment"]] = rec

    merged = sorted(existing.values(), key=lambda r: r["segment"])
    print(f"合并后共 {len(merged):,} 条（原 {len(existing) - len(new_records):,} + 新增/更新 {len(new_records):,}）")
    return merged


def write_csv(records: list[dict[str, str]], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["segment", "province", "city", "operator", "type"],
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(records)
    print(f"已写入: {output}  ({len(records):,} 条)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="从 phone 离线库批量提取号码段数据",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--output", type=Path, default=OUTPUT_FILE, help="输出 CSV 路径")
    p.add_argument("--limit", type=int, default=0, metavar="N", help="最多提取 N 条（0=全量）")
    p.add_argument("--prefix", default="", metavar="PREFIX", help="仅提取指定号码前缀，例如 139")
    p.add_argument("--all", dest="all_types", action="store_true", help="包含固话/特服等非手机号记录")
    p.add_argument("--merge", action="store_true", help="与现有 CSV 合并（以 segment 去重）")
    return p


def main() -> None:
    args = build_parser().parse_args()

    records = extract_records(
        prefix_filter=args.prefix,
        limit=args.limit,
        mobile_only=not args.all_types,
    )

    if args.merge:
        records = merge_with_existing(records, args.output)
    else:
        records = sorted(records, key=lambda r: r["segment"])

    write_csv(records, args.output)


if __name__ == "__main__":
    main()
