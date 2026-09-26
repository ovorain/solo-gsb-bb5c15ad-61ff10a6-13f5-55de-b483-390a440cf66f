"""Per-row validation for the bulk item-bank import endpoint.

The import request deliberately does *not* fail at the Pydantic boundary on
the first malformed row: every row is validated independently and every
problem is returned together with its stable 1-based array index, so a
spreadsheet-sized upload can be fixed in a single pass.

A row that passes every check is returned as a :class:`NormalizedItem`
(strings stripped, option/answer keys upper-cased); anything else produces
one or more :class:`RowError` objects and no normalised row.  Duplicate ids
inside the same batch are reported on every occurrence after the first one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

MAX_IMPORT_ITEMS = 1_000
ID_MAX_LENGTH = 128
STEM_MAX_LENGTH = 2_000
CONTENT_TAG_MAX_LENGTH = 64
OPTION_KEY_MAX_LENGTH = 16
OPTION_TEXT_MAX_LENGTH = 500
MIN_OPTIONS = 2
MAX_OPTIONS = 8
# Bounds mirror the supported theta range; discrimination must be strictly
# positive because a <= 0 carries zero Fisher information everywhere.
DIFFICULTY_MIN = -6.0
DIFFICULTY_MAX = 6.0
DISCRIMINATION_MIN = 0.01
DISCRIMINATION_MAX = 5.0


@dataclass(frozen=True)
class RowError:
    """One validation problem on one input row."""

    row: int  # 1-based index in the request's ``items`` array
    code: str
    message: str
    field: str | None = None
    id: str | None = None  # parseable item id, when available


@dataclass(frozen=True)
class NormalizedItem:
    """A fully validated, canonicalised import row."""

    row: int
    id: str
    difficulty: float
    discrimination: float
    content_tag: str | None
    stem: str
    options: list[dict[str, str]]
    answer: str


def _is_finite_number(value: Any) -> bool:
    """Real JSON number: int/float but not bool, and not NaN/Infinity."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _validate_id(raw: dict[str, Any], row: int, errors: list[RowError]) -> str | None:
    if "id" not in raw:
        errors.append(RowError(row, "missing_field", "缺少必填字段 id", "id"))
        return None
    value = raw["id"]
    if not isinstance(value, str):
        errors.append(
            RowError(
                row,
                "invalid_type",
                f"id 必须是字符串，收到 {type(value).__name__}",
                "id",
            )
        )
        return None
    stripped = value.strip()
    if not stripped:
        errors.append(RowError(row, "invalid_id", "id 不能为空白字符串", "id"))
    elif len(stripped) > ID_MAX_LENGTH:
        errors.append(
            RowError(
                row,
                "invalid_id",
                f"id 长度不能超过 {ID_MAX_LENGTH} 个字符，当前 {len(stripped)} 个",
                "id",
            )
        )
    else:
        return stripped
    return None


def _validate_stem(
    raw: dict[str, Any], row: int, errors: list[RowError]
) -> str | None:
    if "stem" not in raw:
        errors.append(RowError(row, "missing_field", "缺少必填字段 stem", "stem"))
        return None
    value = raw["stem"]
    if not isinstance(value, str):
        errors.append(
            RowError(
                row,
                "invalid_type",
                f"stem 必须是字符串，收到 {type(value).__name__}",
                "stem",
            )
        )
        return None
    stripped = value.strip()
    if not stripped:
        errors.append(RowError(row, "invalid_stem", "stem 不能为空白字符串", "stem"))
    elif len(stripped) > STEM_MAX_LENGTH:
        errors.append(
            RowError(
                row,
                "invalid_stem",
                f"stem 长度不能超过 {STEM_MAX_LENGTH} 个字符，当前 {len(stripped)} 个",
                "stem",
            )
        )
    else:
        return stripped
    return None


def _validate_number(
    raw: dict[str, Any],
    row: int,
    field: str,
    label: str,
    lower: float,
    upper: float,
    errors: list[RowError],
) -> float | None:
    if field not in raw:
        errors.append(RowError(row, "missing_field", f"缺少必填字段 {field}", field))
        return None
    value = raw[field]
    if not _is_finite_number(value):
        errors.append(
            RowError(
                row,
                "invalid_type",
                f"{label}必须是有限数值（不接受布尔值、NaN 或 Infinity）",
                field,
            )
        )
        return None
    number = float(value)
    if not lower <= number <= upper:
        errors.append(
            RowError(
                row,
                "out_of_range",
                f"{label}必须在 [{lower}, {upper}] 之间，收到 {number}",
                field,
            )
        )
        return None
    return number


