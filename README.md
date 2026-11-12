# Gree Firmware Downgrader

An assistant skill that finds Gree HVAC devices and restores local network control using known LAN-capable firmware. For cloud-only devices it can inventory the Gree account and request an approved cloud OTA downgrade, then verifies the reported firmware and local status afterward.

[Firmware version reference](skills/gree-firmware-downgrader/references/firmware-versions.md)

[Cloud access and install guide](skills/gree-firmware-downgrader/references/cloud-access.md)

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

Start a new session after installing. Running the skill requires Python 3.10+, a private virtual environment with `greeclimate==4.2.5` and `greeclimate-davo22==2.7.5`, and access to the device's local network for verification.

## Use

Claude Code:

```text
/gree-firmware-downgrader:gree-firmware-downgrader Find my devices and show the downgrade plan.
```

Codex:

```text
$gree-firmware-downgrader Find my devices and show the downgrade plan.
```

Flashing can brick a device, lose pairing/settings or cloud access, and may make it unreachable. Devices with LAN access disabled require a working Gree cloud route and account for cloud inventory/install.

[GPL-3.0](LICENSE)
