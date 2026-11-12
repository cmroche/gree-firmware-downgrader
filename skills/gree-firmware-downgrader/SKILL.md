---
name: gree-firmware-downgrader
description: >
  Discover Gree Wi-Fi devices through cloud and LAN inventory, select each
  device's latest verified LAN-capable firmware, acquire the exact original
  from Gree, and restore LAN access with an approved cloud OTA or optional
  direct LAN upgrade. Verify fresh same-device HID and local status replies.

# Gree firmware downgrader

Restore local access with the newest **verified LAN-capable build**, which is not necessarily numerically older. Use Gree cloud inventory as the primary source for cloud-only devices and LAN discovery as a supplement. Keep blocked and skipped devices visible.

The optional direct-LAN upgrade assumes devices share the scan/bind/status/URL-based `upgrade` workflow. This is not cross-device validation. Cloud OTA uses the separate documented client protocol. Neither route assumes identical CPUs, image formats, flash sizes or interchangeable firmware. Never apply CS532AE's image wrapper or payload-size limit to another family, and never repackage an original image.

## 1. Read prerequisites and establish authority

Resolve bundled references relative to this `SKILL.md`, not the user's current directory. No sibling repository checkout is required. Read:

- [Firmware-version reference](references/firmware-versions.md), then compare its linked public upstream cloud-only table, HIDs and MIDs. Preserve the snapshot date and record which revision/evidence selected the targets; do not edit the installed plugin cache.
- [Cloud inventory and OTA guide](references/cloud-access.md) for account-region inventory, cloud authentication/TLS scope, explicit one-device approval, image hosting and post-OTA LAN verification.
- [Firmware acquisition reference](references/firmware-download.md) for regional lookup, historical provenance, verified TLS, URL safety, checksums and throttling. Acquisition sends no device upgrade packets; installation is this skill's separate, explicitly authorized phase.

```sh
umask 077
state="${XDG_STATE_HOME:-$HOME/.local/state}/gree-firmware-downgrader"
mkdir -p "$state"
run=$(mktemp -d "$state/run.XXXXXX")
python3 -m venv "$run/venv"
"$run/venv/bin/python" -m pip install 'greeclimate==4.2.5' 'greeclimate-davo22==2.7.5'
```

Run private scripts with that environment's Python. Keep device inventory, attempted downloads, original metadata/headers, image hashes, approval, send timestamps and verification observations in the private run directory, never in the plugin or a public repository. Never store credentials or raw encrypted/decrypted packets; disable `greeclimate` and every loaded child logger before LAN binding.

**Creating or reading this skill is not authorization to flash.** Discovery and read-only firmware queries may prepare a plan. For direct LAN upgrade, show the per-device canonical MAC, IP, current full HID, proposed full HID, original Gree URL, SHA-256 and HTTP/region limitations. For cloud OTA, follow the exact terminal approval challenge in the cloud guide; previous conversational consent is not a substitute. Explain possible bricking, lost pairing/settings and loss of cloud/local access. Do not alter HVAC controls, Wi-Fi settings, calibration or power automatically. Require stable power and an uninterrupted maintenance window.

## 2. Discover and identify devices

Start with `gree_cloud.py inventory --region '<account region>'` as documented in [Cloud inventory and OTA](references/cloud-access.md). Use the account's actual region; the output keeps physical modules, blocked logical children and failures. It can identify devices with no LAN response. Then use the existing client below as an optional LAN supplement or for direct-LAN upgrade; do not build a new UDP discovery implementation.

```python
import asyncio
import logging
from ipaddress import IPv4Address

from greeclimate.device import Device
from greeclimate.discovery import Discovery
from greeclimate.network import Response


def canonical_mac(value):
    if not isinstance(value, str):
        raise ValueError("Missing physical-module MAC")
    mac = value.replace(":", "").replace("-", "").replace(".", "").lower()
    if len(mac) != 12 or any(c not in "0123456789abcdef" for c in mac):
        raise ValueError("Not a physical-module MAC")
    return mac


async def discover(target_addresses=None):
    discovery = Discovery()
    try:
        return await discovery.scan(wait_for=3, target_addresses=target_addresses)
    finally:
        discovery.close()


async def fresh_hid(device):
    future = asyncio.get_running_loop().create_future()

    def received(data):
        hid = data.get("hid")
        if not future.done() and isinstance(hid, str) and hid:
            future.set_result(hid)

    device.add_handler(Response.DATA, received)
    try:
        await device.send(device.create_status_message(device.device_info, "hid"))
        return await asyncio.wait_for(future, timeout=5)
    finally:
        device.remove_handler(Response.DATA, received)


async def observe(info):
    canonical_mac(info.mac)
    device = Device(info, bind_timeout=3)
    try:
        await asyncio.wait_for(device.bind(), timeout=8)
        return await fresh_hid(device)
    finally:
        device.close()
```

Disable package logging after imports, before calling these helpers:

```python
for name in ("greeclimate", *tuple(logging.Logger.manager.loggerDict)):
    if name == "greeclimate" or name.startswith("greeclimate."):
        logging.getLogger(name).disabled = True
```

