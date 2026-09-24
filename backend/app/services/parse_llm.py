from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from app.services.llm import _get_openai_client, _max_tokens, _resolve_model
from app.services.parser import parse_text

LOGGER = logging.getLogger("reportrx.backend")


class ParseExtractionError(Exception):
    def __init__(self, detail: str, status_code: int = 502) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


class LabResultRow(BaseModel):
    test_name: str
    result: str
    unit: str
    reference_range: str
    flag: Literal["H", "L"] | None = None


LAB_ROWS_JSON_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "test_name": {"type": "string"},
                    "result": {"type": "string"},
                    "unit": {"type": "string"},
                    "reference_range": {"type": "string"},
                    "flag": {"type": ["string", "null"]},
                },
                "required": ["test_name", "result", "unit", "reference_range", "flag"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["rows"],
    "additionalProperties": False,
}


SYSTEM_PROMPT = (
    "You extract ONLY pathology/lab test result rows from report text. "
    "Return JSON only with no markdown and no commentary. "
    "Output must be either a JSON array of rows or an object with a top-level 'rows' array. "
    "Each row must strictly match this schema: "
    '{"test_name": string, "result": string, "unit": string, "reference_range": string, '
    '"flag": "H" | "L" | null}. '
    "Exclude all non-test metadata, including patient name, DOB, age, sex, Medicare number, "
    "provider number, requesting doctor, specimen type, report ID, lab name, ABN, and any row where "
    "the value is a unit string with no associated test name. "
    "Do not invent values."
)

