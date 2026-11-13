# Gree Firmware Downgrader

An assistant skill for restoring local control of [supported Gree Wi-Fi devices](skills/gree-firmware-downgrader/references/firmware-versions.md) using bundled, verified LAN-capable firmware.

## Install

**Claude Code**

```sh
claude plugin marketplace add cmroche/gree-firmware-downgrader
claude plugin install gree-firmware-downgrader@gree-firmware-downgrader
```

**Codex CLI**

```sh
codex plugin marketplace add cmroche/gree-firmware-downgrader
codex plugin add gree-firmware-downgrader@gree-firmware-downgrader
```

Start a new session after installing. Requires Python 3.10+ and access to the device's local network.

## Use

Claude Code:

```text
/gree-firmware-downgrader:gree-firmware-downgrader Find my devices and show the downgrade plan.
```

Codex:

```text
$gree-firmware-downgrader Find my devices and show the downgrade plan.
```

For cloud access, provide your Gree+ account region and a private credential location when asked. Never paste credentials into chat.

Flashing can brick a device or lose pairing, settings and cloud access. Review the plan and explicitly approve each device before flashing.

Project code: [GPL-3.0](LICENSE). Vendor firmware redistribution rights remain unverified.
