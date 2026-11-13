# Gree Firmware Downgrader

Interactive Python tool for restoring local control with bundled firmware. Discovers physical Gree modules and lets you select compatible vendor or local-only firmware separately for every device.

## Install and run

Requires Python 3.11+ and access to the devices' LAN.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python downgrade.py
```

To include cloud-only devices, specify your Gree+ account's region:

```sh
.venv/bin/python downgrade.py --region Europe
```

The tool prompts privately for cloud credentials, or reads `GREE_USERNAME` and `GREE_PASSWORD`. Cloud access has an explicit certificate-validation warning; do not use it on an untrusted network.

## Firmware selection

- Each identified device offers **keep current** and all compatible bundled images. Matching uses firmware code/module, not numeric version ordering.
- Local-only images are `firmware/*-local-only.bin`; they need no separate catalog or additional approval.
- All choices are collected before updates start. Selecting an image authorizes one update for that device. Unsupported and blocked devices remain visible.
- Images are served unchanged from distinct SHA-256 URLs on LAN port 80. The tool never downloads firmware or retries an OTA automatically.

Use `--bind <this-computer's-LAN-IPv4>` if automatic interface selection is unsuitable. Port 80 must be available and reachable from the devices. If your OS requires administrator privileges, use an elevated terminal; the tool does not escalate privileges or change firewall/routing settings.

Updates run one device at a time, with fresh identity checks and post-update LAN observations. An unresolved update stops subsequent installations and keeps hosting available until Ctrl-C. Private operation records are under `${XDG_STATE_HOME:-~/.local/state}/gree-firmware-downgrader/`.

Flashing can brick a module or lose pairing/settings. Keep power stable. LAN availability and an unchanged HID cannot prove the installed bytes of a local-only image; the tool does not claim flash readback or physical HVAC verification.

Project code: [GPL-3.0](LICENSE). Vendor firmware redistribution rights remain unverified.

