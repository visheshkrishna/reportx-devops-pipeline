from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.services.ocr import extract_text_from_image_bytes, extract_text_from_pdf_bytes
from app.services.parser import parse_text


class UploadLike(Protocol):
    filename: str | None
    content_type: str | None

    async def read(self) -> bytes: ...


class ParseServiceError(Exception):
    def __init__(self, detail: str, status_code: int) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


@dataclass(frozen=True)
class ParseConfig:
    max_file_bytes: int = 500 * 1024 * 1024
    max_pdf_pages: int = 5
    max_files: int = 5


def collect_uploads(
    file: UploadLike | None,
    files: list[UploadLike] | None,
) -> list[UploadLike]:
    uploads: list[UploadLike] = []
    if files:
        uploads.extend(upload for upload in files if upload is not None)
    if file is not None:
        uploads.append(file)
    return uploads


def extract_text_from_json_payload(content_type: str, payload: Any) -> str:
    if "application/json" not in (content_type or "").lower():
        raise ParseServiceError('Send a PDF file or JSON {"text": "..."}."', 400)
    if not isinstance(payload, dict) or "text" not in payload:
        raise ParseServiceError("Body must include 'text'.", 400)
    return str(payload.get("text") or "")


def _is_pdf_upload(upload: UploadLike) -> bool:
    return "pdf" in (upload.content_type or "application/octet-stream").lower()


def _is_supported_image_upload(upload: UploadLike) -> bool:
    content_type = (upload.content_type or "application/octet-stream").lower()
    if content_type.startswith("image/") and any(
        token in content_type for token in ("png", "jpeg", "jpg")
    ):
        return True
    filename = (upload.filename or "").lower()
    return filename.endswith((".png", ".jpg", ".jpeg"))


async def extract_text_from_uploads(
    uploads: list[UploadLike],
    *,
    content_length: int | None,
    config: ParseConfig | None = None,
) -> str:
    active_config = config or ParseConfig()
    if len(uploads) > active_config.max_files:
        raise ParseServiceError(f"Too many files (max {active_config.max_files}).", 413)

    max_total = active_config.max_file_bytes * active_config.max_files
    if content_length and content_length > max_total:
        raise ParseServiceError(
            (
                f"Payload too large (max {active_config.max_files} files, "
                f"{active_config.max_file_bytes // (1024 * 1024)}MB each)."
            ),
            413,
        )

    extracted_parts: list[str] = []
    for upload in uploads:
        is_pdf = _is_pdf_upload(upload)
        is_image = _is_supported_image_upload(upload)
        if not (is_pdf or is_image):
            raise ParseServiceError(
                (
                    f"Unsupported file type for {upload.filename or 'upload'}. "
                    "Use PDF or image (PNG/JPEG)."
                ),
                400,
            )

        data = await upload.read()
        if len(data) > active_config.max_file_bytes:
            raise ParseServiceError(
                (
                    f"{upload.filename or 'file'} exceeds "
                    f"{active_config.max_file_bytes // (1024 * 1024)}MB limit."
                ),
                413,
            )

        try:
            if is_pdf:
                text = extract_text_from_pdf_bytes(
                    data, max_pages=active_config.max_pdf_pages, ocr_lang="eng"
                )
            else:
                text = extract_text_from_image_bytes(data, lang="eng")
        except Exception as exc:
            raise ParseServiceError(
                f"Failed to read file {upload.filename or ''}: {exc}",
                400,
            ) from exc

        if normalized := (text or "").strip():
            extracted_parts.append(normalized)

    return "\n".join(extracted_parts)


def build_parse_response(text_content: str) -> dict[str, Any]:
    rows, unparsed = parse_text(text_content or "")
    payload_rows = []
    for index, row in enumerate(rows, start=1):
        payload_rows.append(
            {
                "id": f"r{index}",
                "test_name": row.test_name,
                "test_name_raw": row.test_name_raw,
                "display_name": row.test_name,
                "value": row.value,
                "value_text": row.value_text or (str(row.value) if row.value is not None else None),
                "value_num": row.value_num,
                "unit": row.unit,
                "unit_raw": row.unit_raw,
                "reference_range": row.reference_range,
                "comparator": row.comparator,
                "flag": row.flag,
                "confidence": row.confidence,
                "page": row.page,
                "bbox": row.bbox,
                "raw_line": row.raw_line,
            }
        )

    return {
        "rows": payload_rows,
        "unparsed_lines": unparsed,
        "unparsed": [{"page": None, "text": item} for item in unparsed],
        "meta": {},
        "extracted_text": text_content or "",
    }
