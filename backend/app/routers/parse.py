from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from app.services.parse_llm import ParseExtractionError, extract_lab_rows_with_openai
from app.services.parse_pipeline import (
    ParseServiceError,
    collect_uploads,
    extract_text_from_uploads,
)
from app.services.parser import extract_report_date

router = APIRouter()

_META_NAME = re.compile(
    r"\b(patient\s*name|dob|date\s*of\s*birth|age|sex|gender|medicare\s*(?:number|no\.?|num\.?|#)|"
    r"provider\s*(?:number|no\.?|num\.?|#)|requesting\s*(?:doctor|dr\.?|physician)|doctor|specimen\s*type|report\s*id|lab\s*name|abn)\b",
    re.IGNORECASE,
)

_UNIT_ONLY = re.compile(
    r"^(%|[a-zA-Zμµ]+(?:/[a-zA-Zμµ]+)?|(?:x?10\^\d+/?[a-zA-Zμµ]+)|(?:10\^\d+/?[a-zA-Zμµ]+))$",
    re.IGNORECASE,
)

_VALUE_NOISE = re.compile(
    r"^(?:\d{1,2}[:/]\d{1,2}(?:[:/]\d{2,4})?|\d{1,4}\s*(?:years?|yrs?|yo|y/o)|"
    r"\d{4,}\s*[A-Z]?$|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)\b|"
    r"(?:mon|tue|wed|thu|fri|sat|sun)\b)",
    re.IGNORECASE,
)

_MONTH_OR_AGE_UNIT = re.compile(
    r"^(?:jan|january|feb|february|mar|march|apr|april|may|jun|june|jul|july|aug|august|"
    r"sep|sept|september|oct|october|nov|november|dec|december|years?|yrs?|yo|y/o|[A-Z])$",
    re.IGNORECASE,
)

_TEST_NAME_NOISE = re.compile(
    r"^(?:x10\^.*|[a-z]\/L|[a-z]{1,3}\/L|[A-Z]\/L|[A-Z]\/mL|[A-Z]\/dL|[A-Z]\/UL|[A-Z]\/uL)$",
    re.IGNORECASE,
)

_REPORT_CODE_LIKE = re.compile(r"^[A-Z0-9]{2,}(?:-[A-Z0-9]{2,})+$")

_PERSON_NAME_LIKE = re.compile(r"^[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3}$")


def _row_get(row: Any, field: str) -> Any:
    if isinstance(row, dict):
        return row.get(field)
    return getattr(row, field, None)


def _coerce_value(result_text: str) -> tuple[float | str, float | None]:
    raw = (result_text or "").strip()
    normalized = raw.replace(",", "")
    try:
        numeric = float(normalized)
        return numeric, numeric
    except Exception:
        return raw, None


def _flag_from_hl(flag: str | None) -> str | None:
    if flag == "H":
        return "high"
    if flag == "L":
        return "low"
    return None


def _is_excluded_row(test_name: str, result: str, unit: str, reference_range: str) -> bool:
    if not test_name or not result:
        return True
    if _META_NAME.search(test_name):
        return True
    if test_name.lower() in {"unit", "units"}:
        return True
    if _TEST_NAME_NOISE.fullmatch(test_name):
        return True
    if _PERSON_NAME_LIKE.fullmatch(test_name) and _VALUE_NOISE.search(result):
        return True
    if _MONTH_OR_AGE_UNIT.fullmatch(unit) and (
        _PERSON_NAME_LIKE.fullmatch(test_name) or not test_name
    ):
        return True
    if _REPORT_CODE_LIKE.fullmatch(test_name) and re.fullmatch(r"\d{6}(?:\.0+)?", result):
        return True
    if _UNIT_ONLY.fullmatch(result) and not unit and not reference_range:
        return True
    return False


@router.post("/parse")
async def parse_endpoint(
    request: Request,
    file: UploadFile | None = File(default=None),
    files: list[UploadFile] | None = File(default=None),
) -> dict[str, Any]:
    try:
        content_length = int(request.headers.get("content-length", "0"))
    except Exception:
        content_length = None

    upload_list = collect_uploads(file, files)

    if upload_list:
        try:
            text_content = await extract_text_from_uploads(
                upload_list,
                content_length=content_length,
            )
        except ParseServiceError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    else:
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body.")
        if not isinstance(payload, dict) or "text" not in payload:
            raise HTTPException(status_code=400, detail="Body must include 'text'.")
        text_content = str(payload.get("text") or "")

    source_text = text_content or ""
    try:
        rows = await extract_lab_rows_with_openai(source_text)
    except ParseExtractionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    unparsed: list[str] = []
    observed_at = extract_report_date(source_text)

    payload_rows = []
    for i, r in enumerate(rows, start=1):
        test_name = str(_row_get(r, "test_name") or "").strip()
        result = str(_row_get(r, "result") or "").strip()
        unit = str(_row_get(r, "unit") or "").strip()
        reference_range = str(_row_get(r, "reference_range") or "").strip()
        flag = _row_get(r, "flag")

        if _is_excluded_row(test_name, result, unit, reference_range):
            continue

        value, value_num = _coerce_value(result)
        value_text = result or None
        row_id = f"r{len(payload_rows) + 1}"
        d = {
            "id": row_id,
            "test_name": test_name,
            "test_name_raw": test_name,
            "value": value,
            "value_text": value_text,
            "value_num": value_num,
            "unit": unit,
            "unit_raw": unit,
            "reference_range": reference_range,
            "comparator": None,
            "flag": _flag_from_hl(flag),
            "confidence": 0.95,
            "page": None,
            "bbox": None,
            "raw_line": None,
        }
        payload_rows.append(d)

    return {
        "rows": payload_rows,
        "unparsed_lines": unparsed,
        "unparsed": [{"page": None, "text": s} for s in unparsed],
        "meta": {
            "report_date": observed_at.isoformat() if observed_at else None,
        },
        "extracted_text": source_text,
    }
