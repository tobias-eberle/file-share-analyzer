"""MIME-type → RAG-relevance category mapping.

Categories:
  text-extractable — readily ingestible into a RAG pipeline
  ocr-needed       — image content that must pass through OCR first
  media            — audio/video that needs transcription, low priority
  archive          — zip/tar/7z; needs unpacking before analysis
  executable       — binaries; never RAG candidates
  other            — unknown/unclassified
"""
from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Optional

try:
    import magic as _magic
except Exception:  # pragma: no cover — environment without libmagic
    _magic = None


_TEXT_EXTRACTABLE = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.oasis.opendocument.text",
    "application/vnd.oasis.opendocument.spreadsheet",
    "application/vnd.oasis.opendocument.presentation",
    "application/rtf",
    "application/json",
    "application/xml",
    "application/xhtml+xml",
    "application/x-tex",
    "application/x-yaml",
    "application/yaml",
    "application/csv",
    "application/sql",
    "message/rfc822",
}

_TEXT_EXTRACTABLE_PREFIXES = ("text/",)

_OCR_NEEDED = {
    "image/jpeg", "image/png", "image/tiff", "image/bmp", "image/gif",
    "image/webp", "image/heic", "image/heif",
}

_MEDIA_PREFIXES = ("audio/", "video/")

_ARCHIVE_TYPES = {
    "application/zip", "application/x-zip-compressed",
    "application/x-tar", "application/gzip", "application/x-gzip",
    "application/x-bzip2", "application/x-7z-compressed",
    "application/x-rar-compressed", "application/vnd.rar",
    "application/x-xz", "application/x-compress",
}

_EXECUTABLE_TYPES = {
    "application/x-msdownload", "application/x-msi",
    "application/x-executable", "application/x-mach-binary",
    "application/x-sharedlib", "application/x-dosexec",
    "application/vnd.microsoft.portable-executable",
}

_EXTENSION_FALLBACK = {
    ".md": "text/markdown",
    ".rst": "text/x-rst",
    ".log": "text/plain",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".toml": "application/toml",
    ".ini": "text/plain",
    ".cfg": "text/plain",
    ".conf": "text/plain",
    ".eml": "message/rfc822",
    ".msg": "application/vnd.ms-outlook",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".pdf": "application/pdf",
    ".7z": "application/x-7z-compressed",
    ".rar": "application/vnd.rar",
    ".tar": "application/x-tar",
    ".gz": "application/gzip",
    ".zip": "application/zip",
    ".exe": "application/vnd.microsoft.portable-executable",
    ".dll": "application/vnd.microsoft.portable-executable",
    ".so":  "application/x-sharedlib",
}


def categorize(mime_type: Optional[str]) -> str:
    if not mime_type:
        return "other"
    mime_type = mime_type.lower().split(";", 1)[0].strip()
    if mime_type in _TEXT_EXTRACTABLE or any(mime_type.startswith(p) for p in _TEXT_EXTRACTABLE_PREFIXES):
        return "text-extractable"
    if mime_type in _OCR_NEEDED or mime_type.startswith("image/"):
        return "ocr-needed"
    if any(mime_type.startswith(p) for p in _MEDIA_PREFIXES):
        return "media"
    if mime_type in _ARCHIVE_TYPES:
        return "archive"
    if mime_type in _EXECUTABLE_TYPES:
        return "executable"
    return "other"


class MimeDetector:
    """Detect MIME via libmagic content sniffing with extension fallback."""

    def __init__(self) -> None:
        self._magic = None
        if _magic is not None:
            try:
                self._magic = _magic.Magic(mime=True)
            except Exception:
                self._magic = None

    def detect(self, path: str | Path, *, head: Optional[bytes] = None) -> str:
        p = Path(path)
        if self._magic is not None:
            try:
                if head is not None:
                    return self._magic.from_buffer(head)
                return self._magic.from_file(str(p))
            except Exception:
                pass
        ext = p.suffix.lower()
        if ext in _EXTENSION_FALLBACK:
            return _EXTENSION_FALLBACK[ext]
        guess, _ = mimetypes.guess_type(str(p))
        return guess or "application/octet-stream"


def seed_mime_categories(conn) -> None:
    """Populate the mime_categories lookup table with the well-known buckets."""
    rows = []
    for mt in _TEXT_EXTRACTABLE:
        rows.append((mt, "text-extractable"))
    for mt in _OCR_NEEDED:
        rows.append((mt, "ocr-needed"))
    for mt in _ARCHIVE_TYPES:
        rows.append((mt, "archive"))
    for mt in _EXECUTABLE_TYPES:
        rows.append((mt, "executable"))
    conn.executemany(
        "INSERT OR REPLACE INTO mime_categories(mime_type, category) VALUES (?, ?)",
        rows,
    )
