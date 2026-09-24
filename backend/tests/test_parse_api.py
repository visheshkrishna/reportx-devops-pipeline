from __future__ import annotations

import asyncio
import io

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers import parse as parse_router
from app.services import parse_llm, parse_pipeline
from app.services.parse_llm import ParseExtractionError


@pytest.fixture
def stub_structured_extraction(monkeypatch):
    def _apply(rows):
        async def fake_extract(_: str):
            return rows

        monkeypatch.setattr(parse_router, "extract_lab_rows_with_openai", fake_extract)

    return _apply


def test_parse_json_body_returns_expected_contract(stub_structured_extraction):
    stub_structured_extraction(
        [
            {
                "test_name": "Glucose",
                "result": "92",
                "unit": "mg/dL",
                "reference_range": "70-99",
                "flag": None,
            },
            {
                "test_name": "ALT",
                "result": "61",
                "unit": "U/L",
                "reference_range": "0-55",
                "flag": "H",
            },
        ]
    )
    client = TestClient(app)

    response = client.post(
        "/api/v1/parse",
        json={"text": "Glucose 92 mg/dL 70-99\nALT 61 U/L 0-55"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert list(payload.keys()) == ["rows", "unparsed_lines", "unparsed", "meta", "extracted_text"]
    assert [row["test_name"] for row in payload["rows"]] == ["Glucose", "ALT"]
    assert payload["rows"][0]["id"] == "r1"
    assert payload["rows"][1]["id"] == "r2"
    assert payload["extracted_text"].startswith("Glucose 92")


def test_parse_multiple_uploads_preserves_extracted_text_order(
    monkeypatch, stub_structured_extraction
):
    stub_structured_extraction(
        [
            {
                "test_name": "Glucose",
                "result": "92",
                "unit": "mg/dL",
                "reference_range": "70-99",
                "flag": None,
            },
            {
                "test_name": "ALT",
                "result": "61",
                "unit": "U/L",
                "reference_range": "0-55",
                "flag": "H",
            },
        ]
    )
    client = TestClient(app)

    def fake_pdf(_: bytes, max_pages: int = 10, ocr_lang: str | None = None) -> str:
        return "Glucose 92 mg/dL 70-99"

    def fake_image(_: bytes, lang: str | None = None) -> str:
        return "ALT 61 U/L 0-55"

    monkeypatch.setattr(parse_pipeline, "extract_text_from_pdf_bytes", fake_pdf)
    monkeypatch.setattr(parse_pipeline, "extract_text_from_image_bytes", fake_image)

    files = [
        ("files", ("report.pdf", io.BytesIO(b"%PDF-1.4"), "application/pdf")),
        ("files", ("scan.png", io.BytesIO(b"png"), "image/png")),
    ]

    response = client.post("/api/v1/parse", files=files)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["extracted_text"] == "Glucose 92 mg/dL 70-99\nALT 61 U/L 0-55"
    assert [row["test_name"] for row in payload["rows"]] == ["Glucose", "ALT"]


def test_parse_excludes_metadata_and_orphan_unit_rows(stub_structured_extraction):
    noisy_rows = [
        {
            "test_name": "Patient Name",
            "result": "John Citizen",
            "unit": "",
            "reference_range": "",
            "flag": None,
        },
        {
            "test_name": "DOB",
            "result": "1984-01-02",
            "unit": "",
            "reference_range": "",
            "flag": None,
        },
        {
            "test_name": "Medicare Number",
            "result": "1234567890",
            "unit": "",
            "reference_range": "",
            "flag": None,
        },
        {
            "test_name": "Unit",
            "result": "mg/dL",
            "unit": "",
            "reference_range": "",
            "flag": None,
        },
        {
            "test_name": "Glucose",
            "result": "92",
            "unit": "mg/dL",
            "reference_range": "70-99",
            "flag": None,
        },
        {
            "test_name": "ALT",
            "result": "61",
            "unit": "U/L",
            "reference_range": "0-55",
            "flag": "H",
        },
    ]
    stub_structured_extraction(noisy_rows)
    client = TestClient(app)

    response = client.post(
        "/api/v1/parse",
        json={"text": "dummy"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()

    parsed_names = [row["test_name"] for row in payload["rows"]]
    assert "Glucose" in parsed_names
    assert "ALT" in parsed_names
    assert "Patient Name" not in parsed_names
    assert "DOB" not in parsed_names
    assert "Medicare Number" not in parsed_names
    assert "Unit" not in parsed_names


def test_parse_maps_h_l_flags_to_high_low(stub_structured_extraction):
    stub_structured_extraction(
        [
            {
                "test_name": "LDL Cholesterol",
                "result": "210",
                "unit": "mg/dL",
                "reference_range": "0-200",
                "flag": "H",
            },
            {
                "test_name": "Ferritin",
                "result": "10",
                "unit": "ng/mL",
                "reference_range": "13-150",
                "flag": "L",
            },
        ]
    )
    client = TestClient(app)

    response = client.post(
        "/api/v1/parse",
        json={"text": "dummy"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    by_name = {row["test_name"]: row for row in payload["rows"]}
    assert by_name["LDL Cholesterol"]["flag"] == "high"
    assert by_name["Ferritin"]["flag"] == "low"


def test_parse_returns_clear_error_when_model_json_is_malformed(monkeypatch):
    async def broken_extract(_: str):
        raise ParseExtractionError(
            "Malformed JSON from extraction model. Please retry parsing this report.",
            status_code=502,
        )

    monkeypatch.setattr(parse_router, "extract_lab_rows_with_openai", broken_extract)
    client = TestClient(app)

    response = client.post(
        "/api/v1/parse",
        json={"text": "dummy"},
    )

    assert response.status_code == 502
    assert response.json()["detail"].startswith("Malformed JSON from extraction model")


def test_fallback_extraction_filters_metadata_and_unit_rows(monkeypatch):
    monkeypatch.setattr(
        parse_llm,
        "_run_openai_extraction",
        lambda _: (_ for _ in ()).throw(RuntimeError("missing_api_key")),
    )

    text = """
    Amy Santiago 12 June
    MPS-FBC 240612 0-1.0
    Requesting Dr: 34 years
    Provider No 2134567 A
    Medicare No 2456
    Hemoglobin 109 g/L 80.0-160
    g/L 160
    Red Cell Count (RBC) 98
    x10^ 12 80.0-5.1
    Haematocrit (HCT) 34
    L/L 36.0-0.46
    """.strip()

    rows = asyncio.run(parse_llm.extract_lab_rows_with_openai(text))
    names = [row.test_name for row in rows]

    assert "Hemoglobin" in names
    assert "Amy Santiago" not in names
    assert "Requesting Dr" not in names
    assert "Provider No" not in names
    assert "Medicare No" not in names
    assert "g/L" not in names
    assert "x10^" not in names
    assert "L/L" not in names