_METADATA_TOKEN = re.compile(
    r"\b(patient\s*name|dob|date\s*of\s*birth|age|sex|gender|medicare\s*(?:number|no\.?|num\.?|#)|"
    r"provider\s*(?:number|no\.?|num\.?|#)|requesting\s*(?:doctor|dr\.?|physician)|doctor|specimen\s*type|report\s*id|"
    r"lab\s*name|abn)\b",
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


def _strip_json_fences(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def _try_parse_json_payload(text: str) -> Any | None:
    """Best-effort parse for model output that may wrap JSON in extra text.

    We first try the full string. If that fails, attempt to parse the broadest
    array/object slices found in the text.
    """
    candidates: list[str] = []
    cleaned = (text or "").strip()
    if cleaned:
        candidates.append(cleaned)

    arr_start = cleaned.find("[")
    arr_end = cleaned.rfind("]")
    if arr_start != -1 and arr_end != -1 and arr_end > arr_start:
        candidates.append(cleaned[arr_start : arr_end + 1].strip())

    obj_start = cleaned.find("{")
    obj_end = cleaned.rfind("}")
    if obj_start != -1 and obj_end != -1 and obj_end > obj_start:
        candidates.append(cleaned[obj_start : obj_end + 1].strip())

    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def _should_exclude_row(row: LabResultRow) -> bool:
    name = (row.test_name or "").strip()
    result = (row.result or "").strip()
    unit = (row.unit or "").strip()
    reference_range = (row.reference_range or "").strip()

    if not name or not result:
        return True
    if _METADATA_TOKEN.search(name):
        return True
    if name.lower() in {"unit", "units"}:
        return True
    if _TEST_NAME_NOISE.fullmatch(name):
        return True
    if _PERSON_NAME_LIKE.fullmatch(name) and _VALUE_NOISE.search(result):
        return True
    if _MONTH_OR_AGE_UNIT.fullmatch(unit) and (_PERSON_NAME_LIKE.fullmatch(name) or not name):
        return True
    if _REPORT_CODE_LIKE.fullmatch(name) and re.fullmatch(r"\d{6}(?:\.0+)?", result):
        return True
    if _UNIT_ONLY.fullmatch(result) and not unit and not reference_range:
        return True
    return False


def _load_rows_from_json(raw: str) -> list[LabResultRow]:
    cleaned = _strip_json_fences(raw)
    payload = _try_parse_json_payload(cleaned)
    if payload is None:
        raise ParseExtractionError(
            "Malformed JSON from extraction model. Please retry parsing this report.",
            status_code=502,
        )

    if isinstance(payload, dict):
        rows_raw = payload.get("rows")
    elif isinstance(payload, list):
        rows_raw = payload
    else:
        raise ParseExtractionError(
            "Extraction model returned an unexpected JSON shape. Please retry.",
            status_code=502,
        )

    if not isinstance(rows_raw, list):
        raise ParseExtractionError(
            "Extraction model did not return a rows array. Please retry.",
            status_code=502,
        )

    rows: list[LabResultRow] = []
    for index, item in enumerate(rows_raw):
        try:
            parsed = LabResultRow.model_validate(item)
        except ValidationError as exc:
            LOGGER.warning(
                {
                    "event": "parse_row_rejected",
                    "index": index,
                    "reason": "schema_validation_failed",
                    "errors": exc.errors(),
                }
            )
            continue

        if _should_exclude_row(parsed):
            LOGGER.info(
                {
                    "event": "parse_row_rejected",
                    "index": index,
                    "reason": "excluded_non_lab_row",
                    "test_name": parsed.test_name,
                }
            )
            continue
        rows.append(parsed)
    return rows


def _fallback_rows_from_text(text: str) -> list[LabResultRow]:
    rows, _ = parse_text(text or "")
    fallback_rows: list[LabResultRow] = []
    for row in rows:
        result = row.value_text or (str(row.value) if row.value is not None else "")
        if not result:
            continue
        flag: Literal["H", "L"] | None
        if row.flag == "high":
            flag = "H"
        elif row.flag == "low":
            flag = "L"
        else:
            flag = None
        try:
            candidate = LabResultRow(
                test_name=row.test_name,
                result=result,
                unit=row.unit or "",
                reference_range=row.reference_range or "",
                flag=flag,
            )
        except ValidationError:
            continue
        if _should_exclude_row(candidate):
            continue
        fallback_rows.append(candidate)
    return fallback_rows


def _run_openai_extraction(text: str) -> str:
    client = _get_openai_client()
    model = _resolve_model(os.getenv("OPENAI_MODEL", "gpt-5"))
    kwargs: dict[str, Any] = {
        "model": model,
        "instructions": SYSTEM_PROMPT,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "lab_result_rows",
                "description": "Extracted lab result rows from a pathology report.",
                "schema": LAB_ROWS_JSON_SCHEMA,
                "strict": True,
            }
        },
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "Extract lab rows from the report text below. Return JSON only.\n\nREPORT_TEXT:\n"
                        + (text or ""),
                    }
                ],
            }
        ],
        "max_output_tokens": _max_tokens(),
    }
    lower_model = model.lower()
    if not (
        model.startswith("gpt-5")
        or lower_model.startswith("o")
        or "omni" in lower_model
        or lower_model.startswith("gpt-4.1")
    ):
        kwargs["temperature"] = 0

    response = client.responses.create(**kwargs)

    out_text = getattr(response, "output_text", None)
    if isinstance(out_text, str) and out_text.strip():
        return out_text

    # Defensive fallback for SDK payload variants.
    dumped: dict[str, Any] = {}
    if hasattr(response, "model_dump"):
        dumped = response.model_dump()
    elif hasattr(response, "dict"):
        dumped = response.dict()  # type: ignore[assignment]

    if isinstance(dumped.get("output_text"), str) and dumped["output_text"].strip():
        return dumped["output_text"]

    parts: list[str] = []
    for item in dumped.get("output", []) if isinstance(dumped, dict) else []:
        for content in item.get("content", []) if isinstance(item, dict) else []:
            text_value = content.get("text") if isinstance(content, dict) else None
            if isinstance(text_value, str) and text_value.strip():
                parts.append(text_value)

    return "".join(parts)


async def extract_lab_rows_with_openai(text: str) -> list[LabResultRow]:
    try:
        raw = await asyncio.to_thread(_run_openai_extraction, text)
        return _load_rows_from_json(raw)
    except ParseExtractionError:
        raise
    except RuntimeError as exc:
        message = str(exc)
        if message == "missing_api_key":
            LOGGER.info({"event": "parse_fallback", "reason": "missing_api_key"})
            return _fallback_rows_from_text(text)
        LOGGER.info({"event": "parse_fallback", "reason": "runtime_error", "message": message})
        return _fallback_rows_from_text(text)
    except Exception as exc:
        LOGGER.exception("OpenAI parse extraction failed")
        message = str(exc)
        if isinstance(exc, json.JSONDecodeError):
            raise ParseExtractionError(
                "Malformed JSON from extraction model. Please retry parsing this report.",
                status_code=502,
            ) from exc
        LOGGER.info({"event": "parse_fallback", "reason": type(exc).__name__, "message": message})
        return _fallback_rows_from_text(text)
