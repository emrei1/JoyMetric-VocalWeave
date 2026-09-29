from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path


class CoverCache:
    def __init__(self, app_root: Path):
        local = os.environ.get("LOCALAPPDATA")
        self.root = (Path(local) / "JoyMetric" / "userdata") if local else (Path.home() / ".joymetric" / "userdata")
        self.cache_dir = self.root / "cover_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def normalize_source(source: str) -> str:
        text = str(source or "").strip()
        return text[:4096]

    def key_for_source(self, source: str) -> str:
        text = self.normalize_source(source)
        if not text:
            raise ValueError("Cover source is required")
        return hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()

    def path_for_key(self, key: str) -> Path:
        safe = "".join(ch for ch in str(key or "") if ch.lower() in "0123456789abcdef")[:40]
        if len(safe) < 8:
            raise ValueError("Invalid cover cache key")
        return self.cache_dir / f"{safe}.png"

    def get_path(self, *, key: str | None = None, source: str | None = None) -> Path | None:
        if not key:
            if not source:
                return None
            key = self.key_for_source(source)
        path = self.path_for_key(key)
        return path if path.exists() and path.is_file() else None

    def save_data_url(self, source: str, data_url: str) -> tuple[str, Path]:
        key = self.key_for_source(source)
        path = self.path_for_key(key)
        text = str(data_url or "")
        prefix = "data:image/png;base64,"
        if not text.startswith(prefix):
            raise ValueError("Only PNG data URLs are supported")
        payload = text[len(prefix):]
        try:
            raw = base64.b64decode(payload, validate=True)
        except Exception as exc:
            raise ValueError("Cover image data is invalid") from exc
        if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Cover image is not a valid PNG")
        if len(raw) > 10 * 1024 * 1024:
            raise ValueError("Cover image is unexpectedly large")
        path.write_bytes(raw)
        return key, path
