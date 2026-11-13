---
name: gree-firmware-downgrader
description: >
  Discover Gree Wi-Fi devices, select an exact bundled LAN-capable original
  using physical-module and build evidence, verify its manifest size and hash,
  serve the approved image on the LAN, and request an approved cloud OTA or
  optional direct LAN upgrade. Verify fresh same-device HID and local status.
---

# Gree firmware downgrader

Restore local access with an exact bundled **verified LAN-capable build**, which is not necessarily numerically older. Use Gree cloud inventory as the primary source for cloud-only devices and LAN discovery as a supplement. Keep blocked and skipped devices visible. Normal runs do not look up or download firmware.

The optional direct-LAN upgrade assumes devices share the scan/bind/status/URL-based `upgrade` workflow. This is not cross-device validation. Cloud OTA uses the separate documented client protocol. Neither route assumes identical CPUs, image formats, flash sizes or interchangeable firmware. Never apply CS532AE's image wrapper or payload-size limit to another family, and never repackage an original image.

## 1. Read prerequisites and establish authority

Resolve bundled references relative to this `SKILL.md`, not the user's current directory. No sibling repository checkout is required. Read:

- [Firmware-version reference](references/firmware-versions.md) and its `firmware/manifest.json` identify supported targets and exact bundled originals. Match physical-module firmware code, module and build evidence; no sibling research checkout is required to use the bundle.
- [Cloud inventory and OTA guide](references/cloud-access.md) for account-region inventory, cloud authentication/TLS scope, explicit one-device approval, bundled-image hosting and post-OTA LAN verification.
- [Firmware acquisition reference](references/firmware-download.md) only for an explicitly approved bundle refresh or recovery; it is not part of normal runs.

Before any cloud login, reuse account details only if the user has already explicitly provided or authorized them; otherwise ask:

- Which account region is selected in Gree+? Do not guess from location, firmware download hosts or the guide's example region, or try credentials across multiple regions.
- Where should credentials be read from: a private local file/path, configured `GREE_USERNAME`/`GREE_PASSWORD`, or terminal prompts operated directly by the user?

**Tell the user not to paste credentials, passwords, tokens or device keys into chat or forms; ask only for the region and credential location.** Follow the cloud guide's private-loading instructions. Never display credential file contents or environment values in tool output.

```sh
umask 077
state="${XDG_STATE_HOME:-$HOME/.local/state}/gree-firmware-downgrader"
mkdir -p "$state"
run=$(mktemp -d "$state/run.XXXXXX")
python3 -m venv "$run/venv"
"$run/venv/bin/python" -m pip install 'greeclimate==4.2.5' 'greeclimate-davo22==2.7.5'
```

Run private scripts with that environment's Python. Keep device inventory, image hashes, approvals, send timestamps and verification observations in the private run directory, never in the plugin or a public repository. Preserve download attempts and original metadata/headers there only for an explicitly approved bundle refresh or recovery. Never store credentials or raw encrypted/decrypted packets; disable `greeclimate` and every loaded child logger before LAN binding.

**Creating or reading this skill is not authorization to flash.** Discovery and read-only inventory may prepare a plan. For either install route, show the per-device canonical MAC, IP, current full HID, positively evidenced target full HID, bundled image path and SHA-256, original `source_url` provenance when known, device-facing `url`, and HTTP/region limitations. For cloud OTA, follow the exact terminal approval challenge in the cloud guide; previous conversational consent is not a substitute. Explain possible bricking, lost pairing/settings and loss of cloud/local access. Do not alter HVAC controls, Wi-Fi settings, calibration or power automatically. Require stable power and an uninterrupted maintenance window.

## 2. Discover and identify devices

After establishing the region and credential source, start with `gree_cloud.py inventory --region '<account region>'` as documented in [Cloud inventory and OTA](references/cloud-access.md). The output keeps physical modules, blocked logical children and failures. It can identify devices with no LAN response. Then use the existing client below as an optional LAN supplement or for direct-LAN upgrade; do not build a new UDP discovery implementation.

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

