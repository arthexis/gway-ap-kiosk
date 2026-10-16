# GWAY RFID

GWAY uses a standalone local RFID scan publisher plus a card-path mode runner.
The publisher owns the RC522 reader and writes passive scan state. The mode
runner watches that scan file and maps card paths under
`/home/arthe/.config/rfid-modes/cards` to local scripts.

Live card IDs, card labels, scan logs, mode logs, voice memo audio, and
`cards.txt` contents are runtime state. Keep them out of normal source control.

## Repository Files

| Source path | Install target |
| --- | --- |
| `scripts/gway/rfid_scan_publisher.py` | `/home/arthe/.local/bin/rfid-scan-publisher` |
| `scripts/gway/rfid_mode_runner.py` | `/home/arthe/.local/bin/rfid-mode-runner` |
| `scripts/gway/rfid_mode_register.py` | `/home/arthe/.local/bin/rfid-mode-register` |
| `scripts/gway/rfid_mode_register.py` | `/home/arthe/.local/bin/rfid-register` symlink |
| `scripts/gway/rfid_clear_current.py` | `/home/arthe/.local/bin/rfid-clear-current` |
| `scripts/gway/rfid_arm_next_script.py` | `/home/arthe/.local/bin/rfid-arm-next-script` |
| `scripts/gway/rfid_mode_reset_root_if_current.py` | `/home/arthe/.local/bin/rfid-mode-reset-root-if-current` |
| `scripts/gway/rfid_record_30s.py` | `/home/arthe/.local/bin/rfid-record-30s` |
| `scripts/gway/rfid_voice_memo.py` | `/home/arthe/.local/bin/rfid-voice-memo` |
| `scripts/gway/rfid_voice_memo_arm_next.py` | `/home/arthe/.local/bin/rfid-voice-memo-arm-next` |
| `scripts/gway/rfid-bluetooth-keyboard-pair` | `/home/arthe/.local/bin/rfid-bluetooth-keyboard-pair` |
| `scripts/gway/rfid-bind-bluetooth-keyboard-pair` | `/home/arthe/.local/bin/rfid-bind-bluetooth-keyboard-pair` |
| `scripts/gway/rfid_etron_register_service.py` | `/usr/local/libexec/gway-home-tools/rfid-etron-register-service.py` |
| `config/systemd/gway/rfid/` | RFID system and user services |
| `config/requirements/gway-rfid-mode.txt` | Hardware-reader virtualenv pins |
| `config/templates/gway-rfid.env.template` | Optional local override reference |
| `config/templates/rfid-modes/` | Placeholder card-tree shape |
| `config/templates/rfid-cards.txt.template` | Redacted `cards.txt` shape |
| `docs/man/man1/rfid-*.1` | RFID command manuals |

## Current Service Shape

Snapshot date: 2026-07-28.

| Unit | Current role | Rebuild note |
| --- | --- | --- |
| `rfid-scan-publisher.service` | Active and enabled system service. | Owns the RC522 hardware, writes the latest status snapshot, and commits card-presence events to a durable FIFO queue. |
| `rfid-mode-runner.service` | Active and enabled user service. | Consumes one queued card command at a time and runs local scripts from `/home/arthe/.config/rfid-modes/cards`. |
| `rfid-replicant.service` | Installed but inactive and disabled. | Legacy suite-backed scanner path; do not enable while the standalone publisher owns the reader. |

The promoted scan publisher defaults to SPI bus 0, SPI device 0, BCM GPIO mode,
and reset pin BCM GPIO25. The hardware manual still requires a physical check
that the installed reader wiring matches those software defaults.

## Scan Gesture and Queue

Initial detection plays the normal scan beep and briefly shows the normalized
RFID plus its mapped command label on the LCD. That display stays unchanged
while the card is held. Removing the card before six seconds commits its
command to `/home/arthe/.local/state/rfid-mode/queue/pending/`. Holding it for
six seconds starts a deep read only for mappings declaring
`RFID_MODE_QUEUE_DEEP_DATA=1`; the publisher emits the configured deep-read
beep once per second and commits that command when the read finishes, even if
the deep read has no usable data. All other mappings queue on removal. The queue commit plays its own acknowledgement
sound and releases the RFID LCD event so the command can own the display.