1. Run `await discover()` on the user's LAN. On multi-interface hosts, use the actual LAN interface's broadcast address, e.g. `[IPv4Address(address)]`; obtain it from interface/router information, not a guessed subnet. Explicit known device IPs can also be passed as unicast targets. Do not scan unrelated networks.
2. Deduplicate by canonical MAC, never device name, MID or IP alone. Keep each physical module's latest reply-source IP/port; never flash a gateway's logical child or use target MAC `"1"`/`"2"` for companion-image updates.
3. For each device, call `await observe(info)` and save its full HID alongside MAC/IP/port/MID. Catch timeout, binding and transport failures per device, record them, and continue inventory. `bind()`, `request_version()` and `update_state()` returning do not by themselves mean a HID reply arrived; use the fresh DATA helper, not a cached property.
4. Extract the literal code before `+` and the module/version from the full HID. Keep discovery `ver`, HID version and API `ver` separate. Do not infer firmware from MID, retail name or IP.
5. A cloud-only module may not answer scan, bind or status. Keep it in the cloud inventory and use the documented cloud OTA route only when it has an exact supported route and freshly solicited HID. Compare with authorized Home Assistant/router inventory to name missing devices, and try read-only unicast at known addresses. A silent scan does not prove absence; without an identity-verified install transport and full HID, report `blocked_unreachable`/`blocked_identity`.

## 3. Select the latest verified LAN build

For every identified device, use the reference's explicit LAN target for its exact code/module variant. Require original provenance tying the candidate build to that LAN evidence, including a known hash when available; resolve duplicate-version builds by hash/evidence, not filename. If provenance is ambiguous, record `blocked_ambiguous_build`.

- Already running the exact selected LAN build: `already_target`; verify a fresh LAN bind/status roundtrip and do not reflash.
- LAN target absent, including the reference's two **Not found** rows: `blocked_no_lan_target`.
- Family absent from the reference: seek positive exact-build LAN evidence in the linked upstream sources or existing authorized research. Neither "not in the cloud-only table" nor "Gree advertises it" proves LAN support. If none is established, `blocked_no_lan_target`.
- Non-monotonic versions are intentional: code `362001067540` selects `2.06`, not an invented version below `2.04`.
- Record the expected **full target HID** from build evidence before installation. Do not use the version number alone or silently substitute a module/region variant.

Do not change `AutoUpdate` automatically. State that vendor/app automatic updates may later replace the restored LAN firmware; neither its default nor its prevention is established here.

## 4. Download the exact original from Gree

Follow the bundled acquisition reference, serially: one request start per second globally across metadata, archive lookups, downloads, alternatives and redirect hops; obey its cooldowns and bounded retry limits. Query regional/auxiliary `Lastversion?firmwareCode=<code>` endpoints to find the selected **exact target**, not simply the currently advertised newest release. Do not add an invented `version=` parameter or treat cloud-only versions as numeric cutoffs.

For an older target, use existing research or archive `.md` sidecars/history to recover an evidenced historical **official Gree image URL**. Archive metadata can locate it; this request requires the binary from Gree, so do not silently install a GitHub/mirror payload. If Gree no longer serves the exact candidate, report `blocked_download` with checked sources; another origin requires explicit scope approval.

Download before asking any device to install. Preserve complete original bytes, metadata, final headers and source/effective URLs. Verify the transfer completed, `Content-Length` when present, `FileMd5` when present, SHA-256, code/module/region/version association and the known LAN-build hash when available. Inspect for HTML/JSON error bodies and conflicting embedded identity. Identical version labels do not establish identical builds. Do not fabricate hashes or treat MD5 as a signature. Keep one verified file per distinct hash while retaining each device's provenance association. Keep verified TLS enabled for firmware acquisition; the cloud guide's narrowly scoped TLS exception is not an acquisition fallback.

### Device-facing image URL

Prefer the official direct HTTP image URL when it serves the same hash-verified bytes. Use `firmware_http.py preflight` as described in [Cloud inventory and OTA](references/cloud-access.md); a successful host-side GET does not prove device reachability. For the optional direct-LAN route, the following URL limits are conservative observations for CS532AE, not universal rules for Gree cloud OTA or all device families:

- Plain ASCII `http://host/path`, at most 159 bytes; IPv4 or DNS resolving to IPv4; no credentials, explicit port, fragment, controls or whitespace. Port 80 only, with no HTTPS, authentication or redirect dependency.
- Direct 200, positive `Content-Length`, uncompressed and unchunked bytes, small headers, and compatibility with an IP-valued `Host:` header. The inspected CS532AE parser may be fragile under TCP fragmentation.

If explicitly approved, use the sibling `firmware_http.py serve` helper to host only the already verified, unchanged Gree image at `/firmware.bin` on an approved LAN IPv4 address, port 80. Do not use generic `python -m http.server`, expose run data, bind publicly, change firewalls, or run sudo automatically. A LAN URL is an explicit device-facing hosting exception, not a firmware-acquisition exception; preserve the official Gree `source_url` provenance. Keep the server running through verification, including an unconfirmed outcome until the user is informed.

