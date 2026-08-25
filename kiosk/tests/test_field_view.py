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


def test_kiosk_html_uses_tabs_and_two_column_lcd_history():
    payload = kiosk_status.build_payload()
    payload["ap"]["qr_data_uri"] = "data:image/png;base64,test"
    html = kiosk_status.render_html(payload)
    lcd_html = kiosk_status.render_lcd_history(
        {
            "latest": [{"line1": "HIGH message", "line2": "LOW message"}],
            "standby": [{"line1": "STANDBY top", "line2": "STANDBY bottom"}],
        }
    )
    access_panel = html.split('class="kiosk-panel kiosk-panel--ap"', 1)[1].split("</article>", 1)[0]

    assert 'data-kiosk-tab="kiosk"' in html
    assert 'data-kiosk-tab="console"' in html
    assert 'data-kiosk-console-frame' in html
    assert "flex: 1 1 0;" in html
    assert "function refreshActiveTab(name)" in html
    assert "Host / Node" in html
    assert access_panel.index("kiosk-ap-copy") < access_panel.index("kiosk-ap-qr")
    assert "grid-template-columns: minmax(0, 1fr) var(--kiosk-qr-size);" in html
    assert "data-kiosk-ap-reset" in access_panel
    assert "--kiosk-qr-size: clamp(207px, 29.25vmin, 297px);" in html
    assert "Reset the AP? This rotates its password and reapplies the AP connection." in html
    assert "kiosk-ap-updated" in access_panel
    assert "<dt>Updated</dt>" not in access_panel
    assert '<div class="kiosk-lcd-history__meta">' not in lcd_html
    assert ">Latest<" in lcd_html
    assert ">Standby<" in lcd_html
    assert "HIGH message" in lcd_html
    assert "LOW message" in lcd_html
    assert "STANDBY top" in lcd_html
    assert "STANDBY bottom" in lcd_html
    assert ".kiosk-lcd-history__frame span {\n  display: block;" in html


def test_lcd_history_splits_out_of_band_messages_from_four_standby_frames(monkeypatch, tmp_path):
    history_dir = tmp_path / "history"
    history_dir.mkdir()
    (history_dir / "lcd-history-0.txt").write_text(
        "\n".join(
            [
                '{"ts":"2026-08-24T10:00:00+00:00","line1":"EVENT 1","line2":"one","label":"high"}',
                '{"ts":"2026-08-24T10:01:00+00:00","line1":"EVENT 2","line2":"two","label":"event"}',
                '{"ts":"2026-08-24T10:02:00+00:00","line1":"EVENT 3","line2":"three","label":"summary"}',
                '{"ts":"2026-08-24T10:03:00+00:00","line1":"EVENT 4","line2":"four","label":"high"}',
                '{"ts":"2026-08-24T10:04:00+00:00","line1":"EVENT 5","line2":"five","label":"high"}',
            ]
        ),
        encoding="utf-8",
    )
    runner_history = tmp_path / "runner.ndjson"
    runner_history.write_text(
        '{"ts":"2026-08-24T10:05:00+00:00","line1":"STANDBY","line2":"ignored","label":"script"}\n',
        encoding="utf-8",
    )
    standby_script = tmp_path / "lcd-rotation.script"
    standby_script.write_text(
        '\n'.join(f'frame "SCREEN {index}" "VALUE {index}"' for index in range(1, 5)),
        encoding="utf-8",
    )
    monkeypatch.setenv("GWAY_KIOSK_LCD_HISTORY_DIR", str(history_dir))
    monkeypatch.setenv("GWAY_KIOSK_LCD_RUNNER_HISTORY_FILE", str(runner_history))
    monkeypatch.setenv("GWAY_KIOSK_LCD_STANDBY_SCRIPT", str(standby_script))

    history = kiosk_status.lcd_history_context()

    assert [item["line1"] for item in history["latest"]] == ["EVENT 5", "EVENT 4", "EVENT 3", "EVENT 2"]
    assert [item["line1"] for item in history["standby"]] == ["SCREEN 1", "SCREEN 2", "SCREEN 3", "SCREEN 4"]


def test_services_only_show_systemd_entries_not_the_duplicate_ap_portal_health_row():
    html = kiosk_status.render_services(
        {
            "portal": {"label": "AP portal", "state": "200", "ok": True},
            "services": [
                {
                    "label": "arthexis-ap-portal",
                    "state": "active",
                    "ok": True,
                }
            ],
        }
    )

    assert "AP portal" not in html
    assert "arthexis-ap-portal" in html


