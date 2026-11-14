# Gree Firmware Downgrader

Tool for restoring LAN access to Gree devices, and optionally loading local-only firmware.

The local-only firmware disables access to Gree cloud, and telemetry serivces. And in some casese where stock firmware doesn't provide LAN access, restores it fully.

It's possible to revert back to stock firmware from local-only, as the local API supports `upgrade` commands.

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
| `362001068411` | `WB05HR13` | Not available| `362001068411-apiV1.02-test-history.bin` | [`362001068411-v2.07-local-only.bin`](firmware/362001068411-v2.07-local-only.bin) |
| `362001071519` | `4MWB65AR11` | Not available | `362001067928+U-4MWB65AR11V2.00-test.bin` | [`362001071519-v2.00-local-only.bin`](firmware/362001071519-v2.00-local-only.bin) |
| `362001000762` | `CS532AE(LT)` | Not bundled | Not applicable| [`362001000762-v3.33-local-only.bin`](firmware/362001000762-v3.33-local-only.bin) |

## Other information

Firmware is served by opening an HTTP service from your local PC on port 80. If automatic network selection doesn't work, specify the interface with `--bind <this-computer's-LAN-IPv4>`.
