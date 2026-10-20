"""Runtime configuration, read from environment variables only."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PAGE_URL = (
    "https://www.anm.ro/medicamente-de-uz-uman/autorizare-medicamente/"
    "notificari-discontinuitate-medicamente/"
)
# CDX prefix for the folder holding every version of the file.
FILES_PREFIX = "anm.ro/_/DISCONTINUITATE MEDICAMENTE/"
FILES_BASE = "https://www.anm.ro/_/DISCONTINUITATE MEDICAMENTE/"

# File names verified by hand before this tool existed (source="known").
KNOWN_FILES = (
    "14.10.2024 Notificari Sunset pt.postat incepand cu luna IUNIE 2016_.pdf",
    "30.07.2025_Notificari discontinuitate de postat pe site .pdf",
    "03.07.2026 Notificari Sunset pt.postat incepand cu luna IUNIE 2016_.pdf",
    "09.10.2026 Notificari Sunset pt.postat incepand cu luna IUNIE 2016_.pdf",
)

DEFAULT_USER_AGENT = "anm-shortage-history/0.1 (+https://github.com/vbadita/anm-shortage-history)"


@dataclass(frozen=True)
class Config:
    data_dir: Path
    min_interval_seconds: float
    timeout_seconds: float
    max_retries: int
    user_agent: str
    pushgateway_url: str | None
    storage_backend: str

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def manifest_path(self) -> Path:
        return self.data_dir / "manifest.csv"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        env = dict(os.environ if env is None else env)
        cfg = cls(
            data_dir=Path(env.get("ANM_DATA_DIR", "data")),
            min_interval_seconds=float(env.get("ANM_MIN_INTERVAL_SECONDS", "2.0")),
            timeout_seconds=float(env.get("ANM_HTTP_TIMEOUT_SECONDS", "60")),
            max_retries=int(env.get("ANM_MAX_RETRIES", "4")),
            user_agent=env.get("ANM_USER_AGENT", DEFAULT_USER_AGENT),
            pushgateway_url=env.get("ANM_PUSHGATEWAY_URL") or None,
            storage_backend=env.get("ANM_STORAGE_BACKEND", "local"),
        )
        if cfg.min_interval_seconds < 2.0:
            raise ValueError("ANM_MIN_INTERVAL_SECONDS must be >= 2.0 (politeness rule)")
        if cfg.storage_backend != "local":
            raise ValueError(f"unsupported ANM_STORAGE_BACKEND={cfg.storage_backend!r}")
        return cfg
