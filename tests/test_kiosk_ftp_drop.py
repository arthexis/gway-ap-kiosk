"""Regression tests for the kiosk's read-only FTP-drop panel."""
import importlib.machinery
import importlib.util
import os
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "kiosk/bin/gway-kiosk-status"
loader = importlib.machinery.SourceFileLoader("kiosk_status_ftp_test", str(SOURCE))
spec = importlib.util.spec_from_loader(loader.name, loader)
kiosk = importlib.util.module_from_spec(spec)
loader.exec_module(kiosk)


def test_ftp_drop_lists_regular_files_only_newest_first(tmp_path, monkeypatch):
    newest = tmp_path / "Café.csv"
    oldest = tmp_path / "old.txt"
    newest.write_text("new", encoding="utf-8")
    oldest.write_text("old", encoding="utf-8")
    os.utime(oldest, (1, 1))
    (tmp_path / "folder").mkdir()
    (tmp_path / "linked.txt").symlink_to(oldest)
    monkeypatch.setattr(kiosk, "FTP_DROP_DIRECTORY", tmp_path)

    context = kiosk.ftp_drop_context()

    assert context["available"] is True
    assert [entry["name"] for entry in context["files"]] == ["Café.csv", "old.txt"]
    assert context["files"][0]["size"] == "3 B"
    assert context["message"] == "Select a file for details"


def test_ftp_drop_unavailable_and_html_escape(tmp_path, monkeypatch):
    monkeypatch.setattr(kiosk, "FTP_DROP_DIRECTORY", tmp_path / "missing")
    assert kiosk.ftp_drop_context() == {
        "available": False, "files": [], "message": "FTP drop unavailable"
    }

    payload = kiosk.build_payload()
    payload["ftp"] = {
        "available": True,
        "message": "Select a file for details",
        "files": [{"name": '<unsafe>.csv', "size": "1 B", "modified": "2026-10-23T00:00Z"}],
    }
    page = kiosk.render_html(payload)
    assert "&lt;unsafe&gt;.csv" in page
    assert '<unsafe>.csv' not in page
    assert 'id="ftp-refresh"' in page
