# Gree Firmware Downgrader

An assistant skill that finds Gree HVAC devices and restores local network control using known LAN-capable firmware. It downloads firmware from Gree, asks before installing, and checks the reported firmware afterward.

[Firmware version reference](skills/gree-firmware-downgrader/references/firmware-versions.md)

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

Start a new session after installing. Running the skill requires Python 3.10+ and access to your devices' local network.

## Use

Claude Code:

```text
/gree-firmware-downgrader:gree-firmware-downgrader Find my devices and show the downgrade plan.
```

Codex:

```text
$gree-firmware-downgrader Find my devices and show the downgrade plan.
```

Flashing can brick a device. Devices with LAN access disabled may be unreachable.

[GPL-3.0](LICENSE)
