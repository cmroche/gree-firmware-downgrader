# Cloud inventory and OTA install

Use this path when a physical module is absent from LAN discovery or cannot accept a LAN upgrade. The cloud command delivers the OTA request; it does not replace the final LAN bind/status verification. Normal runs use the exact bundled original selected from `firmware/manifest.json`; read the parent skill's target-selection, image-integrity, approval and recovery guidance first. Cloud access is not evidence that a firmware build is LAN-capable.

## Establish account access before login

Reuse an explicitly authorized account region and credential source when already supplied; ask only for missing details before any cloud login:

> Which region is selected in your Gree+ account, and where should I read your credentials: a private local file path or configured environment? You can instead enter them directly at the CLI's local terminal prompts. **Do not paste credentials into chat or forms; share only the region and credential location.**

Use the Gree+ account's actual region, not the user's physical location, firmware download region or this guide's Europe examples. Do not guess a region or try the password against multiple regions.

If the user names a directory, locate likely credential files there without opening unrelated files. Read only the authorized file privately into memory, never into chat or tool output; check private permissions (`0600` on POSIX or equivalent access controls). Do not print passwords, tokens, device keys or environment values, import secrets from another app, or copy credentials into source, logs or run evidence. Ask about an unreadable file or unclear format, not for secret values.

The CLI reads `GREE_USERNAME`/`GREE_PASSWORD` or prompts locally; it has no credential-file option. For an authorized file, load it in a private wrapper and populate only that process's environment before invoking the helper. Never embed values in displayed shell commands or arguments. Use terminal prompts only when the user can enter credentials directly without relaying them through chat.

## Private setup and read-only inventory

Set `skill_dir` to the installed `skills/gree-firmware-downgrader` directory. All script, image and run paths below are then anchored to that directory or the private run directory, not the shell's current working directory. Use Python 3.10+ and a private environment containing both clients:

```sh
skill_dir="/absolute/path/to/installed/skills/gree-firmware-downgrader"
state="${XDG_STATE_HOME:-$HOME/.local/state}/gree-firmware-downgrader"
umask 077
mkdir -p "$state"
run=$(mktemp -d "$state/run.XXXXXX")
python3 -m venv "$run/venv"
"$run/venv/bin/python" -m pip install 'greeclimate==4.2.5' 'greeclimate-davo22==2.7.5'
cloud="$skill_dir/scripts/gree_cloud.py"
firmware_http="$skill_dir/scripts/firmware_http.py"
```

For example, after confirming the account region, inventory Europe with `"$run/venv/bin/python" "$cloud" inventory --region 'Europe'`; substitute the user's confirmed region. The script prompts for username/password if `GREE_USERNAME` and `GREE_PASSWORD` are not set. It emits sanitized JSON with each physical module's canonical MAC, name, cloud protocol, freshly solicited HID and status; blocked logical children and per-device failures remain visible. Keep the output and device inventory private.

Inventory queries the cloud homes/devices endpoints directly, preserving physical-gateway records. Merge its output with LAN observations by canonical physical MAC. Use the physical 12-hex MAC as identity; a different authoritative `pmac` means the record is a logical child and must not be flashed. Cloud inventory can identify a module with no LAN response. A missing exact route or fresh HID is a named blocker, not permission to infer an alias or use stale inventory. Install refreshes inventory and solicits HID again; never use a cached cloud inventory version as HID. The cloud fork's `get_all_devices()` drops physical module records; do not use it for inventory.

The cloud client intentionally disables TLS certificate/hostname validation for Gree cloud REST, MQTT and TLS-gateway connections. This accepted exception applies only to those cloud connections and is disclosed during install approval; it does not apply to firmware acquisition, which must retain verified TLS as described in [firmware-download](firmware-download.md). Never make this a process-wide TLS setting. REST redirects are rejected, and a plaintext legacy gateway is refused even with this TLS exception.

## Prepare and approve one install

Select the exact manifest entry by physical-module firmware code, module and build evidence, as described in the parent skill and [firmware version reference](firmware-versions.md). Normal runs do not query firmware metadata or download an image. Resolve `image` relative to `skill_dir` as an absolute bundled path, then compare its byte size and SHA-256 with the entry's `size` and `sha256` before hosting or approval. A missing file, mismatch or unresolved build is a blocker; never download or swap versions automatically. A null/missing exact `target_hid` or unresolved provenance blocks flashing until positive evidence resolves it.

A plan JSON has exactly these fields:

```json
{
  "mac": "canonical 12-hex physical MAC",
  "current_hid": "fresh full current HID",
  "target_hid": "exact positively evidenced full target HID",
  "image": "/absolute/installed/skill/firmware/<manifest image>",
  "sha256": "64-hex SHA-256 from the manifest",
  "source_url": "official Gree image URL from manifest",
  "url": "http://<approved LAN IPv4>/firmware.bin"
}
```

