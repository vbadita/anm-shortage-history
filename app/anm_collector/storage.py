"""Content validation, immutable raw storage and the manifest catalog."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import os
import tempfile
import zipfile
from dataclasses import asdict, dataclass, fields
from datetime import UTC, date, datetime
from pathlib import Path


@dataclass(frozen=True)
class FileType:
    ext: str
    mime: str


PDF = FileType("pdf", "application/pdf")
XLSX = FileType("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
XLS = FileType("xls", "application/vnd.ms-excel")

_OLE_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")


def detect_type(data: bytes) -> FileType | None:
    """Identify a file by its first bytes, never by URL or Content-Type.
    Anything else (notably an HTML error page served as .pdf) is None."""
    if data.startswith(b"%PDF-"):
        return PDF
    if data.startswith(b"PK\x03\x04"):
        # Every .docx/.jar/.zip is also PK..; an .xlsx has xl/workbook.xml.
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                if "xl/workbook.xml" in zf.namelist():
                    return XLSX
        except zipfile.BadZipFile:
            return None
        return None
    if data.startswith(_OLE_MAGIC):
        return XLS
    return None


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def wayback_digest(data: bytes) -> str:
    """The Wayback CDX 'digest' field: base32(SHA-1(body))."""
    return base64.b32encode(hashlib.sha1(data).digest()).decode()


class RawStore:
    """data/raw/<YYYY-MM-DD>_<sha256[:12]>.<ext>; files are write-once."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def path_for(self, file_date: date | None, sha256: str, ext: str) -> Path:
        day = file_date.isoformat() if file_date else "undated"
        return self.root / f"{day}_{sha256[:12]}.{ext}"

    def save(self, data: bytes, path: Path) -> bool:
        """Write atomically without ever replacing an existing file.
        Returns False if the path already existed."""
        self.root.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".tmp-")
        try:
            os.fchmod(fd, 0o644)  # mkstemp creates 0600; raw files are public data
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.link(tmp, path)  # atomic; fails with FileExistsError if taken
            return True
        except FileExistsError:
            return False
        finally:
            os.unlink(tmp)


@dataclass
class ManifestRow:
    file_date: str
    original_url: str
    source: str  # where the bytes came from: live | wayback
    wayback_timestamp: str
    sha256: str
    size_bytes: int
    content_type: str
    downloaded_at: str
    status: str  # new | duplicate | invalid | failed
    wayback_digest: str
    path: str

    @property
    def ok(self) -> bool:
        return self.status in ("new", "duplicate")


MANIFEST_FIELDS = [f.name for f in fields(ManifestRow)]


class Manifest:
    """Append-only CSV. Each row is flushed immediately, so a crash mid-run
    loses at most the row being written."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.rows: list[ManifestRow] = []
        if path.exists():
            with path.open(newline="", encoding="utf-8") as fh:
                for rec in csv.DictReader(fh):
                    rec["size_bytes"] = int(rec["size_bytes"] or 0)
                    self.rows.append(ManifestRow(**rec))

    def append(self, row: ManifestRow) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not self.path.exists()
        with self.path.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=MANIFEST_FIELDS, lineterminator="\n")
            if new_file:
                writer.writeheader()
            writer.writerow(asdict(row))
            fh.flush()
            os.fsync(fh.fileno())
        self.rows.append(row)

    def ok_rows(self) -> list[ManifestRow]:
        return [r for r in self.rows if r.ok]

    def has_ok_url(self, url: str, source: str | None = None) -> bool:
        return any(
            r.ok and r.original_url == url and (source is None or r.source == source)
            for r in self.rows
        )

    def known_digests(self) -> set[str]:
        return {r.wayback_digest for r in self.rows if r.ok}

    def stored_sha256(self) -> dict[str, str]:
        return {r.sha256: r.path for r in self.rows if r.status == "new"}


class Catalog:
    """Raw store + manifest: the single place deciding new/duplicate/invalid."""

    def __init__(self, store: RawStore, manifest: Manifest) -> None:
        self.store = store
        self.manifest = manifest

    def ingest(
        self,
        data: bytes,
        *,
        url: str,
        file_date: date | None,
        source: str,
        wayback_timestamp: str = "",
    ) -> ManifestRow:
        ftype = detect_type(data)
        sha = sha256_hex(data)
        row = ManifestRow(
            file_date=file_date.isoformat() if file_date else "",
            original_url=url,
            source=source,
            wayback_timestamp=wayback_timestamp,
            sha256=sha,
            size_bytes=len(data),
            content_type=ftype.mime if ftype else "",
            downloaded_at=datetime.now(UTC).isoformat(timespec="seconds"),
            status="invalid",
            wayback_digest=wayback_digest(data),
            path="",
        )
        if ftype is None:
            self.manifest.append(row)
            return row
        existing = self.manifest.stored_sha256().get(sha)
        if existing:
            row.status, row.path = "duplicate", existing
        else:
            path = self.store.path_for(file_date, sha, ftype.ext)
            self.store.save(data, path)
            row.status, row.path = "new", path.relative_to(self.store.root.parent).as_posix()
        self.manifest.append(row)
        return row

    def record_failure(
        self, *, url: str, file_date: date | None, source: str, wayback_timestamp: str = ""
    ) -> ManifestRow:
        row = ManifestRow(
            file_date=file_date.isoformat() if file_date else "",
            original_url=url,
            source=source,
            wayback_timestamp=wayback_timestamp,
            sha256="",
            size_bytes=0,
            content_type="",
            downloaded_at=datetime.now(UTC).isoformat(timespec="seconds"),
            status="failed",
            wayback_digest="",
            path="",
        )
        self.manifest.append(row)
        return row
