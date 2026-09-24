from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.services import llm as llm_service

router = APIRouter()


SUPPORTED_LANGUAGES: dict[str, str] = {
    "en": "English",
    "es": "Spanish",
    "ar": "Arabic",
    "zh": "Mandarin Chinese",
    "hi": "Hindi",
    "fr": "French",
}


class TranslateRequest(BaseModel):
    text: str
    target_language: str
    prefetch_all: bool = False


@router.post("/translate")
async def translate_endpoint(payload: TranslateRequest) -> Any:
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text must be a non-empty string")

    code = (payload.target_language or "").strip().lower()
    if code not in SUPPORTED_LANGUAGES or code == "en":
        # We allow 'en' in the selector client-side but it produces no request.
        # If called directly with 'en', treat it as unsupported to avoid no-op calls.
        raise HTTPException(status_code=400, detail="unsupported target_language")

    label = SUPPORTED_LANGUAGES[code]
    if payload.prefetch_all:

        async def _translate(code_item: str, label_item: str):
            translation_item, meta_item = await llm_service.translate_summary(
                text,
                target_language=code_item,
                language_label=label_item,
            )
            return code_item, translation_item, meta_item

        translate_targets = [
            (lang_code, lang_label)
            for lang_code, lang_label in SUPPORTED_LANGUAGES.items()
            if lang_code != "en"
        ]
        results = await asyncio.gather(
            *[_translate(lang_code, lang_label) for lang_code, lang_label in translate_targets],
            return_exceptions=False,
        )

        translations: dict[str, str] = {}
        per_language_meta: dict[str, Any] = {}
        requested_translation: str | None = None
        requested_meta: dict[str, Any] = {}
        for lang_code, translated_text, translated_meta in results:
            per_language_meta[lang_code] = {
                key: translated_meta.get(key)
                for key in [
                    "duration_ms",
                    "llm",
                    "attempts",
                    "ok",
                    "model",
                    "endpoint",
                    "status",
                    "usage",
                    "finish_reason",
                    "error",
                    "language",
                ]
                if key in translated_meta
            }
            if translated_text:
                translations[lang_code] = translated_text
            if lang_code == code:
                requested_translation = translated_text
                requested_meta = translated_meta

        if requested_translation is None:
            err = (requested_meta or {}).get("error", {}) or {}
            error_code = str(err.get("code") or "")
            safe_requested_meta = {
                key: requested_meta[key]
                for key in [
                    "duration_ms",
                    "llm",
                    "attempts",
                    "ok",
                    "model",
                    "endpoint",
                    "status",
                    "usage",
                    "finish_reason",
                    "error",
                    "language",
                ]
                if key in requested_meta
            }
            if error_code in {"missing_api_key", "missing_openai_dependency"}:
                return JSONResponse(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    content={
                        "detail": "Translation service unavailable",
                        "meta": safe_requested_meta,
                    },
                )
            return JSONResponse(
                status_code=status.HTTP_502_BAD_GATEWAY,
                content={"detail": "Translation failed", "meta": safe_requested_meta},
            )

        return {
            "language": code,
            "translation": requested_translation,
            "translations": translations,
            "meta": {
                "requested": {
                    key: requested_meta[key]
                    for key in [
                        "duration_ms",
                        "llm",
                        "attempts",
                        "ok",
                        "model",
                        "endpoint",
                        "status",
                        "usage",
                        "finish_reason",
                        "error",
                        "language",
                    ]
                    if key in requested_meta
                },
                "prefetch": per_language_meta,
            },
        }

    translation, meta = await llm_service.translate_summary(
        text,
        target_language=code,
        language_label=label,
    )

    # Shared public meta keys from interpret + language
    public_meta_keys = [
        "duration_ms",
        "llm",
        "attempts",
        "ok",
        "model",
        "endpoint",
        "status",
        "usage",
        "finish_reason",
        "error",
        "language",
    ]
    safe_meta = {k: meta[k] for k in public_meta_keys if k in meta}

    if translation is None:
        err = (meta or {}).get("error", {}) or {}
        code = str(err.get("code") or "")
        if code in {"missing_api_key", "missing_openai_dependency"}:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"detail": "Translation service unavailable", "meta": safe_meta},
            )
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"detail": "Translation failed", "meta": safe_meta},
        )

    return {
        "language": code,
        "translation": translation,
        "meta": safe_meta,
    }