## 3. Select the exact bundled LAN target

For each identified physical module, match its firmware code, module and build evidence to one exact entry in `firmware/manifest.json` and the version reference. Use only that entry's unchanged original image. Do not substitute by version label, prefix, family, MID or cloud advertisement; if evidence does not resolve one exact build, report `blocked_ambiguous_build`.

- Already running the exact selected LAN build: `already_target`; verify a fresh LAN bind/status roundtrip and do not reflash.
- `362001068411` (HR13) and `362001071519` (AR11) have no established LAN target: `blocked_no_lan_target`. Do not invent or substitute an image.
- The `362001067540` WR13 target is **2.06**, even though the observed cloud-only version is 2.04.
- Use the manifest's exact `target_hid` only when positively evidenced. A null/missing full target HID or unresolved build provenance is `blocked_identity`; never manufacture an HID from labels or a version number. In particular, the `362001068295` UR 2.02 bundle has `target_hid: null`: it may be integrity-checked and hosted if separately approved, but do not approve or send an install until positive evidence establishes its exact emitted HID.

Do not change `AutoUpdate` automatically. State that vendor/app automatic updates may later replace the restored LAN firmware; neither its default nor its prevention is established here.

## 4. Verify the bundled original and serve the approved image

Normal runs use the exact unchanged original named by the matched manifest entry; they make no firmware metadata or image requests. Resolve `image` relative to the skill directory as `firmware/<image>`, and use the manifest's `size`, `sha256`, `source_url` and evidenced `target_hid` in the plan. Before hosting or approval, compare the bundled file's byte size and SHA-256 with the manifest. A missing file, mismatch or unresolved entry is `blocked_bundle_integrity`; do not download, overwrite or silently choose another version. Use a private wrapper to read manifest fields if needed.

For an explicitly approved LAN host, set `image` to the absolute bundled file path, `sha256` to its manifest hash, `source_url` to the exact official Gree image URL recorded in the matching manifest entry, and `url` to `http://<approved-LAN-IPv4>/firmware.bin`. `source_url` is acquisition provenance, not the device-facing LAN origin; `provenance_url` is the separate evidence reference. Serve only through the existing helper:

```sh
"$run/venv/bin/python" "$skill_dir/scripts/firmware_http.py" serve \
  --image "$image" --sha256 "$sha256" --bind "$lan_ip"
```

It serves only `/firmware.bin` with `Content-Length` and `FileMd5` on port 80. Keep it running through verification and any unresolved install. Preflight the LAN URL before approval:

```sh
"$run/venv/bin/python" "$skill_dir/scripts/firmware_http.py" preflight \
  --url "$url" --image "$image" --sha256 "$sha256" --allow-lan
```

Do not copy the firmware folder or run metadata into a server directory, use a generic HTTP server, bind publicly, or change firewalls. If port 80 needs privileges or routing blocks the URL, report `blocked_url`; do not use automatic sudo, alter firewall/routing, or silently fall back to the official URL. Cloud install uses `--allow-lan-url`. A successful host-side preflight does not prove device reachability.

For the optional direct-LAN route, URL limits below are conservative observations for CS532AE, not universal rules for Gree cloud OTA or all device families:

- Plain ASCII `http://host/path`, at most 159 bytes; IPv4 or DNS resolving to IPv4; no credentials, explicit port, fragment, controls or whitespace. Port 80 only, with no HTTPS, authentication or redirect dependency.
- Direct 200, positive `Content-Length`, uncompressed and unchunked bytes, small headers, and compatibility with an IP-valued `Host:` header. The inspected CS532AE parser may be fragile under TCP fragmentation.

## 5. Request approved installation, one device at a time

Finish exact target selection, bundled-file integrity checks, approved LAN hosting, URL preflight and per-device approval first.

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
