from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from app.db.models import FindingFlag, ReportFinding
from app.services.llm import (
    _call_openai_chat,
    _call_openai_responses,
    _resolve_model,
    _timeout_seconds,
)

logger = logging.getLogger("reportrx.backend")


def _fallback_questions(flagged_findings: list[ReportFinding]) -> list[str]:
    """Fallback list of questions if LLM fails or is unavailable."""
    if not flagged_findings:
        return [
            "Are there any trends in my lab results I should be aware of?",
            "Do I need to make any changes to my diet, exercise, or medications?",
            "When should I have these or other tests done again?",
        ]

    # Extract names for flagged items
    names = [
        f.display_name
        for f in flagged_findings
        if f.flag in {FindingFlag.HIGH, FindingFlag.LOW, FindingFlag.ABNORMAL}
    ]
    if not names:
        names = [f.display_name for f in flagged_findings][:2]

    names_str = ", ".join(names[:2])
    if len(names) > 2:
        names_str += " and others"

    return [
        f"What might be causing my abnormal results for {names_str}?",
        f"Do my current symptoms fit with these findings for {names_str}?",
        "Are there immediate steps or new medications needed based on these flags?",
    ]


def _build_prompt(flagged_findings: list[ReportFinding]) -> str:
    simplified = [
        {
            "test_name": f.display_name,
            "value": f.value_text or str(f.value_numeric),
            "unit": f.unit,
            "flag": f.flag.value,
        }
        for f in flagged_findings
    ]

    instructions = (
        "Based on the following flagged lab results, generate exactly 3 patient-friendly questions "
        "the patient should ask their clinician. The questions should cover: "
        "1. Symptoms or causes relative to the abnormal findings, "
        "2. Level of concern or urgency, "
        "3. Recommended next steps or timeline for follow-up.\n"
        "Return the output as a valid JSON array of strings, with no additional text or Markdown formatting.\n\n"
        "Example Output:\n"
        '["What could be causing my elevated glucose?", '
        '"Should I be concerned about my low iron?", '
        '"When should I retest?"]\n\n'
    )
    return instructions + "RESULTS:\n" + json.dumps(simplified, ensure_ascii=False)


async def generate_questions(findings: list[ReportFinding]) -> tuple[list[str], dict[str, Any]]:
    start = time.perf_counter()
    meta: dict[str, Any] = {"llm": "none", "attempts": 0}
    meta["model"] = _resolve_model(os.getenv("OPENAI_MODEL", "gpt-5"))
    meta["endpoint"] = "unknown"

    flagged = [
        f for f in findings if f.flag in {FindingFlag.HIGH, FindingFlag.LOW, FindingFlag.ABNORMAL}
    ]

    # If no flagged findings, return generic fallback immediately to save LLM cost
    if not flagged:
        meta["ok"] = True
        meta["duration_ms"] = int((time.perf_counter() - start) * 1000)
        return _fallback_questions([]), meta

    prompt = _build_prompt(flagged[:10])  # limit to 10 flags to fit context

    try:
        use_responses = meta["model"].startswith("gpt-5") or os.getenv(
            "OPENAI_USE_RESPONSES", "0"
        ) in {
            "1",
            "true",
            "True",
        }
        meta["llm"] = "openai"
        meta["attempts"] = 1
        meta["endpoint"] = "responses" if use_responses else "chat.completions"

        raw: str
        call: dict[str, Any]
        if use_responses:
            try:
                raw, call = await _call_openai_responses(
                    prompt, timeout_s=_timeout_seconds("responses")
                )
            except Exception:
                meta["endpoint"] = "chat.completions"
                raw, call = await _call_openai_chat(prompt, timeout_s=_timeout_seconds("chat"))
        else:
            raw, call = await _call_openai_chat(prompt, timeout_s=_timeout_seconds("chat"))

        text_out = (raw or "").strip()

        # Try to parse the JSON array
        questions: list[str] = []
        try:
            # Clean up markdown code blocks if present
            if text_out.startswith("```json"):
                text_out = text_out[7:]
            if text_out.endswith("```"):
                text_out = text_out[:-3]
            text_out = text_out.strip()

            parsed = json.loads(text_out)
            if isinstance(parsed, list) and all(isinstance(i, str) for i in parsed):
                questions = parsed
        except Exception:
            pass

        if len(questions) < 2:
            # Fallback if parsing failed or didn't provide enough
            questions = _fallback_questions(flagged)

        meta["ok"] = True
        if call:
            if "usage" in call:
                meta["usage"] = call["usage"]
        logger.info({"event": "llm_generate_questions", "ok": True, "attempts": meta["attempts"]})

        meta["duration_ms"] = int((time.perf_counter() - start) * 1000)
        return questions[:3], meta

    except Exception as e:
        logger.exception("Failed to generate questions")
        meta["ok"] = False
        meta["error"] = str(e)
        meta["duration_ms"] = int((time.perf_counter() - start) * 1000)
        return _fallback_questions(flagged), meta
