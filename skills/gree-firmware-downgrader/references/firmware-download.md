# Firmware acquisition for explicitly approved bundle refresh or recovery

This reference applies only when the user explicitly approves acquiring or replacing a specific bundled original for refresh or recovery. Normal runs use the exact bundled original; a missing, corrupt or mismatched bundle never triggers a download or version swap. This reference does not authorize flashing. Adapted from [greeclimate's firmware-download skill](https://github.com/cmroche/greeclimate/blob/91e08befc9209cff53be4c3a98a1a7047802afe1/.agents/skills/gree-firmware-download/SKILL.md), copyright Clifford Roche, GPL-3.0. Keep TLS certificate and hostname verification enabled for every acquisition request. The cloud guide's accepted TLS-validation exception for Gree cloud REST/MQTT/gateway connections does not apply here.

The parent skill's explicitly approved LAN image host is a device-facing firmware URL, not an acquisition source. It does not permit private/LAN URLs for metadata or image acquisition and does not relax the checks below.

## Identify the exact target for an approved refresh or recovery

- Take the firmware code and full target HID from the bundled version reference and original build evidence. Preserve the literal code, module/variant, version and regional association. A HID prefix is a lookup candidate, not proof of an OTA alias; a MID or retail model is not a firmware code.
- The newest advertised firmware is not necessarily LAN-capable. Select only the explicit verified LAN build; matching a numeric version does not resolve distinct builds sharing that label.
- Keep API `ver`, HID/module version and `commProtVer` separate. Never fabricate a full target HID from an ambiguous API version or compare version labels across families.

## Safety and pacing

Use existing HTTP/file tools rather than adding a downloader framework.

- Start at most one request per second globally across workers, hosts, metadata, archive lookups, downloads, retries, HTTPS/HTTP alternatives and redirect hops. Use monotonic next-start deadlines with no catch-up bursts; honor an explicit user throttle instead.
- Disable automatic redirects and retries. Inspect each redirect, validate its destination and resolved addresses, and pace it as a new request. Allow at most ten redirect hops.
- Accept only HTTP(S), without embedded credentials. Reject loopback, private, link-local, multicast, reserved or unspecified addresses, including DNS results and redirect destinations. Do not execute metadata, interpolate it into shell code, or turn server filenames into local paths. Investigate unfamiliar hosts before fetching them.
- Prefer verified HTTPS. If a public Gree host has a certificate failure but an explicit public HTTP endpoint works, disclose unauthenticated transport and record the fallback. Never use `--insecure`, disable TLS verification or send credentials for firmware acquisition. The cloud-connection exception is separate and narrowly scoped; do not carry it into this workflow.
- On 429, pause globally for `Retry-After` (seconds or HTTP date). Also pause after three transient 5xx/reset/timeout failures in five requests. Use exponential backoff `min(60, 2**failure_streak)` seconds, or a longer server cooldown. Preserve cooldowns across resume; reset the streak only after a successful response.
- Bound each lookup/transfer to five total attempts. Stop the acquisition run after five consecutive transient failures, leaving named unresolved work rather than hammering another host. An application `r:405` not-found is not a transient failure.
- Preserve each attempt before advancing: run ID, exact code/host/URL, UTC start/end, HTTP status, app result, raw metadata/header paths, errors, redirects, cooldown and outcome. Keep interrupted and failed attempts; resume unresolved entries, not merely the last successful code. Store raw evidence privately and redact signed query tokens from shared output.

## Regional and historical lookup

For the known code, independently query each production host:

- global: `grih.gree.com`
- Europe: `eu.grih.gree.com`
- North America: `na.grih.gree.com`
- Russia: `ru.grih.gree.com`
- Hong Kong: `hk.grih.gree.com`
- India: `in.grih.gree.com`
- South America: `sa.grih.gree.com`

Also check `test.grih.gree.com`, `tmp.grih.gree.com` and `kfgrih.gree.com` as auxiliary endpoints, not production regions. Keep queried host and returned download host separate. Use the evidenced endpoint:

```text
https://<host>/wifiModule/Lastversion?firmwareCode=<code>
```

Read the body, not just HTTP status:

- App `r:200` with nonempty `ver` and HTTP(S) `url`: an advertised candidate, not a verified download. Preserve `CreateDate`, `desc`, `forcedUpgrade` and `commProtVer` when present.
- App `r:405`, `msg:"Firmware not found."`: no candidate advertised by this host at this time; historical availability remains unknown.
- HTTP 404/405, DNS/TLS failure, timeout, non-JSON or other errors: lookup failed, availability unknown. Do not label it absent.

An older LAN build may still be latest in another region. If no latest response matches, inspect the [firmware archive](https://github.com/maxim-smirnov/gree-wifimodule-firmware), its matching `.md` sidecars/history, and the version reference's linked evidence to recover the exact original official Gree image URL. Image IDs are host-specific: never transplant IDs between regions or invent `version=` parameters. Use archive metadata to locate Gree's original; do not install a mirror payload. If Gree no longer serves the exact verified build, report `blocked_download` with the checked sources; another origin requires explicit scope approval.

## Download only for an explicitly approved refresh or recovery

After inspecting and validating a candidate URL, download one hop into a fresh private attempt directory. Use HTTP tooling with explicit connect and total timeouts; save response headers and incomplete bytes separately. A failed/interrupted `.part` file is not verified firmware. Try a verified-HTTPS counterpart of an advertised HTTP URL first; record both advertised and effective URLs and any HTTP fallback.

Before promoting the file:

1. Require a complete final 2xx transfer. Reject HTML/JSON error bodies, login pages and unrelated content; a `.bin` extension or octet-stream content type alone proves nothing.
2. Compare final `Content-Length` with the saved byte count when present. Compare final `FileMd5` with the whole file's computed MD5 when present. Record missing checksums, never invent them.
3. Compute SHA-256 and compare known exact-build hashes when available. Preserve complete containers and every distinct build. MD5, SHA-256 and HTTP headers are not digital signatures or proof of safe flashing.
4. Establish the selected code/module/region/version and expected full HID from original provenance and embedded identity where available. Resolve conflicts or duplicate-label builds before installation; do not relabel another payload.
5. Preserve the original immutable bytes plus raw metadata, final headers, source/effective URL, source filename as evidence only, size, SHA-256 and verification result. Use locally chosen safe basenames, not a server-controlled filename. Never overwrite a prior original blindly.

For each explicitly approved refresh/recovery target, finish with a verified local original or a named blocker. Querying all regions does not require downloading unrelated cloud-only/newer releases. Identical verified hashes may share one local file, but retain every selected device's provenance association.

After approved acquisition, return the verified original, exact target HID, hash, actual acquisition URL and separate provenance reference to the parent skill's bundle-integrity, URL, approval, installation and fresh-verification steps. Normal runs never reach this acquisition workflow.
