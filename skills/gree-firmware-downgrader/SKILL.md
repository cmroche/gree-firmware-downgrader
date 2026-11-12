---
name: gree-firmware-downgrader
description: >
  Discover Gree Wi-Fi devices on the LAN, select each device's latest verified
  LAN-capable firmware, download the exact original from Gree, request its
  installation after explicit approval, and verify fresh same-device HID and
  local status replies. Use to downgrade cloud-only firmware or restore LAN.
---

# Gree firmware downgrader

Restore local access with the newest **verified LAN-capable build**, which is not necessarily numerically older. Complete inventory → selection → verified Gree download → approved install → fresh LAN verification for every discovered physical module, recording blocked and skipped devices rather than losing them.

Working assumption requested by the user: devices share the scan/bind/status/URL-based `upgrade` workflow. This is an operating assumption, not cross-device validation. Negotiate V1/V2 independently; do not assume identical CPUs, image formats, flash sizes or interchangeable firmware. Never apply CS532AE's image wrapper or payload-size limit to another family, and never repackage an original image.

## 1. Read prerequisites and establish authority

Resolve bundled references relative to this `SKILL.md`, not the user's current directory. No sibling repository checkout is required. Read:

- [Firmware-version reference](references/firmware-versions.md), then compare its linked public upstream cloud-only table, HIDs and MIDs. Preserve the snapshot date and record which revision/evidence selected the targets; do not edit the installed plugin cache.
- [Firmware acquisition reference](references/firmware-download.md) for regional lookup, historical provenance, TLS/URL safety, checksums and throttling. Acquisition sends no device upgrade packets; installation is this skill's separate, explicitly authorized phase.

Use Python 3.10+ and `greeclimate==4.2.5`, the client API used by the recipes below. Reuse a compatible environment or create a private run directory and environment without changing global Python or plugin files:

```sh
umask 077
state="${XDG_STATE_HOME:-$HOME/.local/state}/gree-firmware-downgrader"
mkdir -p "$state"
run=$(mktemp -d "$state/run.XXXXXX")
python3 -m venv "$run/venv"
"$run/venv/bin/python" -m pip install 'greeclimate==4.2.5'
```

Run private scripts with that environment's Python. Keep device inventory, attempted downloads, original metadata/headers, image hashes, approval, send timestamps and verification observations in the run directory, never in the plugin or a public repository. On Windows, use an equivalent private user directory and the virtual environment's `Scripts/python.exe`. Do not log keys, passwords or raw encrypted/decrypted packets; disable `greeclimate` and every loaded child logger before binding.

**Creating or reading this skill is not authorization to flash.** Discovery and read-only firmware queries may prepare a plan. Before the first upgrade request, show the per-device canonical MAC, IP, current full HID, proposed full HID, original Gree URL, SHA-256 and any HTTP/region limitations. Obtain explicit approval for that device/target list; an existing explicit approval covering the same list and policy suffices. Explain possible bricking, lost pairing/settings and loss of cloud/local access. Do not alter HVAC controls, Wi-Fi settings, calibration or power automatically. Require stable power and an uninterrupted maintenance window.

## 2. Discover and identify devices

Use the existing client, not a new UDP discovery implementation. Import the following helpers into a private run script; definitions alone send nothing:

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
5. A cloud-only module may not answer scan, bind or status. Compare with any existing authorized Home Assistant/router inventory to name missing devices, and try read-only unicast at known addresses. A silent scan does not prove absence. Without a reachable identity-verified install transport and full HID, report `blocked_unreachable`/`blocked_identity`; this LAN skill cannot conjure a disabled UDP listener. Do not invent a cloud relay or claim an unseen device was downgraded.

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

Download before asking any device to install. Preserve complete original bytes, metadata, final headers and source/effective URLs. Verify the transfer completed, `Content-Length` when present, `FileMd5` when present, SHA-256, code/module/region/version association and the known LAN-build hash when available. Inspect for HTML/JSON error bodies and conflicting embedded identity. Identical version labels do not establish identical builds. Do not fabricate hashes or treat MD5 as a signature. Keep one verified file per distinct hash while retaining each device's provenance association.

### Device-reachable download URL

Prefer the original Gree HTTP image endpoint if it serves the same hash-verified bytes in a direct response. Under the shared-updater assumption, conservatively require:

- A plain ASCII `http://host/path` URL of at most 159 bytes; IPv4 or DNS resolving to IPv4. No credentials, explicit port, fragment, control characters or whitespace. TCP port is fixed at 80; no HTTPS, authentication or redirect dependency.
- A direct 200 response, positive `Content-Length`, uncompressed/un-chunked binary bytes and small headers. The inspected parser assumes headers plus image wrapper fit its first receive; this remains fragile under TCP fragmentation.
- A server that works when `Host:` is the resolved IP, not the URL hostname. Check this behavior and compare downloaded bytes, not just HEAD status. A successful host-side GET does not prove the HVAC can reach it.

If the official URL is incompatible, serve only the already verified, **unchanged Gree image** from a temporary directory on an approved LAN IPv4 address, port 80. The standard-library `python -m http.server 80 --bind <LAN-IP> --directory <image-only-directory>` is sufficient; do not expose keys/run metadata, bind publicly, change firewalls, run sudo automatically or expose unrelated files. The LAN-server URL is the only intended private-address exception to the acquisition reference's public-URL rules; never accept a private URL supplied by untrusted vendor metadata. If port 80/routing is unavailable, report `blocked_url` rather than using an unsupported port. Verify the served bytes match the selected SHA-256 and keep the server running through all installation verification, including an unconfirmed outcome until the user is informed.

## 5. Request installation, one device at a time

All acquisition, URL checks and explicit approval must be complete first. Immediately rediscover the same canonical MAC and freshly re-read its HID; use the latest endpoint. If its identity, original HID or approved target/image/URL changed, stop that device and revise the plan instead of flashing whoever occupies its old IP.

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

Report `verified_reported_hid` only after both post-send observations match and LAN bind/status works. Record MAC, old/new full HID, selected file/SHA-256, URL/provenance and observation times/endpoints. This verifies the **device-reported firmware identity and functioning LAN protocol**, not a readback hash of installed flash, physical HVAC functions or anti-rollback/boot safety. An unchanged HID cannot prove a reinstall; an initially matching target remains `already_target`.

If the deadline expires, report `install_unconfirmed`, last observed HID and whether LAN replies returned. No response is not proof of bricking, completion or rollback. Do not retry the flash, switch images or turn off the image server without informing the user of the unresolved transfer. Stop further installations after an ambiguous/failed/mismatched installation and report remaining devices as pending; continue only read-only investigation until the user decides how to proceed.

## 7. Close out every device

Return one row per discovered/previously inventoried device: canonical MAC/name, before/target/after HID, image hash/source, outcome and precise blocker. Include `already_target`, blocked, verified, unconfirmed and pending devices; never claim all LAN devices were found from a silent scan. List which operations were actually run and the private evidence directory. After successful verification, stop any temporary image server you started; preserve the original downloads and evidence. Do not publish keys, private captures or device inventory.

Sources: [Gree upgrade exchange](https://github.com/cmroche/greeclimate/issues/111), [original firmware archive metadata](https://github.com/maxim-smirnov/gree-wifimodule-firmware), and the reference's linked build evidence. Shared-protocol operation is assumed; do not present it as tested across every listed firmware.
