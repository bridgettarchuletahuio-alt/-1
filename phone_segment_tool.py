#!/usr/bin/env python3
"""全国号码段筛选工具（命令行版）。

说明：
- 默认读取 data/phone_segments.csv
- 支持按省份、城市、运营商、号码段前缀进行筛选
- 支持导出筛选结果到 CSV
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Iterable


DATA_FILE = Path(__file__).parent / "data" / "phone_segments.csv"

# 省级行政区（常见简写）
EXPECTED_PROVINCES = {
    "北京",
    "天津",
    "河北",
    "山西",
    "内蒙古",
    "辽宁",
    "吉林",
    "黑龙江",
    "上海",
    "江苏",
    "浙江",
    "安徽",
    "福建",
    "江西",
    "山东",
    "河南",
    "湖北",
    "湖南",
    "广东",
    "广西",
    "海南",
    "重庆",
    "四川",
    "贵州",
    "云南",
    "西藏",
    "陕西",
    "甘肃",
    "青海",
    "宁夏",
    "新疆",
    "香港",
    "澳门",
    "台湾",
}

PROVINCE_ALIASES = {
    "北京市": "北京",
    "天津市": "天津",
    "河北省": "河北",
    "山西省": "山西",
    "内蒙古自治区": "内蒙古",
    "辽宁省": "辽宁",
    "吉林省": "吉林",
    "黑龙江省": "黑龙江",
    "上海市": "上海",
    "江苏省": "江苏",
    "浙江省": "浙江",
    "安徽省": "安徽",
    "福建省": "福建",
    "江西省": "江西",
    "山东省": "山东",
    "河南省": "河南",
    "湖北省": "湖北",
    "湖南省": "湖南",
    "广东省": "广东",
    "广西壮族自治区": "广西",
    "海南省": "海南",
    "重庆市": "重庆",
    "四川省": "四川",
    "贵州省": "贵州",
    "云南省": "云南",
    "西藏自治区": "西藏",
    "陕西省": "陕西",
    "甘肃省": "甘肃",
    "青海省": "青海",
    "宁夏回族自治区": "宁夏",
    "新疆维吾尔自治区": "新疆",
    "香港特别行政区": "香港",
    "澳门特别行政区": "澳门",
    "台湾省": "台湾",
}


def load_rows(csv_file: Path) -> list[dict[str, str]]:
    if not csv_file.exists():
        raise FileNotFoundError(f"数据文件不存在: {csv_file}")

    with csv_file.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        required = {"segment", "province", "city", "operator", "type"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            names = ", ".join(sorted(missing))
            raise ValueError(f"CSV 缺少必要字段: {names}")

        rows = []
        for row in reader:
            if not row.get("segment"):
                continue
            rows.append({k: (v or "").strip() for k, v in row.items()})

    return rows


def parse_multi_values(value: str | None) -> set[str]:
    if not value:
        return set()
    return {item.strip() for item in value.split(",") if item.strip()}


def normalize_province(name: str) -> str:
    text = name.strip()
    return PROVINCE_ALIASES.get(text, text)


def filter_rows(
    rows: Iterable[dict[str, str]],
    provinces: set[str],
    cities: set[str],
    operators: set[str],
    segment_prefix: str,
    keyword: str,
) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []

    for row in rows:
        if provinces and row["province"] not in provinces:
            continue
        if cities and row["city"] not in cities:
            continue
        if operators and row["operator"] not in operators:
            continue
        if segment_prefix and not row["segment"].startswith(segment_prefix):
            continue
        if keyword:
            text = " ".join([row["segment"], row["province"], row["city"], row["operator"], row["type"]])
            if keyword not in text:
                continue

        result.append(row)

    return result


def pretty_print(rows: list[dict[str, str]]) -> None:
    headers = ["segment", "province", "city", "operator", "type"]
    if not rows:
        print("未匹配到任何记录。")
        return

    widths = {h: len(h) for h in headers}
    for row in rows:
        for h in headers:
            widths[h] = max(widths[h], len(row[h]))

    line = " | ".join(h.ljust(widths[h]) for h in headers)
    sep = "-+-".join("-" * widths[h] for h in headers)
    print(line)
    print(sep)

    for row in rows:
        print(" | ".join(row[h].ljust(widths[h]) for h in headers))

    print(f"\n共 {len(rows)} 条。")


def export_csv(rows: list[dict[str, str]], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["segment", "province", "city", "operator", "type"])
        writer.writeheader()
        writer.writerows(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="全国号码段筛选工具")
    parser.add_argument("--data", type=Path, default=DATA_FILE, help="CSV 数据文件路径")
    parser.add_argument("--province", help="省份，支持多个值用逗号分隔")
    parser.add_argument("--city", help="城市，支持多个值用逗号分隔")
    parser.add_argument("--operator", help="运营商，支持多个值用逗号分隔")
    parser.add_argument("--segment-prefix", default="", help="号码段前缀，例如 139 或 1390")
    parser.add_argument("--keyword", default="", help="关键字匹配（省市/运营商/类型/号段）")
    parser.add_argument("--list-provinces", action="store_true", help="列出数据中所有省份及数量")
    parser.add_argument("--ai-audit", action="store_true", help="AI 辅助评估号码段库完整度与质量")
    parser.add_argument("--audit-output", type=Path, help="将 AI 评估报告导出为 JSON")
    parser.add_argument("--output", type=Path, help="将结果导出为 CSV")
    return parser


def list_provinces(rows: Iterable[dict[str, str]]) -> None:
    counter: dict[str, int] = {}
    for row in rows:
        p = row["province"]
        counter[p] = counter.get(p, 0) + 1

    for province in sorted(counter):
        print(f"{province}: {counter[province]}")


def audit_rows(rows: list[dict[str, str]]) -> dict[str, object]:
    segment_counter: dict[str, int] = {}
    province_counter: dict[str, int] = {}
    operator_counter: dict[str, int] = {}
    invalid_segments: list[str] = []
    unknown_province_rows: list[dict[str, str]] = []
    segment_to_provinces: dict[str, set[str]] = {}

    for row in rows:
        segment = row["segment"].strip()
        province = normalize_province(row["province"])
        operator = row["operator"].strip()

        segment_counter[segment] = segment_counter.get(segment, 0) + 1
        province_counter[province] = province_counter.get(province, 0) + 1
        operator_counter[operator] = operator_counter.get(operator, 0) + 1

        if not (segment.isdigit() and len(segment) == 7):
            invalid_segments.append(segment)

        if province and province not in EXPECTED_PROVINCES:
            unknown_province_rows.append(row)

        if segment not in segment_to_provinces:
            segment_to_provinces[segment] = set()
        segment_to_provinces[segment].add(province)

    duplicate_segments = sorted([s for s, c in segment_counter.items() if c > 1])
    conflicting_segments = sorted([s for s, pset in segment_to_provinces.items() if len(pset) > 1])
    missing_provinces = sorted(EXPECTED_PROVINCES - set(province_counter.keys()))

    # 启发式评分：用于辅助判断，不是监管口径
    score = 100.0
    score -= min(40.0, len(missing_provinces) * 1.6)
    score -= min(25.0, len(invalid_segments) * 2.0)
    score -= min(20.0, len(duplicate_segments) * 1.5)
    score -= min(15.0, len(conflicting_segments) * 2.0)
    score = max(0.0, round(score, 2))

    if score >= 85:
        level = "高"
    elif score >= 60:
        level = "中"
    else:
        level = "低"

    return {
        "total_rows": len(rows),
        "unique_segments": len(segment_counter),
        "coverage": {
            "expected_provinces": len(EXPECTED_PROVINCES),
            "covered_provinces": len(province_counter),
            "missing_provinces": missing_provinces,
        },
        "quality": {
            "invalid_segments": sorted(set(invalid_segments)),
            "duplicate_segments": duplicate_segments,
            "conflicting_segments": conflicting_segments,
            "unknown_province_rows": unknown_province_rows[:20],
        },
        "distribution": {
            "province_counter": dict(sorted(province_counter.items())),
            "operator_counter": dict(sorted(operator_counter.items())),
        },
        "ai_assessment": {
            "score": score,
            "confidence_level": level,
            "note": "该结果为启发式 AI 辅助判断，不等同于运营商或监管部门官方认证。",
        },
    }


def print_audit_report(report: dict[str, object]) -> None:
    coverage = report["coverage"]
    quality = report["quality"]
    ai_assessment = report["ai_assessment"]

    print("AI 辅助号码段库评估报告")
    print("=" * 28)
    print(f"总记录数: {report['total_rows']}")
    print(f"唯一号码段: {report['unique_segments']}")
    print(
        "地区覆盖: "
        f"{coverage['covered_provinces']}/{coverage['expected_provinces']} "
        f"(缺失 {len(coverage['missing_provinces'])} 个)"
    )
    print(f"AI 评分: {ai_assessment['score']} / 100")
    print(f"可信度: {ai_assessment['confidence_level']}")

    missing = coverage["missing_provinces"]
    if missing:
        print("缺失省级地区:", "、".join(missing))

    invalid_segments = quality["invalid_segments"]
    duplicate_segments = quality["duplicate_segments"]
    conflicting_segments = quality["conflicting_segments"]

    print(f"异常号码段格式: {len(invalid_segments)}")
    print(f"重复号码段: {len(duplicate_segments)}")
    print(f"同号段多地区冲突: {len(conflicting_segments)}")
    print(f"说明: {ai_assessment['note']}")


def export_audit_report(report: dict[str, object], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    rows = load_rows(args.data)

    if args.ai_audit:
        report = audit_rows(rows)
        print_audit_report(report)
        if args.audit_output:
            export_audit_report(report, args.audit_output)
            print(f"评估报告已导出: {args.audit_output}")
        return

    if args.list_provinces:
        list_provinces(rows)
        return

    filtered = filter_rows(
        rows=rows,
        provinces=parse_multi_values(args.province),
        cities=parse_multi_values(args.city),
        operators=parse_multi_values(args.operator),
        segment_prefix=args.segment_prefix.strip(),
        keyword=args.keyword.strip(),
    )

    pretty_print(filtered)

    if args.output:
        export_csv(filtered, args.output)
        print(f"结果已导出: {args.output}")


if __name__ == "__main__":
    main()