The runner atomically claims the oldest pending record and does not claim the
next one until the active command exits. Incomplete claimed records are returned
to pending after a runner restart; commands should therefore remain idempotent.

New command cards are queue-first one-shots. Register `--interrupt` only for
control actions that must preempt work; `--clear-queue` also cancels a pending
queue-aware gadget arm. `--consume-next-seconds N` lets a card intentionally
consume the next queued command. Use `rfid-queue-status` for UID-safe counts,
active command labels and waiting interrupts.

## Physical Steps

No physical step is needed to promote this source into the repository.

Physical checks are needed for full rebuild validation:

1. Confirm the RFID reader is wired to SPI0 and powered at the module's required
   voltage.
2. Confirm the reader reset line is physically wired to BCM GPIO25, or update
   `RFID_RST_PIN` and the hardware manual.
3. Use a non-private test card for validation. Do not scan a high-value or
   production card while validating templates.
4. If writing labels to cards, keep the test card on the reader until the LCD or
   command output confirms the write completed.

## Install

Install the hardware-reader virtualenv:

```bash
python3 -m venv /home/arthe/.local/share/rfid-mode/.venv
/home/arthe/.local/share/rfid-mode/.venv/bin/python -m pip install --upgrade pip
/home/arthe/.local/share/rfid-mode/.venv/bin/python -m pip install \
  -r config/requirements/gway-rfid-mode.txt
```

Install commands:

```bash
install -d -m 0755 "$HOME/.local/bin" "$HOME/.local/share/man/man1"
sudo install -d -m 0755 /usr/local/libexec/gway-home-tools /usr/local/bin

install -m 0755 scripts/gway/rfid_scan_publisher.py "$HOME/.local/bin/rfid-scan-publisher"
install -m 0755 scripts/gway/rfid_mode_runner.py "$HOME/.local/bin/rfid-mode-runner"
install -m 0755 scripts/gway/rfid_mode_register.py "$HOME/.local/bin/rfid-mode-register"
install -m 0755 scripts/gway/rfid_queue_status.py "$HOME/.local/bin/rfid-queue-status"
ln -sfn "$HOME/.local/bin/rfid-mode-register" "$HOME/.local/bin/rfid-register"
install -m 0755 scripts/gway/rfid_clear_current.py "$HOME/.local/bin/rfid-clear-current"
install -m 0755 scripts/gway/rfid_arm_next_script.py "$HOME/.local/bin/rfid-arm-next-script"
install -m 0755 scripts/gway/rfid_mode_reset_root_if_current.py \
  "$HOME/.local/bin/rfid-mode-reset-root-if-current"
install -m 0755 scripts/gway/rfid_record_30s.py "$HOME/.local/bin/rfid-record-30s"
install -m 0755 scripts/gway/rfid_voice_memo.py "$HOME/.local/bin/rfid-voice-memo"
install -m 0755 scripts/gway/rfid_voice_memo_arm_next.py \
  "$HOME/.local/bin/rfid-voice-memo-arm-next"
install -m 0755 scripts/gway/rfid-bluetooth-keyboard-pair \
  "$HOME/.local/bin/rfid-bluetooth-keyboard-pair"
install -m 0755 scripts/gway/rfid-bind-bluetooth-keyboard-pair \
  "$HOME/.local/bin/rfid-bind-bluetooth-keyboard-pair"
sudo install -m 0755 scripts/gway/rfid_etron_register_service.py \
  /usr/local/libexec/gway-home-tools/rfid-etron-register-service.py
sudo ln -sfn /usr/local/libexec/gway-home-tools/rfid-etron-register-service.py \
  /usr/local/bin/rfid-etron-register-service
sudo ln -sfn /usr/local/libexec/gway-home-tools/rfid-etron-register-service.py \
  /usr/local/bin/rfid-etron-register-service.py
```

