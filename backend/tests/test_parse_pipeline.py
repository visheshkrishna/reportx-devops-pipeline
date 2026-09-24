from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from app.services.parse_pipeline import (
    ParseConfig,
    ParseServiceError,
    build_parse_response,
    extract_text_from_json_payload,
    extract_text_from_uploads,
)


@dataclass
class FakeUpload:
    filename: str
    content_type: str
    data: bytes

    async def read(self) -> bytes:
        return self.data


def test_extract_text_from_uploads_rejects_too_many_files():
    uploads = [
        FakeUpload(filename=f"report-{idx}.pdf", content_type="application/pdf", data=b"pdf")
        for idx in range(6)
    ]

    with pytest.raises(ParseServiceError) as exc_info:
        asyncio.run(
            extract_text_from_uploads(uploads, content_length=None, config=ParseConfig(max_files=5))
        )

    assert exc_info.value.status_code == 413
    assert exc_info.value.detail == "Too many files (max 5)."


def test_extract_text_from_json_payload_requires_text_field():
    with pytest.raises(ParseServiceError) as exc_info:
        extract_text_from_json_payload("application/json", {"rows": []})

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Body must include 'text'."


def test_build_parse_response_preserves_row_order_and_contract_keys():
    response = build_parse_response(
        "Glucose 92 mg/dL 70-99\nALT 61 U/L 0-55",
    )

    assert list(response.keys()) == ["rows", "unparsed_lines", "unparsed", "meta", "extracted_text"]
    assert [row["display_name"] for row in response["rows"]] == ["Glucose", "ALT"]
    assert response["rows"][0]["id"] == "r1"
    assert response["rows"][1]["id"] == "r2"
    assert response["meta"] == {}
    assert response["extracted_text"].startswith("Glucose 92")
