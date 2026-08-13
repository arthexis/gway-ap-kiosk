# RFID Mode Card Tree Template

This directory documents the shape of `/home/arthe/.config/rfid-modes/cards`.

Use placeholder names such as `FIRST_CARD`, `SECOND_CARD`, and `CLEAR_CARD` in
repository templates. Replace them with real card IDs only on the rebuilt host.
Do not commit live card IDs or command scripts containing secrets.

Top-level entries are directories. If a same-name `.sh` or `.py` sidecar exists
beside a directory, scanning that card both enters the directory and runs the
sidecar script.