def _validate_content_tag(
    raw: dict[str, Any], row: int, errors: list[RowError]
) -> str | None:
    # Missing behaves like an explicit null: the item simply has no tag.
    value = raw.get("content_tag")
    if value is None:
        return None
    if not isinstance(value, str):
        errors.append(
            RowError(
                row,
                "invalid_type",
                f"content_tag 必须是字符串或 null，收到 {type(value).__name__}",
                "content_tag",
            )
        )
        return None
    stripped = value.strip()
    if len(stripped) > CONTENT_TAG_MAX_LENGTH:
        errors.append(
            RowError(
                row,
                "invalid_content_tag",
                f"content_tag 长度不能超过 {CONTENT_TAG_MAX_LENGTH} 个字符，"
                f"当前 {len(stripped)} 个",
                "content_tag",
            )
        )
        return None
    # A blank tag is meaningless (quotas/statistics treat '' like NULL);
    # normalise it to null instead of rejecting the whole row.
    return stripped or None


def _option_key(
    opt: dict[str, Any],
    row: int,
    index: int,
    seen: set[str],
    errors: list[RowError],
) -> str | None:
    location = f"options[{index}]"
    if "key" not in opt:
        errors.append(
            RowError(row, "missing_field", f"{location} 缺少必填字段 key", "options")
        )
        return None
    raw_key = opt["key"]
    if not isinstance(raw_key, str):
        errors.append(
            RowError(
                row,
                "invalid_type",
                f"{location}.key 必须是字符串，收到 {type(raw_key).__name__}",
                "options",
            )
        )
        return None
    key = raw_key.strip().upper()
    if not key:
        errors.append(
            RowError(row, "invalid_option", f"{location}.key 不能为空白字符串", "options")
        )
    elif len(key) > OPTION_KEY_MAX_LENGTH:
        errors.append(
            RowError(
                row,
                "invalid_option",
                f"{location}.key 长度不能超过 {OPTION_KEY_MAX_LENGTH} 个字符",
                "options",
            )
        )
    elif key in seen:
        errors.append(
            RowError(
                row,
                "duplicate_option_key",
                f"选项键 {key!r} 在该行 options 中重复",
                "options",
            )
        )
    else:
        seen.add(key)
        return key
    return None


def _option_text(
    opt: dict[str, Any], row: int, index: int, errors: list[RowError]
) -> str | None:
    location = f"options[{index}]"
    if "text" not in opt:
        errors.append(
            RowError(row, "missing_field", f"{location} 缺少必填字段 text", "options")
        )
        return None
    raw_text = opt["text"]
    if not isinstance(raw_text, str):
        errors.append(
            RowError(
                row,
                "invalid_type",
                f"{location}.text 必须是字符串，收到 {type(raw_text).__name__}",
                "options",
            )
        )
        return None
    text = raw_text.strip()
    if not text:
        errors.append(
            RowError(row, "invalid_option", f"{location}.text 不能为空白字符串", "options")
        )
    elif len(text) > OPTION_TEXT_MAX_LENGTH:
        errors.append(
            RowError(
                row,
                "invalid_option",
                f"{location}.text 长度不能超过 {OPTION_TEXT_MAX_LENGTH} 个字符",
                "options",
            )
        )
    else:
        return text
    return None


def _validate_options(
    raw: dict[str, Any], row: int, errors: list[RowError]
) -> list[dict[str, str]] | None:
    if "options" not in raw:
        errors.append(
            RowError(row, "missing_field", "缺少必填字段 options", "options")
        )
        return None
    value = raw["options"]
    if not isinstance(value, list):
        errors.append(
            RowError(
                row,
                "invalid_type",
                f"options 必须是数组，收到 {type(value).__name__}",
                "options",
            )
        )
        return None
    if not MIN_OPTIONS <= len(value) <= MAX_OPTIONS:
        errors.append(
            RowError(
                row,
                "invalid_options",
                f"options 必须包含 {MIN_OPTIONS}~{MAX_OPTIONS} 个选项，"
                f"当前 {len(value)} 个",
                "options",
            )
        )
        return None

    normalized: list[dict[str, str]] = []
    seen_keys: set[str] = set()
    failed = False
    for index, opt in enumerate(value):
        if not isinstance(opt, dict):
            errors.append(
                RowError(
                    row,
                    "invalid_type",
                    f"options[{index}] 必须是包含 key 与 text 的对象，"
                    f"收到 {type(opt).__name__}",
                    "options",
                )
            )
            failed = True
            continue
        key = _option_key(opt, row, index, seen_keys, errors)
        text = _option_text(opt, row, index, errors)
        if key is not None and text is not None:
            normalized.append({"key": key, "text": text})
        else:
            failed = True
    return None if failed else normalized