## 5. Request installation, one device at a time

Finish target selection, acquisition, URL checks and approval first.

### Cloud OTA for cloud-only devices

Use `scripts/gree_cloud.py install` following [Cloud inventory and OTA](references/cloud-access.md). It refreshes cloud inventory and HID before approval, requires the exact per-device terminal challenge, rechecks HID after approval, and records an exclusive durable ledger before its single send. The selected protocol comes from the cloud record, not the numeric HID version. Do not call cloud HVAC-control methods or retry/reconnect/resend an OTA. Do not test a valid OTA URL by issuing an OTA request as a dry run: it may flash immediately. Do offline protocol checks and read-only cloud status first. After dispatch, even an ambiguous exception, continue to fresh LAN verification below; cloud acknowledgement or online state is not installation proof.

### Optional direct-LAN upgrade

For an already LAN-reachable module using the direct-LAN route, immediately rediscover the same canonical MAC and freshly re-read its HID; use the latest endpoint. If identity, original HID or approved target/image/URL changed, stop and revise the plan instead of flashing whoever occupies the old IP.

Use the negotiated device session for the request. There is no `Commands.UPGRADE` or firmware-installer method in the current library; construct the existing protocol envelope directly, not `create_command_message()` and not `push_state_update()`:

```python
async def request_install(info, expected_old_hid, firmware_url):
    # Call only after approving and validating this exact device/target/URL plan.
    mac = canonical_mac(info.mac)
    device = Device(info, bind_timeout=3)
    try:
        await asyncio.wait_for(device.bind(), timeout=8)
        if await fresh_hid(device) != expected_old_hid:
            raise RuntimeError("Firmware changed since approval; installation not sent")
        await device.send({
            "cid": "app", "i": 0, "t": "pack", "uid": 0, "tcid": mac,
            "pack": {"t": "upgrade", "mac": mac, "url": firmware_url},
        })
    finally:
        device.close()
```

Persist `install_requested` and the exact plan/send time **before** calling the send so an interrupted run never automatically repeats it. If sending fails or the process dies, report dispatch as uncertain; UDP send completion is not device acknowledgement. A `ret/r=200` response, where decoded, means acceptance only; it may use the bootstrap key rather than the negotiated key, so a missing/undecodable reply is not a reason to resend. Do not change the session cipher or add a new library response enum just for the ack. Never automatically retry an upgrade packet or issue power/reset commands. Proceed to read-only verification even if the acknowledgement was lost.

## 6. Validate installation and restored LAN access

After the send, retire the old object/session/cache. For up to five minutes per device, use monotonic time, pause between attempts, and perform fresh discovery (including unicast at its last known address), canonical-MAC matching and a **new** `Device` bind. Use a fresh `Discovery` instance per pass: its accumulated list is not proof of a new reply. Default pauses are five seconds; bound each bind/status attempt to the remaining overall deadline and close every object in `finally`.

On the matching MAC, obtain a fresh HID via `observe(info)`. Only the exact expected target HID is a candidate success. Old HID means not yet installed; a different code/module/unexpected HID is an explicit mismatch, not success. Confirm the target again with another fresh bind/HID status roundtrip at least five seconds later. Follow DHCP address changes only for this same MAC; a response from another unit must never satisfy the operation. Do not require observing an offline interval: reboot traffic can be missed.

Report `verified_reported_hid` only after both post-send observations match and LAN bind/status works; cloud-online status after downgrade is not required. Record MAC, old/new full HID, selected file/SHA-256, URL/provenance and observation times/endpoints. This verifies the **device-reported firmware identity and functioning LAN protocol**, not a readback hash of installed flash, physical HVAC functions or anti-rollback/boot safety. An unchanged HID cannot prove a reinstall; an initially matching target remains `already_target`.

If the deadline expires, report `install_unconfirmed`, last observed HID and whether LAN replies returned. No response is not proof of bricking, completion or rollback. Do not retry the flash, switch images or turn off the image server without informing the user of the unresolved transfer. Stop further installations after an ambiguous/failed/mismatched installation and report remaining devices as pending; continue only read-only investigation until the user decides how to proceed.

## 7. Close out every device

Return one row per discovered/previously inventoried device: canonical MAC/name, before/target/after HID, image hash/source, outcome and precise blocker. Include `already_target`, blocked, verified, unconfirmed and pending devices; never claim all LAN devices were found from a silent scan. List which operations were actually run and the private evidence directory. After successful verification, stop any temporary image server you started; preserve the original downloads and evidence. Do not publish keys, private captures or device inventory.

Sources: [Gree upgrade exchange](https://github.com/cmroche/greeclimate/issues/111), [original firmware archive metadata](https://github.com/maxim-smirnov/gree-wifimodule-firmware), and the reference's linked build evidence. The shared direct-LAN protocol is an operating assumption, not tested across every listed firmware.
