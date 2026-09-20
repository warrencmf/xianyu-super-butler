"""Parse uploaded carrier rate books (报价表) into normalized route rows.

This is a *replacement* implementation.  The upstream repository references
``app.routers.logistics_quote`` from ``reply_server`` but never committed the
module, so the parsing contract had to be reconstructed from the frontend
type definitions (``frontend/services/api.ts``) and the database schema
(``logistics_quote_books`` / ``logistics_quote_routes``).

Scope, deliberately kept narrow:

* Read ``.xlsx`` / ``.xlsm`` / ``.xls`` / ``.csv``.
* Locate the header row per sheet and map columns through a synonym table.
* Emit one normalized row per route with the numeric fields the frontend
  expects, plus a per-row confidence score and review state.
* Report mapping gaps and anomalies as warnings instead of failing the upload.

It is intentionally forgiving: an unrecognised sheet yields warnings, not an
exception, because rate books are hand-maintained and rarely uniform.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from loguru import logger

__all__ = [
    "PARSER_VERSION",
    "SUPPORTED_EXTENSIONS",
    "ParseError",
    "parse_rate_book",
]

PARSER_VERSION = "reconstructed-1.0"

SUPPORTED_EXTENSIONS = ("xlsx", "xlsm", "xls", "csv")

#: 触发"这行是表头"的最少可识别列数。
_MIN_HEADER_HITS = 2

#: 表头行搜索深度，报价表通常前几行是标题/说明。
_MAX_HEADER_SCAN_ROWS = 12


class ParseError(ValueError):
    """Raised when the upload cannot be read at all."""


# 列同义词表：规范字段 -> 表头里可能出现的写法（小写、去空格后比较）。
_COLUMN_SYNONYMS: dict[str, tuple[str, ...]] = {
    "carrier": ("承运商", "快递", "快递公司", "物流", "物流公司", "carrier", "渠道", "服务", "服务商"),
    "origin_province": ("始发省", "出发省", "发件省", "寄出省", "origin_province"),
    "origin_city": ("始发城市", "始发地", "出发城市", "发件城市", "寄出城市", "出发地", "起运城市", "origin_city", "始发"),
    "origin": ("始发", "出发", "起运地", "origin"),
    "destination_province": ("目的省", "到达省", "收货省", "destination_province"),
    "destination_city": ("目的城市", "目的地", "到达城市", "收货城市", "目的城市名", "destination_city", "到达"),
    "destination": ("目的地", "到达地", "收货地", "destination"),
    "first_weight_kg": ("首重", "首重kg", "首重(kg)", "首重重量", "first_weight", "first_weight_kg"),
    "first_price": ("首重价格", "首重价", "首重费用", "首重(元)", "首重价格(元)", "first_price", "首费"),
    "continued_unit_kg": ("续重", "续重kg", "续重(kg)", "续重单位", "continued_unit", "continued_unit_kg"),
    "continued_price": ("续重价格", "续重价", "续重费用", "续重(元)", "续重价格(元)", "continued_price", "续费"),
    "quote": ("报价", "总价", "价格", "运费", "费用", "quote", "金额"),
    "book_kind": ("类型", "报价类型", "book_kind", "业务类型"),
}

#: 反向索引：表头文本 -> 规范字段。构建一次，解析时 O(1) 命中。
_SYNONYM_INDEX: dict[str, str] = {}
for _canonical, _aliases in _COLUMN_SYNONYMS.items():
    for _alias in _aliases:
        _SYNONYM_INDEX.setdefault(_alias, _canonical)


def _normalize_header(value: Any) -> str:
    """Normalize a header cell so lookups survive formatting noise."""
    text = "" if value is None else str(value)
    text = text.strip().lower()
    text = re.sub(r"[\s\u3000]+", "", text)
    text = text.replace("（", "(").replace("）", ")")
    text = text.replace("*", "").replace("：", "").replace(":", "")
    return text


def _looks_like_header(row: Iterable[Any]) -> int:
    """Return how many cells of ``row`` map to a known column."""
    hits = 0
    for cell in row:
        if _normalize_header(cell) in _SYNONYM_INDEX:
            hits += 1
    return hits


def _to_float(value: Any) -> Optional[float]:
    """Best-effort numeric parse; returns ``None`` when not a number."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    text = text.replace(",", "").replace("，", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group())
    except ValueError:
        return None


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _split_place(value: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Split a combined "广东省 深圳市" style cell into (province, city)."""
    if not value:
        return None, None
    parts = [part for part in re.split(r"[\s/\-—,，]+", value) if part]
    if not parts:
        return None, None
    if len(parts) == 1:
        return None, parts[0]
    return parts[0], parts[-1]


def _detect_rule_type(columns: set[str]) -> Optional[str]:
    """Infer the pricing model from which columns the sheet actually has."""
    has_first = {"first_price"} <= columns
    has_continued = {"continued_price"} <= columns
    has_quote = "quote" in columns
    if has_first and has_continued:
        return "first_additional"
    if has_quote:
        return "minimum_then_per_kg"
    if has_first:
        return "minimum_then_per_kg"
    return None


def _detect_book_kind(carriers: list[str], headers: set[str]) -> Optional[str]:
    """Classify the book as express (快递) or logistics (物流/专线)."""
    if "book_kind" in headers:
        return None  # 由逐行数据决定
    joined = " ".join(carriers)
    if any(token in joined for token in ("物流", "专线", "货运", "零担")):
        return "logistics"
    if any(token in joined for token in ("快递", "快运", "顺丰", "中通", "圆通", "韵达", "申通", "极兔", "邮政", "EMS")):
        return "express"
    return None


@dataclass
class _SheetParse:
    """Intermediate result for one worksheet."""

    sheet_name: str
    header_row: int = 0
    mapping: dict[str, str] = field(default_factory=dict)
    unmatched: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rule_type: Optional[str] = None


def parse_rate_book(
    filename: str,
    content: bytes,
    *,
    max_rows_per_sheet: int = 20000,
) -> dict[str, Any]:
    """Parse an uploaded rate book into the API response contract.

    Raises :class:`ParseError` for unreadable files or unsupported extensions.
    """
    if not content:
        raise ParseError("上传文件为空")

    extension = (filename.rsplit(".", 1)[-1] if "." in filename else "").lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise ParseError(
            f"不支持的文件类型 .{extension}，请上传 "
            + " / ".join(f".{item}" for item in SUPPORTED_EXTENSIONS)
        )

    digest = hashlib.sha256(content).hexdigest()
    warnings: list[str] = []

    if extension == "csv":
        sheets = _read_csv_sheets(content)
    else:
        sheets = _read_excel_sheets(content, extension, warnings)

    if not sheets:
        raise ParseError("文件里没有可读取的工作表")

    parsed_sheets: list[_SheetParse] = []
    for sheet_name, table in sheets:
        result = _parse_sheet(sheet_name, table, max_rows_per_sheet, warnings)
        if result is not None:
            parsed_sheets.append(result)

    if not parsed_sheets:
        raise ParseError(
            "没有识别到有效的报价表头，请确认表格包含承运商/始发地/目的地等列"
        )

    services: list[dict[str, Any]] = []
    carriers_seen: dict[str, dict[str, Any]] = {}
    all_rows: list[dict[str, Any]] = []
    total_valid = total_review = total_rejected = 0
    headers_union: set[str] = set()

    for parsed in parsed_sheets:
        headers_union |= set(parsed.mapping.keys())
        for row in parsed.rows:
            all_rows.append(row)
            state = row["review_state"]
            if state == "valid":
                total_valid += 1
            elif state == "review":
                total_review += 1
            else:
                total_rejected += 1
            carrier_name = row.get("carrier") or "未标注承运商"
            entry = carriers_seen.setdefault(
                carrier_name,
                {"name": carrier_name, "route_count": 0, "quote_count": 0},
            )
            entry["route_count"] += 1
            if row.get("quote") is not None:
                entry["quote_count"] += 1

        services.append(
            {
                "name": parsed.sheet_name,
                "sheet_name": parsed.sheet_name,
                "row_count": len(parsed.rows),
                "route_count": len(parsed.rows),
                "rule_type": parsed.rule_type or "unknown",
                "book_kind": _detect_book_kind(
                    sorted({r.get("carrier") for r in parsed.rows if r.get("carrier")}),
                    set(parsed.mapping.keys()),
                ),
                "mapping": parsed.mapping,
            }
        )

    book_kind = _detect_book_kind(list(carriers_seen), headers_union)
    total_rows = len(all_rows)

    if total_rejected:
        warnings.append(f"{total_rejected} 行缺少关键字段，已标记为 rejected")
    if total_review:
        warnings.append(f"{total_review} 行存在可疑数值，已标记为 review")

    return {
        "success": True,
        "mode": "rate_book_summary" if len(parsed_sheets) > 1 else "carrier_only",
        "source": {
            "filename": filename,
            "size": len(content),
            "sha256": digest,
            "content_type": None,
            "file_type": extension,
            "parser_version": PARSER_VERSION,
            "status": "parsed" if total_rows and not total_rejected else "needs_review",
        },
        "mapping": {
            "matched": parsed_sheets[0].mapping if len(parsed_sheets) == 1 else {
                key: value for parsed in parsed_sheets for key, value in parsed.mapping.items()
            },
            "unmatched": sorted({item for parsed in parsed_sheets for item in parsed.unmatched}),
        },
        "summary": {
            "total": total_rows,
            "valid": total_valid,
            "review": total_review,
            "rejected": total_rejected,
        },
        "book_kind": book_kind,
        "service_count": len(services),
        "route_count": total_rows,
        "services": services,
        "carriers": sorted(carriers_seen.values(), key=lambda item: item["name"]),
        "rows": all_rows[:500],
        "sample_row": all_rows[0] if all_rows else None,
        "warning_count": len(warnings),
        "warnings": warnings,
    }


def _read_csv_sheets(content: bytes) -> list[tuple[str, list[list[Any]]]]:
    """Read a CSV upload, tolerating the encodings Chinese exports use."""
    text: Optional[str] = None
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ParseError("无法识别 CSV 编码，请另存为 UTF-8 或 GBK")

    reader = csv.reader(io.StringIO(text))
    return [("CSV", [list(row) for row in reader])]


def _read_excel_sheets(
    content: bytes,
    extension: str,
    warnings: list[str],
) -> list[tuple[str, list[list[Any]]]]:
    """Read every worksheet of an Excel upload into plain lists."""
    try:
        import pandas as pd
    except ImportError as exc:  # pragma: no cover - pandas is a hard dependency
        raise ParseError("缺少 pandas，无法解析 Excel 文件") from exc

    engine = "xlrd" if extension == "xls" else "openpyxl"
    try:
        workbook = pd.read_excel(
            io.BytesIO(content),
            sheet_name=None,
            header=None,
            dtype=object,
            engine=engine,
        )
    except Exception as exc:  # noqa: BLE001 - surface any reader failure uniformly
        if extension == "xls":
            raise ParseError(
                "读取 .xls 失败，请另存为 .xlsx 后重试"
            ) from exc
        raise ParseError(f"读取 Excel 失败：{exc}") from exc

    sheets: list[tuple[str, list[list[Any]]]] = []
    for sheet_name, frame in workbook.items():
        table = frame.where(frame.notna(), None).values.tolist()
        if not table:
            warnings.append(f"工作表「{sheet_name}」为空，已跳过")
            continue
        sheets.append((str(sheet_name), table))
    return sheets


def _parse_sheet(
    sheet_name: str,
    table: list[list[Any]],
    max_rows: int,
    warnings: list[str],
) -> Optional[_SheetParse]:
    """Locate the header row and normalize every data row of one sheet."""
    header_index: Optional[int] = None
    for index, row in enumerate(table[:_MAX_HEADER_SCAN_ROWS]):
        if _looks_like_header(row) >= _MIN_HEADER_HITS:
            header_index = index
            break

    if header_index is None:
        warnings.append(f"工作表「{sheet_name}」未识别到表头，已跳过")
        return None

    header_row = table[header_index]
    mapping: dict[str, str] = {}
    unmatched: list[str] = []

    for position, cell in enumerate(header_row):
        label = _text(cell)
        if not label:
            continue
        canonical = _SYNONYM_INDEX.get(_normalize_header(label))
        if canonical and canonical not in mapping:
            mapping[canonical] = label
        elif not canonical:
            unmatched.append(label)

    if not mapping:
        warnings.append(f"工作表「{sheet_name}」表头无法映射到任何已知字段，已跳过")
        return None

    # 规范字段 -> 列下标
    index_of: dict[str, int] = {}
    for position, cell in enumerate(header_row):
        canonical = _SYNONYM_INDEX.get(_normalize_header(cell))
        if canonical and canonical not in index_of:
            index_of[canonical] = position

    parsed = _SheetParse(
        sheet_name=sheet_name,
        header_row=header_index,
        mapping=mapping,
        unmatched=unmatched,
        rule_type=_detect_rule_type(set(mapping.keys())),
    )

    for row in table[header_index + 1:]:
        if len(parsed.rows) >= max_rows:
            warnings.append(
                f"工作表「{sheet_name}」超过 {max_rows} 行，其余行已截断"
            )
            break
        record = _build_row(row, index_of, parsed.rule_type)
        if record is not None:
            parsed.rows.append(record)

    if not parsed.rows:
        warnings.append(f"工作表「{sheet_name}」表头已识别但没有数据行")
    return parsed


def _build_row(
    row: list[Any],
    index_of: dict[str, int],
    rule_type: Optional[str],
) -> Optional[dict[str, Any]]:
    """Normalize one data row, or return ``None`` when it is clearly blank."""

    def cell(field_name: str) -> Any:
        position = index_of.get(field_name)
        if position is None or position >= len(row):
            return None
        return row[position]

    if all(_text(value) is None for value in row):
        return None

    origin_city = _text(cell("origin_city"))
    dest_city = _text(cell("destination_city"))
    origin_province = _text(cell("origin_province"))
    dest_province = _text(cell("destination_province"))
    origin_combined = _text(cell("origin"))
    destination_combined = _text(cell("destination"))

    # 报价表常把"省+市"塞在一列里（"广东省 广州市"）。只要省缺失就尝试拆一次：
    # 拆出省就补上，拆不出省（例如只写了"深圳市"）则保持原样。
    if not origin_province:
        source = origin_combined or origin_city
        if source:
            origin_province, origin_city = _split_place(source)
    if not dest_province:
        source = destination_combined or dest_city
        if source:
            dest_province, dest_city = _split_place(source)

    origin = origin_combined or origin_city
    destination = destination_combined or dest_city

    first_weight = _to_float(cell("first_weight_kg"))
    first_price = _to_float(cell("first_price"))
    continued_unit = _to_float(cell("continued_unit_kg"))
    continued_price = _to_float(cell("continued_price"))
    quote = _to_float(cell("quote"))

    issues: list[str] = []
    if not (origin_city or origin_province or origin):
        issues.append("缺少始发地")
    if not (dest_city or dest_province or destination):
        issues.append("缺少目的地")

    has_any_price = any(
        value is not None
        for value in (quote, first_price, continued_price)
    )
    if not has_any_price:
        issues.append("缺少价格字段")

    for label, value in (
        ("首重", first_weight),
        ("首重价格", first_price),
        ("续重", continued_unit),
        ("续重价格", continued_price),
        ("报价", quote),
    ):
        if value is not None and value < 0:
            issues.append(f"{label}为负数")

    # 置信度按"关键字段齐不齐"打分，前端用它决定默认筛选视图。
    confidence = 1.0
    confidence -= 0.25 * len([issue for issue in issues if "缺少" in issue])
    confidence -= 0.15 * len([issue for issue in issues if "负数" in issue])
    confidence = round(max(0.0, min(1.0, confidence)), 2)

    if not issues:
        review_state = "valid"
    elif has_any_price and (origin_city or origin_province) and (dest_city or dest_province):
        review_state = "review"
    else:
        review_state = "rejected"

    return {
        "carrier": _text(cell("carrier")),
        "service_name": _text(cell("carrier")),
        "origin_province": origin_province,
        "origin_city": origin_city,
        "origin": origin,
        "destination_province": dest_province,
        "destination_city": dest_city,
        "destination": destination,
        "first_weight_kg": first_weight,
        "first_price": first_price,
        "continued_unit_kg": continued_unit,
        "continued_price": continued_price,
        "continued_tiers": None,
        "fixed_tiers": None,
        "rule_type": rule_type,
        "book_kind": _text(cell("book_kind")),
        "quote": quote,
        "confidence": confidence,
        "review_state": review_state,
        "issues": issues,
        "raw": {
            str(key): ("" if value is None else str(value))
            for key, value in zip(sorted(index_of, key=lambda k: index_of[k]), row)
        },
    }


def summarize_rows(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Count review states; helper for callers that already hold parsed rows."""
    summary = {"total": len(rows), "valid": 0, "review": 0, "rejected": 0}
    for row in rows:
        state = row.get("review_state")
        if state in summary:
            summary[state] += 1
    return summary