def _validate_answer(
    raw: dict[str, Any],
    row: int,
    options: list[dict[str, str]] | None,
    errors: list[RowError],
) -> str | None:
    if "answer" not in raw:
        errors.append(
            RowError(row, "missing_field", "缺少必填字段 answer", "answer")
        )
        return None
    value = raw["answer"]
    if not isinstance(value, str):
        errors.append(
            RowError(
                row,
                "invalid_type",
                f"answer 必须是字符串（选项键），收到 {type(value).__name__}",
                "answer",
            )
        )
        return None
    answer = value.strip().upper()
    if not answer:
        errors.append(
            RowError(row, "invalid_answer", "answer 不能为空白字符串", "answer")
        )
        return None
    # Membership can only be judged when the option set itself is valid.
    if options is not None and answer not in {o["key"] for o in options}:
        errors.append(
            RowError(
                row,
                "answer_not_in_options",
                f"正确答案 {answer!r} 不在选项键 "
                f"{sorted(o['key'] for o in options)} 中",
                "answer",
            )
        )
        return None
    return answer


def _validate_row(
    raw: Any, row: int, seen_ids: dict[str, int]
) -> tuple[NormalizedItem | None, list[RowError]]:
    if not isinstance(raw, dict):
        return None, [
            RowError(
                row,
                "invalid_row",
                f"每一行必须是 JSON 对象（键值对），收到 {type(raw).__name__}",
            )
        ]

    errors: list[RowError] = []
    item_id = _validate_id(raw, row, errors)
    stem = _validate_stem(raw, row, errors)
    difficulty = _validate_number(
        raw, row, "difficulty", "难度 difficulty",
        DIFFICULTY_MIN, DIFFICULTY_MAX, errors,
    )
    discrimination = _validate_number(
        raw, row, "discrimination", "区分度 discrimination",
        DISCRIMINATION_MIN, DISCRIMINATION_MAX, errors,
    )
    content_tag = _validate_content_tag(raw, row, errors)
    options = _validate_options(raw, row, errors)
    answer = _validate_answer(raw, row, options, errors)

    # In-batch duplicate: only the first occurrence is the "canonical" row;
    # every later occurrence is reported with the first one's line number.
    if item_id is not None:
        first_row = seen_ids.get(item_id)
        if first_row is not None:
            errors.append(
                RowError(
                    row,
                    "duplicate_id_in_batch",
                    f"题目 ID {item_id!r} 在本批第 {first_row} 行已出现，"
                    "同一批次不能重复",
                    "id",
                    item_id,
                )
            )
        else:
            seen_ids[item_id] = row

    if errors:
        return None, errors
    assert item_id is not None and stem is not None
    assert difficulty is not None and discrimination is not None
    assert options is not None and answer is not None
    return (
        NormalizedItem(
            row=row,
            id=item_id,
            difficulty=difficulty,
            discrimination=discrimination,
            content_tag=content_tag,
            stem=stem,
            options=options,
            answer=answer,
        ),
        [],
    )


def validate_rows(
    raw_rows: list[Any],
) -> tuple[list[NormalizedItem], list[RowError]]:
    """Validate every row.

    Returns ``(items, errors)``; ``items`` preserves the request order and
    contains only fully valid rows, ``errors`` is sorted by stable 1-based
    row number (and, within a row, by check order).
    """
    items: list[NormalizedItem] = []
    errors: list[RowError] = []
    seen_ids: dict[str, int] = {}
    for row_number, raw in enumerate(raw_rows, start=1):
        item, row_errors = _validate_row(raw, row_number, seen_ids)
        if item is not None:
            items.append(item)
        errors.extend(row_errors)
    return items, errors