Install manuals:

```bash
install -m 0644 docs/man/man1/rfid-*.1 "$HOME/.local/share/man/man1/"
```

Install system and user services:

```bash
sudo install -m 0644 config/systemd/gway/rfid/rfid-scan-publisher.service \
  /etc/systemd/system/rfid-scan-publisher.service
sudo install -D -m 0644 config/systemd/gway/rfid/rfid-scan-publisher.service.d/deep-read-hold.conf \
  /etc/systemd/system/rfid-scan-publisher.service.d/deep-read-hold.conf
sudo install -m 0644 config/systemd/gway/rfid/rfid-replicant.service \
  /etc/systemd/system/rfid-replicant.service

install -d -m 0755 "$HOME/.config/systemd/user"
install -m 0644 config/systemd/gway/rfid/user/rfid-mode-runner.service \
  "$HOME/.config/systemd/user/rfid-mode-runner.service"

sudo systemctl daemon-reload
systemctl --user daemon-reload
sudo systemctl enable --now rfid-scan-publisher.service
systemctl --user enable --now rfid-mode-runner.service
sudo systemctl disable --now rfid-replicant.service
```

Install local templates only after replacing placeholders:

```bash
install -d -m 0700 "$HOME/.config/rfid-modes/cards"
install -m 0600 config/templates/rfid-modes/scan-file.template \
  "$HOME/.config/rfid-modes/scan-file"
install -m 0600 config/templates/rfid-cards.txt.template "$HOME/cards.txt"
```

Do not copy `config/templates/rfid-modes/cards` directly into production without
renaming placeholders to real local card IDs.

## Validation

Software and service checks:

```bash
ls -l /dev/spidev0.*
systemctl status rfid-scan-publisher.service --no-pager
systemctl --user status rfid-mode-runner.service --no-pager
rfid-register --list
rfid-mode-runner --status
cat /home/arthe/.local/state/rfid-mode/rfid-scan-publisher.json
find /home/arthe/.local/state/rfid-mode/queue -maxdepth 2 -type f
```

One-card reader check with a non-private test card:

```bash
/home/arthe/.local/share/rfid-mode/.venv/bin/python \
  /home/arthe/.local/bin/rfid-scan-publisher --once --once-timeout 10 \
  --scan-file /tmp/rfid-scan.json --log-file /tmp/rfid-scans.ndjson \
  --event-log /tmp/rfid-events.ndjson --status-file /tmp/rfid-status.json
```

Mode-runner dry-run against the test scan:

```bash
RFID_MODE_SCAN_FILE=/tmp/rfid-scan.json rfid-mode-runner --once --dry-run
```

Registration dry-runs that do not bind production cards:

```bash
rfid-bind-bluetooth-keyboard-pair --dry-run --card TEST_CARD
rfid-voice-memo-arm-next --help
rfid-arm-next-script --help
```

Expected rebuild signals:

- `/dev/spidev0.0` or `/dev/spidev0.1` exists and the service user has `spi`
  group access.
- `rfid-scan-publisher.service` is active and writes fresh status when a test
  card is scanned.
- `rfid-mode-runner.service` is active and consumes queued records one at a
  time.
- `rfid-register --list` reads the card tree without exposing private card IDs
  in public output.
## Safety Notes

- Do not commit `/home/arthe/.config/rfid-modes/cards`, state, logs, backups,
  `cards.txt`, voice memo databases, WAV files, or raw scan JSON.
- Do not bind new behavior to a known high-value card unless replacement is
  explicitly intended.
- Do not enable both `rfid-scan-publisher.service` and a suite/replicant RFID
  hardware owner at the same time.
- Do not put secrets in RFID scripts. Use scripts that terminate cleanly on
  `SIGTERM`.
- Keep card-label writes to non-private test cards until the rebuild process is
  proven.
