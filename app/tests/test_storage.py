import io
import zipfile
from datetime import date

import pytest

from anm_collector.storage import (
    PDF,
    XLS,
    XLSX,
    Catalog,
    Manifest,
    RawStore,
    detect_type,
    wayback_digest,
)

PDF_BYTES = b"%PDF-1.7\n%fake but valid header\n"


def make_zip(names):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n in names:
            zf.writestr(n, "x")
    return buf.getvalue()


# ---- magic bytes ---------------------------------------------------------
def test_detect_pdf():
    assert detect_type(PDF_BYTES) == PDF


def test_detect_xlsx():
    assert detect_type(make_zip(["[Content_Types].xml", "xl/workbook.xml"])) == XLSX


def test_detect_xls():
    assert detect_type(bytes.fromhex("D0CF11E0A1B11AE1") + b"\0" * 100) == XLS


@pytest.mark.parametrize(
    "data",
    [
        b"<!DOCTYPE html><html><body>404 Not Found</body></html>",  # error page saved as .pdf
        b"\xef\xbb\xbf<html>",  # BOM + html
        make_zip(["word/document.xml"]),  # a .docx is a zip but not a spreadsheet
        b"PK\x03\x04garbage",  # truncated zip
        b"",
    ],
)
def test_detect_rejects(data):
    assert detect_type(data) is None


def test_wayback_digest_format():
    # base32(sha1(b"")) — the value Wayback CDX reports for an empty body
    assert wayback_digest(b"") == "3I42H3S6NNFQ2MSVX7XZKYAYSCX5QBYJ"


# ---- dedupe and immutability ---------------------------------------------
@pytest.fixture
def catalog(tmp_path):
    return Catalog(RawStore(tmp_path / "raw"), Manifest(tmp_path / "manifest.csv"))


def test_same_bytes_twice_is_duplicate(catalog, tmp_path):
    a = catalog.ingest(PDF_BYTES, url="https://x/a.pdf", file_date=date(2024, 1, 1), source="live")
    b = catalog.ingest(
        PDF_BYTES, url="https://x/b.pdf", file_date=date(2024, 2, 2), source="wayback"
    )
    assert (a.status, b.status) == ("new", "duplicate")
    assert b.path == a.path  # points at the first stored copy
    assert len(list((tmp_path / "raw").iterdir())) == 1


def test_different_bytes_are_new(catalog, tmp_path):
    catalog.ingest(PDF_BYTES, url="u1", file_date=date(2024, 1, 1), source="live")
    catalog.ingest(PDF_BYTES + b"v2", url="u2", file_date=date(2024, 1, 1), source="live")
    assert len(list((tmp_path / "raw").iterdir())) == 2


def test_invalid_content_not_stored(catalog, tmp_path):
    row = catalog.ingest(b"<html>oops</html>", url="u", file_date=None, source="live")
    assert row.status == "invalid"
    assert not (tmp_path / "raw").exists() or not any((tmp_path / "raw").iterdir())


def test_dedupe_survives_restart(tmp_path):
    first = Catalog(RawStore(tmp_path / "raw"), Manifest(tmp_path / "manifest.csv"))
    first.ingest(PDF_BYTES, url="u", file_date=date(2024, 1, 1), source="live")
    # new process: manifest is re-read from disk
    second = Catalog(RawStore(tmp_path / "raw"), Manifest(tmp_path / "manifest.csv"))
    row = second.ingest(PDF_BYTES, url="u", file_date=date(2024, 1, 1), source="live")
    assert row.status == "duplicate"
    assert [r.status for r in Manifest(tmp_path / "manifest.csv").rows] == ["new", "duplicate"]


def test_raw_store_never_overwrites(tmp_path):
    store = RawStore(tmp_path)
    path = store.path_for(date(2024, 1, 1), "ab" * 32, "pdf")
    assert path.name == "2024-01-01_abababababab.pdf"
    assert store.save(b"original", path) is True
    assert store.save(b"something else", path) is False
    assert path.read_bytes() == b"original"
    assert path.stat().st_mode & 0o777 == 0o644  # readable by other UIDs (parser, backup)
    assert [p.name for p in tmp_path.iterdir()] == [path.name]  # no temp files left


# ---- run lock --------------------------------------------------------------
def test_second_run_lock_is_refused_and_released(tmp_path):
    from anm_collector.storage import LockHeld, RunLock

    lock_path = tmp_path / ".lock"
    # flock is per open file: a second open of the same path conflicts
    with RunLock(lock_path), pytest.raises(LockHeld), RunLock(lock_path):
        pass
    with RunLock(lock_path):  # released on exit
        pass
