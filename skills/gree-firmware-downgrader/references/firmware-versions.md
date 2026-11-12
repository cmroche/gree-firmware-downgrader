# Known cloud-only and LAN-capable firmware builds

Source: [`greeclimate/docs/CLOUD_ONLY_FIRMWARE.md`](https://github.com/cmroche/greeclimate/blob/91e08befc9209cff53be4c3a98a1a7047802afe1/docs/CLOUD_ONLY_FIRMWARE.md). Table and qualifications copied on 12 November 2026; upstream findings are dated 17 October 2026. SIMCom QMI modules are excluded. Adapted from greeclimate, copyright Clifford Roche, under GPL-3.0; see the repository's `LICENSE`.

The version columns show the **latest verified LAN-capable build** and **earliest observed cloud-only build**, not guaranteed transition releases. Missing intermediate builds and non-monotonic version labels prevent a universal cutoff.

| MID | Firmware Code | Module Type | Firmware Versions Examined | Device Type | Last Verified LAN Version | First Observed Cloud-Only Version |
|---|---|---|---|---|---|---|
| Unknown | `362001066300` | `4MWB05DF13` | `3.05`, `3.27`, `4.12` | Not established | **3.05** — native UDP/7000 bind and receive polling | **3.27** |
| Unknown | `362001067540` | `WB05WR13` | `2.04`, `2.06` | Not established | **2.06** — native UDP/7000 bind and receive polling | **2.04** — not a monotonic cutoff |
| Unknown | `362001067541` | `WB05WR11` | `2.04`, `2.08`, `2.09`, `2.10` | Air conditioner | **2.04** — native verification and owner-confirmed local control | **2.08** |
| `10001` reported on `2.09`; not recovered from these binaries | `362001068254` | `4MWB65VR` | `2.11`, `2.12` | Air conditioner — Airy / Clivia reports | **2.11** — native verification and owner-confirmed local control | **2.12** |
| Unknown | `362001068295` | `4MWB05UR` | `2.02`, `2.04` — two distinct `2.04` builds | Not established | **2.02** — native UDP/7000 bind and receive polling | **2.04** |
| Unknown | `362001068411` | `WB05HR13` | `1.02`, `2.07`, `4.07` | Not established | **Not found** | **1.02** |
| Unknown | `362001071519` | `4MWB65AR11` | `2.00` binary; `2.04` owner report | Air conditioner reported for `2.04`: Gree Clivia R22; `2.00` appliance unverified | **Not found** | **2.00** |

## Important qualifications

- `WB05WR13` non-monotonic version change, possibly means local control was added back after being disabled.

These are inspected-build findings, not firmware-code-wide LAN exclusions or upgrade recommendations. See [known HIDs](https://github.com/cmroche/greeclimate/blob/master/docs/HIDS.md) for public owner reports and firmware-code matching, and [MID/client contracts](https://github.com/cmroche/greeclimate/blob/master/docs/MIDS.md) for supported local interfaces. Detailed native-build evidence is retained in [private firmware research](https://github.com/cmroche/gree-research/blob/main/docs/HIDS.md); public client and emulator tests do not require it. The [current upstream table](https://github.com/cmroche/greeclimate/blob/master/docs/CLOUD_ONLY_FIRMWARE.md) may contain newer findings; private-research access is not required to use the bundled table, but missing exact-build evidence can block a target.

## Selection policy for this skill

- Use each row's explicit **Last Verified LAN Version** as the default target, not a numeric cutoff derived from the cloud-only column. `362001067540` targets `2.06`, even though its cloud-only example is `2.04`: restoring LAN is not always a numeric downgrade.
- `362001068411` and `362001071519` have no established LAN target. Report `blocked_no_lan_target`; do not guess an older release.
- Match firmware code, module/variant and exact build provenance. A shared MID, similar filename or matching version number is not enough to substitute firmware. Preserve hashes when multiple builds share a label.
- "Latest" means latest verified LAN-capable build in this evidence, not the newest release advertised by Gree. Refresh against the upstream table before a run; do not silently promote an unexamined release. For an unlisted family, require additional positive LAN-build evidence or report `blocked_no_lan_target`.
