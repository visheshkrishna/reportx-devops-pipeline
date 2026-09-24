from app.services import parse_llm
from app.services.parse_llm import ParseExtractionError, _load_rows_from_json


def test_load_rows_from_json_repairs_wrapped_array_payload():
    raw = (
        "Here are rows:\n"
        "[\n"
        '  {"test_name":"Glucose","result":"92","unit":"mg/dL","reference_range":"70-99","flag":null}\n'
        "]\n"
        "Thanks"
    )
    rows = _load_rows_from_json(raw)

    assert len(rows) == 1
    assert rows[0].test_name == "Glucose"
    assert rows[0].result == "92"


def test_load_rows_from_json_raises_for_truly_malformed_payload():
    raw = "{this is not valid json"

    try:
        _load_rows_from_json(raw)
        assert False, "Expected ParseExtractionError"
    except ParseExtractionError as exc:
        assert "Malformed JSON" in exc.detail


def test_run_openai_extraction_requests_strict_json_schema(monkeypatch):
    captured: dict[str, object] = {}

    class FakeResponses:
        def create(self, **kwargs):
            captured.update(kwargs)

            class FakeResponse:
                output_text = '{"rows":[{"test_name":"Glucose","result":"92","unit":"mg/dL","reference_range":"70-99","flag":null}]}'

            return FakeResponse()

    class FakeClient:
        responses = FakeResponses()

    monkeypatch.setattr(parse_llm, "_get_openai_client", lambda: FakeClient())
    monkeypatch.setattr(parse_llm, "_resolve_model", lambda default: "fake-model")
    monkeypatch.setattr(parse_llm, "_max_tokens", lambda: 123)

    raw = parse_llm._run_openai_extraction("report text")

    assert raw.startswith("{")
    assert captured["text"]["format"]["type"] == "json_schema"
    assert captured["text"]["format"]["strict"] is True
    assert captured["temperature"] == 0


def test_run_openai_extraction_omits_temperature_for_gpt5(monkeypatch):
    captured: dict[str, object] = {}

    class FakeResponses:
        def create(self, **kwargs):
            captured.update(kwargs)

            class FakeResponse:
                output_text = '{"rows":[{"test_name":"Glucose","result":"92","unit":"mg/dL","reference_range":"70-99","flag":null}]}'

            return FakeResponse()

    class FakeClient:
        responses = FakeResponses()

    monkeypatch.setattr(parse_llm, "_get_openai_client", lambda: FakeClient())
    monkeypatch.setattr(parse_llm, "_resolve_model", lambda default: "gpt-5")

    parse_llm._run_openai_extraction("report text")

    assert "temperature" not in captured