`source_url` must exactly match the installed manifest entry's official Gree image URL, never the LAN origin. The manifest's `provenance_url` is the separate evidence reference. Do not invent either URL, a full target HID or a build association. With explicit approval, host only this verified bundled image using the existing `firmware_http.py` helper:

```sh
"$run/venv/bin/python" "$firmware_http" serve \
  --image "$image" --sha256 "$sha256" --bind "$lan_ip"
```

Keep the server running in a private terminal through verification and any unresolved install. Preflight the exact LAN URL:

```sh
"$run/venv/bin/python" "$firmware_http" preflight \
  --url "$url" --image "$image" --sha256 "$sha256" --allow-lan
```

The helper serves only `/firmware.bin` on port 80 with `Content-Length` and `FileMd5`. Do not copy the firmware directory or run metadata into a server directory, use a generic HTTP server, bind publicly, or change firewalls. If port 80 requires privileges or routing blocks the URL, report `blocked_url`; do not use automatic sudo, change firewall/routing, or silently fall back to an official URL. Preflight success does not prove device reachability.

Review MAC, old and exact target HIDs, image/hash, original `source_url`, LAN `url`, and the disclosed cloud TLS exception. The only install consent is typing `FLASH <canonicalmac> <sha256>` with those placeholders replaced by the device's canonical MAC and plan SHA-256. There is no `--yes` or reusable scope approval. Immediately after approval, the script fetches HID again; a mismatch stops before send. Never treat a valid OTA URL/install attempt as a safe dry run: it may flash immediately. Do offline protocol checks and read-only cloud status first.

Example invocation after preparing `$run/plan.json` from verified evidence:

```sh
"$run/venv/bin/python" "$cloud" install \
  --region 'Europe' --plan "$run/plan.json" --run-dir "$run" --allow-lan-url
```

Use `--allow-lan-url` only for the explicitly approved LAN-hosted image; do not use it to bypass URL validation. The script creates `install-<mac>.json` exclusively, mode 600, and fsyncs it before sending. Any existing ledger, including malformed or interrupted state, blocks another attempt. Preserve it; do not remove/edit it to retry. An ambiguous send is never automatically replayed.

The cloud protocol is selected from the cloud record's protocol version, not a numeric firmware version in the HID. Missing or unparseable protocol data is a blocker, never a guessed default. MQTT uses read-only V1/V2 negotiation for HID; the legacy TLS gateway uses ECB, and plaintext legacy gateway connections are refused. Gateway `QueryOnline` resolution is limited to public IP addresses. Do not call cloud HVAC-control methods. HID status replies must match the physical MAC in the outer `cid` and the exact request `tcid`; retained or unrelated MQTT replies are ignored, and GCM reply tags are verified. Send the OTA request once through the selected cloud transport, with no reconnect/resend; MQTT uses QoS 0 and non-retained delivery. A missing acknowledgement is ambiguous, not a reason to send again. A local image server changes only where the bytes are hosted: the OTA command still travels through Gree cloud.

## Approved LAN image hosting

The default URL is the explicitly approved LAN host serving the unchanged bundled image at `/firmware.bin`; do not prefer an official direct image URL or automatically fall back to it. The helper's `preflight` rechecks the file hash and complete bytes served at the URL. Use the manifest's absolute `image`, `sha256` and exact `source_url`, and set `url` to the LAN URL, as above.

The 159-ASCII-byte URL limit, HTTP port 80 and IP-valued `Host` compatibility are conservative observations for CS532AE, not universal Gree cloud-firmware rules. Do not assume every family supports HTTPS, particular headers, anti-rollback behavior or the same parser. A successful host-side GET proves neither device reachability nor device acceptance. The cloud TLS exception never changes the acquisition reference's verified-HTTPS and explicit HTTP-fallback policy.
The known real-cloud test with an emulated device establishes URL relay only; it does not prove a physical downgrade or general production transport compatibility. The production transport limits above still apply.

## Verify and recover

After dispatch, including an ambiguous exception, use the parent skill's fresh LAN rediscovery/bind/status procedure. Success requires the exact target full HID on two fresh observations at least five seconds apart, with actual LAN bind/status working. Cloud-online status after downgrade is not required. Do not infer completion from cloud acceptance, an acknowledgement, offline time or one HID. If the module does not return, report it unconfirmed; do not resend, switch images or stop an active image server before informing the user.

A downgrade can brick a module or lose pairing, settings, cloud service or local access; successful reported HID is not a flash readback or proof of physical operation. Use stable power and an uninterrupted maintenance window, and know the device's recovery path. Never change HVAC controls, power, Wi-Fi or automatic-update settings as part of this procedure. Offline protocol checks and read-only cloud status are not a live OTA test; a real install requires the account's actual region and credentials plus explicit approval of that exact device and image. Do not claim live cloud login or OTA validation unless actually performed.
