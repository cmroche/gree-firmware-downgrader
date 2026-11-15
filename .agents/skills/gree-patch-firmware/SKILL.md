---
name: gree-patch-firmware
description: Use when the user asks to patch a Gree device firmware to re-enable local LAN access to the device, and/or to disable connectivity to Gree cloud and other remote telemetry services. DO NOT use this skill for updating firmware to a device, including downgrades, or installing a local-only firmware image on a device.
---

# Gree Patch Firmware

## Context

Each Gree image has a firmware code, module and version. For example, `362001066300+U-4MWB05DF13V3.27-eu.bin` names code `362001066300`, module `U-4MWB05DF13` and version `3.27`. The embedded HID can differ from the download filename; verify both.

Gree firmware may support UDP/7000 LAN access and custom TCP or MQTT cloud access. These transports can share packet, crypto and appliance handlers. Some builds retain those handlers but no working LAN transport; a port literal or cloud parser is not proof of LAN support.

## Goal
Restore complete local access and disable Gree cloud and remote telemetry for the exact image requested. Work offline; patching does not authorize hosting, uploading, flashing or physical device commands.

## Gate

1. Identify the exact firmware code, module and version. If the target is unknown, STOP and ask for it.
2. Locate the original in `firmware/`, or use the `gree-firmware-download` skill. If unavailable, STOP and ask for its location. Record provenance, filename, embedded HID, size and SHA-256; preserve the original.
3. Identify the chipset, instruction set, load addresses, container/segment layout and integrity fields. Use same-SDK images only as analysis references, never as evidence of cross-flash compatibility. Do not reuse offsets across builds.

# Patching

Use container inspection (for example, binwalk) and native disassembly/decompilation (for example, Ghidra) as needed. Confirm critical callsites, address mappings, instruction widths and ABI against the exact source bytes; decompiled C is not a recovered buildable project.

Read `reference/lan-v2.md` for packet-flow guidance. It describes an ESP32 replacement's CipherV2 contract, not every vendor build: do not transplant its fixed HID, ESP32 APIs, storage policy or GCM-only restrictions. Establish the target's native cipher selectors, keys, framing and semantics from its own handlers.

## Workflow Overview

1. Search for LAN access handlers
2. Search for cloud access handlers
3. Identify supported commands and properties
4. Enable LAN access
5. Disable cloud access

## 1. Search for LAN access handlers

Trace startup through socket creation, bind, receive, parsing, decryption, dispatch and reply to the actual source IP/port. Locate discovery (including pre-table `scan` branches), bind/key handling, status, commands and HID responses. Reuse native handlers and the UART/property-cache backend; distinguish a dormant path from a missing transport.

## 2. Search for cloud access handlers

Trace cloud task startup, DNS/connect, TCP/MQTT login, incoming messages, reports, heartbeat/reconnect and independent UDP telemetry workers. Check cloud-state dependencies in Wi-Fi setup, credential persistence, reset, timers, watchdogs and UART/cache updates. Classify NTP, HTTP OTA, provisioning and diagnostics separately; strings and port numbers alone do not prove reachability.

## 3. Identify supported commands and properties

Map registered requests and property handlers, crypto/key selection and reply routing. Establish which handlers are safe to expose over LAN; do not expose plaintext cloud provisioning such as `wlan` merely because the shared parser accepts it. A property name or registered handler does not prove appliance support or physical command completion.

## 4. Enable LAN access

Prefer restoring the existing LAN path. If transport is absent, add the smallest UDP receive/reply adapter around proven native parsing, crypto and business handlers. Preserve initialization order, HID construction, allocator callbacks, native metadata, buffer bounds, stack/ABI constraints and per-iteration scheduler yield, including malformed-input paths.

Accept only the intended LAN envelopes (`scan`/`pack` where native), preserve authentication failures, reject truncated/oversized packets and reply to each datagram's actual peer. Trace signed crypto/error returns through callers and narrowing conversions so failures cannot become success. Recover socket/allocation failures without replaying commands. Establish local operation before removing its cloud dependencies; do not fake cloud-connected status.

Always retain the local firmware update/revert command (`upgrade` in the native protocol), its authenticated LAN dispatch, URL scheduler and updater. Cloud removal must not disable or stub this path; user-requested HTTP downloads are not automatic cloud traffic.

## 5. Disable cloud access

Bypass the proven cloud dispatcher/startup calls and independent telemetry workers, not the entire Wi-Fi task. If replacing a cloud task, retain its required local initialization, scheduler and hardware services. Verify first-time/reset provisioning, credential commit and reconnect without cloud; if unresolved, report that limit rather than claiming complete offline operation.

List retained outbound paths explicitly, including user-requested HTTP firmware downloads. Never disable local update/revert to enforce “local-only”; do not blacklist properties by name or call the result a WAN firewall while NTP, requested HTTP or diagnostics remain reachable.

# Validation

## Validation Overview

1. Verify image integrity and reproducibility
2. Exercise native LAN and lifecycle behavior
3. Independently reproduce the build and validation
4. Exercise a real client through loopback UDP

## 1. Verify image integrity and reproducibility

Create a reproducible builder that pins the source SHA-256 and expected bytes at each patch site, checks patch capacity/alignment, writes a separate candidate and refuses conflicting existing output. Recompute the exact container checksum/digest (for example, wrapper MD5 or OTA1 additive sum); do not alter integrity/signature verifiers.

