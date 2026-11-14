# Gree Firmware Downgrader

Tool for restoring LAN access to Gree devices, and optionally loading local-only firmware.

## Install and run

Requires Python 3.11+ and access to the LAN so that the script can server the firmware update to your device.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python downgrade.py
```

To include cloud-only devices, specify your Gree+ account's region:

```sh
.venv/bin/python downgrade.py --region Europe
```

Valid `--region` values: `Australia`, `China Mainland`, `East South Asia`, `Europe`, `India`, `Middle East`, `North American`, `Russia`, `South American`. Quote names containing spaces, for example `--region "North American"`.

The tool prompts privately for cloud credentials, or reads `GREE_USERNAME` and `GREE_PASSWORD`. Cloud access has an explicit certificate-validation warning; do not use it on an untrusted network.

## Firmware reference

Cloud-only families follow [greeclimate's inspected-build list](https://github.com/cmroche/greeclimate/blob/main/docs/CLOUD_ONLY_FIRMWARE.md) dated 17 October 2026. Filenames below identify representative inspected files; linked files are bundled here. The additional CS532AE row covers a LAN-capable build with local-only firmware, not an established cloud-only family.

| Firmware code | Module | Latest verified LAN firmware | Earliest observed cloud-only firmware | Bundled local-only firmware |
|---|---|---|---|---|
| `362001066300` | `4MWB05DF13` | [`362001066300-v3.05.bin`](firmware/362001066300-v3.05.bin) | `362001066300+U-4MWB05DF13V3.27-eu.bin` | — |
| `362001067540` | `WB05WR13` | [`362001067540-v2.06.bin`](firmware/362001067540-v2.06.bin) | `362001067540+U-WB05WR13V2.04-test.bin` | — |
| `362001067541` | `WB05WR11` | [`362001067541-v2.04.bin`](firmware/362001067541-v2.04.bin) | `362001067538+U-WB05WR11V2.08-test.bin` | — |
| `362001068254` | `4MWB65VR` | [`362001068254-v2.11.bin`](firmware/362001068254-v2.11.bin) | `362001068254+U-4MWB65VRV2.12-eu.bin` | — |
| `362001068295` | `4MWB05UR` | [`362001068295-v2.02.bin`](firmware/362001068295-v2.02.bin) | `362001068295+U-4MWB05URV2.04-eu.bin`<br>`362001068295+U-4MWB05URV2.04-test.bin` | — |
| `362001068411` | `WB05HR13` | Not found | `362001068411-apiV1.02-test-history.bin` | — |
| `362001071519` | `4MWB65AR11` | Not found | `362001067928+U-4MWB65AR11V2.00-test.bin` | — |
| `362001000762` | `CS532AE(LT)` | `362001000762+U-CS532AE(LT)V3.33.bin` (not bundled) | Not established | [`362001000762-v3.33-local-only.bin`](firmware/362001000762-v3.33-local-only.bin) |

## Firmware selection

- Each identified device offers **keep current** and all compatible bundled images. Matching uses firmware code/module, not numeric version ordering.
- Local-only images are `firmware/*-local-only.bin`; they need no separate catalog or additional approval.
- All choices are collected before updates start. Selecting an image authorizes one update for that device. Unsupported and blocked devices remain visible.
- Images are served unchanged from distinct SHA-256 URLs on LAN port 80. The tool never downloads firmware or retries an OTA automatically.

Use `--bind <this-computer's-LAN-IPv4>` if automatic interface selection is unsuitable. Port 80 must be available and reachable from the devices. If your OS requires administrator privileges, use an elevated terminal; the tool does not escalate privileges or change firewall/routing settings.

Updates run one device at a time, with fresh identity checks and post-update LAN observations. An unresolved update stops subsequent installations and keeps hosting available until Ctrl-C. Private operation records are under `${XDG_STATE_HOME:-~/.local/state}/gree-firmware-downgrader/`.

Flashing can brick a module or lose pairing/settings. Keep power stable. LAN availability and an unchanged HID cannot prove the installed bytes of a local-only image; the tool does not claim flash readback or physical HVAC verification.

Project code: [GPL-3.0](LICENSE). Vendor firmware redistribution rights remain unverified.