def test_only_arthexis_and_lcd_services_render_restart_controls():
    html = kiosk_status.render_services(
        {
            "services": [
                {"name": "arthexis.service", "label": "arthexis", "state": "active", "ok": True, "restartable": True},
                {"name": "lcd-arthexis.service", "label": "lcd-arthexis", "state": "active", "ok": True, "restartable": True},
                {"name": "rfid-scan-publisher.service", "label": "rfid-scan-publisher", "state": "active", "ok": True, "restartable": False},
            ]
        }
    )

    assert html.count(">RESTART</button>") == 2
    assert 'data-kiosk-restart-service="arthexis.service"' in html
    assert 'data-kiosk-restart-service="lcd-arthexis.service"' in html
    assert 'data-kiosk-restart-service="rfid-scan-publisher.service"' not in html
    assert (
        '<span>arthexis</span><button class="kiosk-service__restart" type="button" '
        'data-kiosk-restart-service="arthexis.service"'
    ) in html
    assert "grid-template-columns: minmax(0, 1fr) auto auto;" in kiosk_status.render_html(kiosk_status.build_payload())


def test_ap_reset_only_runs_the_maintained_reveal_command(monkeypatch, tmp_path):
    reset_command = tmp_path / "prodigy-reveal-ap"
    lcd_tool = tmp_path / "prodigy-password-lcd-60s"
    reset_command.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    lcd_tool.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    reset_command.chmod(0o755)
    lcd_tool.chmod(0o755)
    calls = []
    monkeypatch.setattr(kiosk_status, "AP_RESET_COMMAND", reset_command)
    monkeypatch.setattr(kiosk_status, "AP_RESET_LCD_TOOL", lcd_tool)
    monkeypatch.setattr(
        kiosk_status.subprocess,
        "run",
        lambda args, **kwargs: calls.append((args, kwargs))
        or type("Result", (), {"returncode": 0})(),
    )

    assert kiosk_status.reset_access_point()
    assert calls[0][0] == [str(reset_command)]
    assert calls[0][1]["env"]["PRODIGY_AP_LCD_TOOL"] == str(lcd_tool)


def test_services_are_sorted_alphabetically(monkeypatch):
    monkeypatch.setenv(
        "GWAY_KIOSK_STATUS_SERVICES",
        "rfid-scan-publisher.service,lcd-arthexis.service,gway-kiosk-status.service,arthexis-ap-portal.service,arthexis.service",
    )
    monkeypatch.setattr(
        kiosk_status,
        "service_status",
        lambda name: {"name": name, "label": name.removesuffix(".service"), "state": "active", "ok": True},
    )

    assert [service["label"] for service in kiosk_status.service_statuses()] == [
        "arthexis",
        "arthexis-ap-portal",
        "gway-kiosk-status",
        "lcd-arthexis",
        "rfid-scan-publisher",
    ]


def test_console_proxy_requires_a_loopback_target(monkeypatch):
    monkeypatch.delenv("KIOSK_CONSOLE_URL", raising=False)
    assert kiosk_status.console_target_url("/kiosk/console/ocpp/") == "http://127.0.0.1:8888/ocpp/"

    monkeypatch.setenv("KIOSK_CONSOLE_URL", "https://console.example.test/")
    assert kiosk_status.console_target_url("/kiosk/console/") == ""


def test_console_proxy_rewrites_root_relative_assets():
    rendered = kiosk_status.rewrite_console_html(
        b'<link href="/static/pages.css"><form action="/login/"><img src="/media/a.png">'
    ).decode()

    assert 'href="/kiosk/console/static/pages.css"' in rendered
    assert 'action="/kiosk/console/login/"' in rendered
    assert 'src="/kiosk/console/media/a.png"' in rendered


def test_console_proxy_returns_rewritten_admin_redirects_to_the_browser():
    handler = kiosk_status.NoRedirectHandler()

    assert handler.redirect_request(None, None, 302, "Found", {}, "http://127.0.0.1:8888/admin/") is None
    assert (
        kiosk_status.KioskStatusHandler._console_location(object(), "/admin/login/?next=/admin/")
        == "/kiosk/console/admin/login/?next=/admin/"
    )


def test_card_keeps_full_rfid_detail_without_a_uid_subtitle_and_orders_command_first():
    payload = kiosk_status.build_payload()
    payload["last_card"].update(
        {
            "display_label": "Test card",
            "rfid": "subtitle-uid-should-not-render",
            "full_rfid": "full-rfid-detail",
            "details": [],
            "command": {
                "command": "run-task",
                "execution_status": "complete",
                "card_name": "Test card",
                "template": "",
                "execution_detail": "",
            },
        }
    )

    html = kiosk_status.render_html(payload)
    card_panel = html.split('class="kiosk-panel kiosk-panel--card"', 1)[1].split("</article>", 1)[0]

    assert "subtitle-uid-should-not-render" not in card_panel
    assert "full-rfid-detail" in card_panel
    assert card_panel.index(">Command<") < card_panel.index(">Command status<")
    assert "--kiosk-qr-size: clamp(126px, 18vmin, 162px);" in html
