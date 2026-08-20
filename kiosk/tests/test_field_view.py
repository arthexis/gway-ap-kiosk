"""Tests for the kiosk's fail-closed live charger field view."""

import importlib.machinery
import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "bin" / "gway-kiosk-status"
loader = importlib.machinery.SourceFileLoader("gway_kiosk_status", str(MODULE_PATH))
spec = importlib.util.spec_from_loader(loader.name, loader)
assert spec is not None
kiosk_status = importlib.util.module_from_spec(spec)
loader.exec_module(kiosk_status)


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return b'{"connected": true, "charger_count": 1}'


def test_field_view_stays_hidden_until_explicitly_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("KIOSK_FIELD_VIEW_ENABLED", "0")
    monkeypatch.setenv("KIOSK_FIELD_VIEW_STATE_FILE", str(tmp_path / "armed"))

    context = kiosk_status.field_view_context()

    assert context["available"] is False
    assert context["reason"] == "disabled"


def test_field_view_requires_carrier_before_it_probes_ocpp(monkeypatch, tmp_path):
    state_file = tmp_path / "armed"
    state_file.touch()
    monkeypatch.setenv("KIOSK_FIELD_VIEW_ENABLED", "1")
    monkeypatch.setenv("KIOSK_FIELD_VIEW_STATE_FILE", str(state_file))
    monkeypatch.setattr(kiosk_status, "interface_has_carrier", lambda _interface: False)
    monkeypatch.setattr(
        kiosk_status,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected probe")),
    )

    context = kiosk_status.field_view_context()

    assert context["available"] is False
    assert context["reason"] == "no_interface_carrier"


def test_field_view_requires_live_ocpp_connection(monkeypatch, tmp_path):
    state_file = tmp_path / "armed"
    state_file.touch()
    monkeypatch.setenv("KIOSK_FIELD_VIEW_ENABLED", "1")
    monkeypatch.setenv("KIOSK_FIELD_VIEW_STATE_FILE", str(state_file))
    monkeypatch.setenv("KIOSK_FIELD_VIEW_CHARGER_IDS", "FIELD-CP-1")
    monkeypatch.setattr(kiosk_status, "interface_has_carrier", lambda _interface: True)
    monkeypatch.setattr(kiosk_status, "urlopen", lambda *_args, **_kwargs: FakeResponse())

    context = kiosk_status.field_view_context()

    assert context["available"] is True
    assert context["reason"] == "live_ocpp_connection"
    assert context["charger_count"] == 1


def test_field_view_html_contains_the_fail_closed_overlay(monkeypatch, tmp_path):
    monkeypatch.setenv("KIOSK_FIELD_VIEW_ENABLED", "0")
    monkeypatch.setenv("KIOSK_FIELD_VIEW_STATE_FILE", str(tmp_path / "armed"))

    html = kiosk_status.render_html(kiosk_status.build_payload())

    assert "data-kiosk-field-view" in html
    assert "data-field-view-url" in html
