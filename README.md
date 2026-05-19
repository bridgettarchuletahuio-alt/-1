# 全国号码段筛选工具

一个轻量命令行工具，用于按地区、运营商、号码段前缀筛选号码段。

同时支持 AI 辅助评估号码段库完整度（覆盖、重复、冲突、格式异常）。

## 1. 快速开始

```bash
python3 phone_segment_tool.py --help
```

## 2. 常见用法

### 按省份筛选

```bash
python3 phone_segment_tool.py --province 广东
```

### 按多个省份筛选

```bash
python3 phone_segment_tool.py --province 广东,广西,海南
```

### 按城市和运营商筛选

```bash
python3 phone_segment_tool.py --city 成都 --operator 中国电信
```

### 按号码段前缀筛选

```bash
python3 phone_segment_tool.py --segment-prefix 139
```

### 关键字模糊筛选

```bash
python3 phone_segment_tool.py --keyword 虚拟运营商
```

### 导出筛选结果

```bash
python3 phone_segment_tool.py --province 江苏 --output output/jiangsu.csv
```

### 查看所有省份及数量

```bash
python3 phone_segment_tool.py --list-provinces
```

### AI 辅助判断号码段库完整度

```bash
python3 phone_segment_tool.py --ai-audit
```

### 导出 AI 评估报告（JSON）

```bash
python3 phone_segment_tool.py --ai-audit --audit-output output/audit_report.json
```

## 3. 数据格式

默认数据文件：`data/phone_segments.csv`

字段要求：
- `segment`：号码段（建议 7 位）
- `province`：省份/地区
- `city`：城市
- `operator`：运营商
- `type`：号码类型（如移动、虚拟运营商）

## 4. 说明

当前仓库内置的是示例数据，用于演示筛选流程。
如果你有完整全国号码段库，直接替换 `data/phone_segments.csv` 即可，无需修改程序。

AI 评估是启发式辅助判断，不等同于运营商或监管官方认证结论。

## 5. 本地离线索引（推荐）

当数据量变大时，直接扫 CSV 会明显变慢。可先构建本地 SQLite 索引库：

```bash
python3 build_local_index.py
```

可选参数：

```bash
python3 build_local_index.py \
	--csv data/phone_segments.csv \
	--db data/phone_segments_index.db \
	--rebuild
```

构建完成后会生成：
- `phone_segments` 表（字段：segment/province/city/operator/type）
- 常用查询索引：`segment`、`province`、`city`、`operator`
- 组合索引：`(province, operator)`、`(province, city)`

说明：
- `--rebuild` 会删除已有表并全量重建。
- 该索引库可用于本地离线查询与批处理，不依赖外部网络。