Independently verify candidate hash, size, segment headers, identity, all changed-byte ranges and repaired integrity fields. Preserve unrelated bytes, keys, calibration and boot records. Record source/candidate hashes, offsets, old/new bytes, toolchain and build commands in a patch manifest.

Separate package checks from boot/OTP signatures and anti-rollback. Inspect every included executable boot stage/overlay with verified address mappings before declaring the remaining policy inaccessible. Where feasible, compare stock/candidate startup probes and record the first unsupported boundary; application-entry probes are not BootROM resets or successful boots.

Where feasible, execute the complete stock updater through HTTP parsing, download, flash readback/integrity and slot finalization—not just its verifier. Use stock and candidate success controls plus corrupt/stale-checksum and truncated-transfer rejection cases; verify cleanup and preservation of the valid old slot on failure. Extract the programmed payload, compare it byte-for-byte with the candidate and rerun native LAN scenarios on those extracted bytes. Record any reconstructed transport wrapper and separately seeded post-update context; neither is proof of reboot continuity.

Exercise fragmented headers/body, short initial reads and retry cleanup. Distinguish package-body checksums from HTTP metadata such as `FileMd5`; document receiver assumptions for installer handoff, without initiating a live update. Passing a checksum or modeled slot activation does not establish boot acceptance; never bypass authentication or infer its absence from an unsuccessful search.

## 2. Exercise native LAN and lifecycle behavior

Run bounded native execution/emulation from the patched entry through the retained parser, crypto, handlers and reply path. Independently decode replies. Cover scan → bind → keyed status → command → changed cached status, exact HID/MAC and distinct peers; cache changes are not physical HVAC proof.

Run the same frozen native regression vectors against stock and candidate without regenerating expectations, covering retained UART/cache, timers and sensor encoding where available. Hash-pin decoder inputs/exports and verify decoded bytes against the corresponding image. Use positive controls: prove stock reaches the cloud dispatcher under the same conditions in which the candidate bypasses it, while required local work and scheduler yield continue.

Include malformed/truncated/oversized input, bad authentication/selector, plaintext provisioning rejection, allocation/socket failures and recovery without command replay. Use inputs that distinguish the intended safeguard from unrelated parser rejection, and execute successful subsequent requests to prove recovery. Check allocator pairing, downstream metadata copies/aliasing, stack bounds, scheduler yield and cloud-worker removal.

Derive lifecycle fixtures from startup/reset paths, comparing fresh/unconfirmed and previously cloud-confirmed state. Exercise credential save/load and reconnect where possible; use a native confirmation control to isolate persistence dependencies. Direct parser execution or pre-seeded flags do not establish provisioning-service reachability or offline setup; record missing dependencies.

Obtain separate independent reviews of patch/ABI safety and validation claims, including modeled callback identities, synthetic initialization and whether hooks skip the behavior claimed. Resolve findings against native bytes or execution before finalizing results; static review is not independent test reproduction.

## 3. Independently reproduce the build and validation

Use a fresh isolated environment and temporary workspace, with pinned dependencies, source images, toolchain and validation inputs. Rebuild injected code and the candidate from the unchanged original; independently compare hashes, changed-byte ranges and integrity fields, then rerun the focused native, lifecycle and updater checks. Preserve original artifacts and frozen expectations. Record reproducible commands, environment versions, input hashes and actual results; do not call reuse of an existing environment a clean reproduction.

Have an independent reviewer inspect native bytes, emulator hooks and expected results for shared assumptions or bypassed behavior. A successful rerun with the same models is reproduction, not independent proof of those models; static review alone is not execution.

## 4. Exercise a real client through loopback UDP

Connect an actual Gree client over real loopback UDP to the native firmware emulator. Adapt only OS/socket boundaries: pass actual datagrams and peer endpoints into native parsing, crypto, dispatch and reply code, and return its actual wire bytes. Do not substitute a Python protocol implementation. Cover discovery, binding, default polling, commands and cached-state readback; independently decrypt/authenticate replies where applicable, suppress credential/key logging and close transports/tasks deterministically.

Always exercise candidate-side encrypted LAN upgrade and revert dispatch using test-only URLs and modeled task/flash boundaries; require the real URL scheduler to register the updater, not merely acknowledge the request. Where supported, execute the native updater and compare programmed bytes and slot state for candidate and original images; distinguish dispatch, modeled installation and reboot continuity. Direct stock-updater entry does not prove this candidate consumer path. Restrict runtime networking to loopback, never discover or command physical equipment, and record the client version/source, checks and every synthetic state or modeled boundary. Do not infer hardware, radio, UART completion or boot acceptance from client interoperability.

Report reproducible commands/results and every modeled ROM/SDK, crypto, flash, network and device-state boundary. Missing full boot, radio, persistent provisioning, UART acknowledgement, bootloader/eFuse policy or recovery evidence remains an explicit limitation, not a passing test. If execution tools are unavailable, label the candidate statically reviewed only.

Deliver the separate experimental image, builder, patch manifest and concise validation report, including retained WAN paths and unresolved risks. Preserve the original HID unless a justified identity change is required; an unchanged HID cannot prove installation. No flash approval follows from offline validation; hardware acceptance and recovery require a separately authorized procedure. Do not publish vendor binaries without established redistribution rights.

At the end of every firmware-patching task, include a user-facing table listing every patched address/range, its file offset, what changed and why. Include injected code/data and repaired checksum/digest fields; distinguish runtime addresses from file-only offsets. Show original → replacement bytes or instructions for short patches, and summarize larger payloads with a link to the complete byte-level manifest. Do not replace this final patch list with only an artifact link or validation summary.