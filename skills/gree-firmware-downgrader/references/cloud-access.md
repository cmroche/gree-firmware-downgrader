# Cloud inventory and OTA install

Use this path when a physical module is absent from LAN discovery or cannot accept a LAN upgrade. The cloud command delivers the OTA request; it does not replace the final LAN bind/status verification. Read the parent skill's target-selection, original-image, approval and recovery guidance first. Cloud access is not evidence that a firmware build is LAN-capable.

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

Use the account's actual Gree region; it is required, not guessed. For example, inventory Europe with `"$run/venv/bin/python" "$cloud" inventory --region 'Europe'`. The script prompts for username/password if `GREE_USERNAME` and `GREE_PASSWORD` are not set. Prefer prompts; never put credentials in command arguments, source, logs or run evidence, and do not import secrets from another app. It emits sanitized JSON with each physical module's canonical MAC, name, cloud protocol, freshly solicited HID and status; blocked logical children and per-device failures remain visible. Keep the output and device inventory private.

Inventory queries the cloud homes/devices endpoints directly, preserving physical-gateway records. Merge its output with LAN observations by canonical physical MAC. Use the physical 12-hex MAC as identity; a different authoritative `pmac` means the record is a logical child and must not be flashed. Cloud inventory can identify a module with no LAN response. A missing exact route or fresh HID is a named blocker, not permission to infer an alias or use stale inventory. Install refreshes inventory and solicits HID again; never use a cached cloud inventory version as HID. The cloud fork's `get_all_devices()` drops physical module records; do not use it for inventory.

The cloud client intentionally disables TLS certificate/hostname validation for Gree cloud REST, MQTT and TLS-gateway connections. This accepted exception applies only to those cloud connections and is disclosed during install approval; it does not apply to firmware acquisition, which must retain verified TLS as described in [firmware-download](firmware-download.md). Never make this a process-wide TLS setting. REST redirects are rejected, and a plaintext legacy gateway is refused even with this TLS exception.

## Prepare and approve one install

Select only the exact positively verified LAN-capable target and exact unchanged Gree original from the parent skill's version and acquisition references. Do not substitute by numeric version, code prefix, module family or cloud advertisement. A plan JSON has exactly these fields:

```json
{
  "mac": "canonical 12-hex physical MAC",
  "current_hid": "fresh full current HID",
  "target_hid": "evidenced full target HID",
  "image": "/private/run/path/to/verified-original.bin",
  "sha256": "64-hex SHA-256 of original image",
  "source_url": "original official Gree provenance URL",
  "url": "direct HTTP firmware URL or explicitly approved LAN HTTP URL"
}
```

Use the original official URL when its direct HTTP response serves the same verified bytes. For a LAN-hosted image, `source_url` remains the original official Gree URL; `url` is the LAN server URL. The script verifies the local image hash and all served bytes, then refreshes cloud inventory and obtains a solicited fresh HID before presenting the full plan. Review MAC, old and target HIDs, image/hash, both URLs, and the disclosed cloud TLS exception. The only install consent is typing `FLASH <canonicalmac> <sha256>` with those placeholders replaced by the device's canonical MAC and plan SHA-256. There is no `--yes` or reusable scope approval. Immediately after approval, the script fetches HID again; a mismatch stops before send. Never treat a valid OTA URL/install attempt as a safe dry run: it may flash immediately. Do offline protocol checks and read-only cloud status first.

Example invocation after preparing `$run/plan.json` from verified evidence:

```sh
"$run/venv/bin/python" "$cloud" install \
  --region 'Europe' --plan "$run/plan.json" --run-dir "$run"
```

For an approved LAN-hosted URL, add `--allow-lan-url`. Do not pass this option for vendor URLs or use it to bypass URL validation. The script creates `install-<mac>.json` exclusively, mode 600, and fsyncs it before sending. Any existing ledger, including malformed or interrupted state, blocks another attempt. Preserve it; do not remove/edit it to retry. An ambiguous send is never automatically replayed.

The cloud protocol is selected from the cloud record's protocol version, not a numeric firmware version in the HID. Missing or unparseable protocol data is a blocker, never a guessed default. MQTT uses read-only V1/V2 negotiation for HID; the legacy TLS gateway uses ECB, and plaintext legacy gateway connections are refused. Gateway `QueryOnline` resolution is limited to public IP addresses. Do not call cloud HVAC-control methods. HID status replies must match the physical MAC in the outer `cid` and the exact request `tcid`; retained or unrelated MQTT replies are ignored, and GCM reply tags are verified. Send the OTA request once through the selected cloud transport, with no reconnect/resend; MQTT uses QoS 0 and non-retained delivery. A missing acknowledgement is ambiguous, not a reason to send again. A local image server changes only where the bytes are hosted: the OTA command still travels through Gree cloud.

## Image URL and optional LAN hosting

Prefer the official direct HTTP firmware URL. The helper's `preflight` rechecks the image hash and the complete bytes served at the URL before install. Example for an official URL:

```sh
"$run/venv/bin/python" "$firmware_http" preflight \
  --url "$official_url" --image "$image" --sha256 "$sha256"
```

If needed and explicitly approved, serve only the verified original at an approved LAN IPv4 address on port 80:

```sh
"$run/venv/bin/python" "$firmware_http" serve \
  --image "$image" --sha256 "$sha256" --bind "$lan_ip"
```

This serves only `/firmware.bin` with `Content-Length` and `FileMd5`. Keep it running through verification and any unresolved install. Preflight that URL with `--allow-lan`; do not expose run metadata, bind publicly, change firewall rules, or use an unapproved private address. The old generic `python -m http.server` recipe is not sufficient.

The 159-ASCII-byte URL limit, HTTP port 80 and IP-valued `Host` compatibility are conservative observations for CS532AE, not universal Gree cloud-firmware rules. Do not assume every family supports HTTPS, particular headers, anti-rollback behavior or the same parser. A successful host-side GET proves neither device reachability nor device acceptance. The cloud TLS exception never changes the acquisition reference's verified-HTTPS and explicit HTTP-fallback policy.

## Verify and recover

After dispatch, including an ambiguous exception, use the parent skill's fresh LAN rediscovery/bind/status procedure. Success requires the exact target full HID on two fresh observations at least five seconds apart, with actual LAN bind/status working. Cloud-online status after downgrade is not required. Do not infer completion from cloud acceptance, an acknowledgement, offline time or one HID. If the module does not return, report it unconfirmed; do not resend, switch images or stop an active image server before informing the user.

A downgrade can brick a module or lose pairing, settings, cloud service or local access; successful reported HID is not a flash readback or proof of physical operation. Use stable power and an uninterrupted maintenance window, and know the device's recovery path. Never change HVAC controls, power, Wi-Fi or automatic-update settings as part of this procedure. Offline protocol checks and read-only cloud status are not a live OTA test; a real install requires the account's actual region and credentials plus explicit approval of that exact device and image. Do not claim live cloud login or OTA validation unless actually performed.
