#!/usr/bin/env python3
"""Select bundled firmware separately for each Gree physical module."""

import argparse
import asyncio
from contextlib import AsyncExitStack
import getpass
import hashlib
from ipaddress import IPv4Address
import json
import logging
import os
from pathlib import Path
import re
import socket
import tempfile
from time import monotonic

from greeclimate.device import Device
from greeclimate.discovery import Discovery
from greeclimate.network import Response

import firmware_http
import gree_cloud as cloud

ROOT = Path(__file__).resolve().parent
for _name in ("greeclimate", *tuple(logging.Logger.manager.loggerDict)):
    if _name == "greeclimate" or _name.startswith("greeclimate."):
        logging.getLogger(_name).disabled = True


def load_catalog(directory):
    directory = Path(directory).resolve()
    entries = json.loads((directory / "manifest.json").read_text())["images"]
    images = []
    for entry in entries:
        code, version, filename = entry["firmware_code"], entry["version"], entry["image"]
        if not re.fullmatch(r"[0-9]{12}", code) or filename != f"{code}-v{version}.bin":
            raise firmware_http.FirmwareError("Invalid bundled firmware filename/code")
        path = (directory / filename).resolve()
        if not path.is_relative_to(directory):
            raise firmware_http.FirmwareError("Firmware path escapes the bundle")
        target = entry["target_hid"]
        if target is not None and (not cloud.valid_hid(target) or target.split("+", 1)[0] != code):
            raise firmware_http.FirmwareError("Invalid bundled target HID")
        info = firmware_http.verify_image(path, entry["sha256"])
        if info["length"] != entry["size"]:
            raise firmware_http.FirmwareError(f"Bundled image size changed: {filename}")
        images.append({**entry, "image": str(path), "local_only": False})
    for path in sorted(directory.glob("*-local-only.bin")):
        match = re.fullmatch(r"([0-9]{12})-v([0-9]+(?:\.[0-9]+)+)-local-only\.bin", path.name)
        if not match:
            raise firmware_http.FirmwareError(f"Invalid local-only filename: {path.name}")
        if not path.resolve().is_relative_to(directory) or not path.is_file():
            raise firmware_http.FirmwareError("Local-only image is not a bundled regular file")
        with path.open("rb") as stream:
            sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
        info = firmware_http.verify_image(path, sha256)
        images.append({"firmware_code": match[1], "version": match[2], "image": str(path.resolve()), "size": info["length"], "sha256": sha256, "target_hid": None, "local_only": True})
    return images


def compatible_images(hid, images):
    if not cloud.valid_hid(hid):
        return []
    code = hid.split("+", 1)[0]
    return sorted(
        (image for image in images if image["firmware_code"] == code and (
            image["local_only"] or (image["target_hid"] is not None and "V" in hid
                and hid.rsplit("V", 1)[0] == image["target_hid"].rsplit("V", 1)[0])
        )),
        key=lambda image: (not image["local_only"], image["version"]),
    )


async def scan(targets=None):
    discovery = Discovery()
    try:
        return await discovery.scan(wait_for=3, target_addresses=targets)
    finally:
        discovery.close()


async def fresh_hid(device):
    future = asyncio.get_running_loop().create_future()
    def received(data):
        hid = data.get("hid")
        if not future.done() and cloud.valid_hid(hid):
            future.set_result(hid)
    device.add_handler(Response.DATA, received)
    try:
        await device.send(device.create_status_message(device.device_info, "hid"))
        return await asyncio.wait_for(future, timeout=5)
    finally:
        device.remove_handler(Response.DATA, received)


async def observe(info):
    device = Device(info, bind_timeout=3)
    try:
        await asyncio.wait_for(device.bind(), timeout=8)
        return await fresh_hid(device)
    finally:
        device.close()


async def rediscover(mac, ip=None):
    for targets in ([IPv4Address(ip)], None) if ip else (None,):
        matches = []
        for info in await scan(targets):
            try:
                if cloud.canonical_mac(info.mac) == mac:
                    matches.append(info)
            except cloud.Blocked:
                continue
        if len(matches) > 1:
            raise cloud.Blocked("Conflicting LAN endpoints for the selected MAC")
        if matches:
            return matches[0]
    raise cloud.Blocked("Selected physical module did not answer fresh LAN discovery")


async def lan_inventory():
    rows, seen = [], set()
    for info in await scan():
        row = {"mac": info.mac, "name": info.name, "ip": str(info.ip), "port": info.port}
        try:
            mac = cloud.canonical_mac(info.mac)
            if mac in seen:
                continue
            seen.add(mac)
            row["mac"] = mac
            row.update(hid=await observe(info), status="identified", route="lan")
        except cloud.Blocked as error:
            row.update(status="blocked_identity", reason=str(error))
        except Exception as error:
            row.update(status="blocked_lan", reason=type(error).__name__)
        rows.append(row)
    return rows


def merge_inventory(lan_rows, cloud_rows):
    rows = {row["mac"]: dict(row) for row in lan_rows}
    for remote in cloud_rows:
        remote = dict(remote)
        try:
            remote["mac"] = cloud.canonical_mac(remote["mac"])
        except cloud.Blocked:
            pass
        if remote.get("status") == "identified":
            remote["route"] = "cloud"
        local = rows.get(remote["mac"])
        if local is None or local.get("status") != "identified":
            if local is not None:
                remote = {**local, **remote}
            rows[remote["mac"]] = remote
        elif remote.get("status") == "identified" and local["hid"] != remote["hid"]:
            local.update(status="blocked_identity", reason="Fresh LAN and cloud HIDs disagree")
        elif remote.get("name"):
            local["name"] = remote["name"]
    return list(rows.values())


def hosting_address(bind, devices):
    if bind:
        return str(IPv4Address(bind))
    local = next((row for row in devices if row.get("route") == "lan"), None)
    if local:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
            route.connect((local["ip"], local["port"]))
            return route.getsockname()[0]
    return str(IPv4Address(input("LAN IPv4 of this computer for firmware hosting: ").strip()))


def select_firmware(devices, images, bind):
    plans, results = [], []
    print("\nFlashing can brick a module or lose pairing/settings and network access.")
    print("Keep power stable. Selecting firmware authorizes ONE installation on that device.")
    for device in devices:
        mac = device["mac"]
        row = {"mac": mac, "name": device.get("name", ""), "before_hid": device.get("hid"), "outcome": "kept_current"}
        results.append(row)
        print(f"\n{device.get('name', '')!r}  MAC {mac!r}")
        if device.get("status") != "identified":
            row["outcome"] = device["status"]
            print(f"  Not selectable: {device.get('reason', device.get('error', device['status']))!r}")
            continue
        print(f"  Current: {device['hid']}  ({device['route']}, {device.get('ip', 'cloud')})")
        options = compatible_images(device["hid"], images)
        if not options:
            row["outcome"] = "blocked_no_firmware"
            print("  No compatible bundled firmware; keeping current firmware.")
            continue
        print("  0. Keep current firmware")
        for number, image in enumerate(options, 1):
            kind = "local-only" if image["local_only"] else "vendor"
            current = " (already current)" if not image["local_only"] and image["target_hid"] == device["hid"] else ""
            print(f"  {number}. {image['version']} {kind}{current} — {Path(image['image']).name}")
            print(f"     SHA-256: {image['sha256']}")
            print(f"     URL: http://{bind}/firmware/{image['sha256']}.bin")
        while True:
            answer = input("  Firmware to INSTALL [0]: ").strip() or "0"
            if answer.isdecimal() and 0 <= int(answer) <= len(options):
                break
            print("  Enter one of the displayed numbers.")
        if int(answer) == 0:
            continue
        image = options[int(answer) - 1]
        row["image"] = Path(image["image"]).name
        if not image["local_only"] and image["target_hid"] == device["hid"]:
            row["outcome"] = "already_target"
            continue
        plan = {"mac": mac, "current_hid": device["hid"], "target_hid": image["target_hid"], "image": image["image"], "sha256": image["sha256"], "size": image["size"], "local_only": image["local_only"], "url": f"http://{bind}/firmware/{image['sha256']}.bin", "route": device["route"], "ip": device.get("ip")}
        plans.append(plan)
        row["outcome"] = "pending"
    return plans, results


async def dispatch_lan(plan, run_dir):
    ledger = cloud.ledger_path(run_dir, plan["mac"])
    await asyncio.to_thread(firmware_http.preflight, plan["url"], plan["image"], plan["sha256"], allow_lan=True)
    info = await rediscover(plan["mac"], plan.get("ip"))
    device = Device(info, bind_timeout=3)
    try:
        await asyncio.wait_for(device.bind(), timeout=8)
        if await fresh_hid(device) != plan["current_hid"]:
            raise cloud.Blocked("Firmware changed after selection; no upgrade sent")
        cloud.record_dispatch(ledger, plan, "lan")
        try:
            await device.send({"cid": "app", "i": 0, "t": "pack", "uid": 0, "tcid": plan["mac"], "pack": {"t": "upgrade", "mac": plan["mac"], "url": plan["url"]}})
        except Exception as error:
            return {"outcome": "install_unconfirmed", "error": type(error).__name__, "ledger": str(ledger), "ip": str(info.ip)}
        return {"outcome": "install_requested", "ledger": str(ledger), "ip": str(info.ip)}
    finally:
        device.close()


async def verify_install(plan, server, started):
    deadline = monotonic() + 300
    first_match = None
    last = {"outcome": "install_unconfirmed", "after_hid": None}
    ip = plan.get("ip")
    route = f"/firmware/{plan['sha256']}.bin"
    while monotonic() < deadline:
        try:
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            info = await asyncio.wait_for(rediscover(plan["mac"], ip), timeout=min(8, remaining))
            ip = str(info.ip)
            hid = await asyncio.wait_for(observe(info), timeout=min(13, max(0, deadline - monotonic())))
            last.update(after_hid=hid, ip=ip)
            if hid.split("+", 1)[0] != plan["current_hid"].split("+", 1)[0]:
                return {**last, "outcome": "install_mismatch"}
            if not plan["local_only"] and hid not in (plan["current_hid"], plan["target_hid"]):
                return {**last, "outcome": "install_mismatch"}
            downloaded = server.completed_downloads.get((ip, route), -1) >= started
            matches = downloaded if plan["local_only"] else hid == plan["target_hid"]
            if matches:
                if first_match is None:
                    first_match = monotonic()
                elif monotonic() - first_match >= 5:
                    outcome = "lan_available" if plan["local_only"] else "verified_reported_hid"
                    return {**last, "outcome": outcome, "download_served": downloaded}
            else:
                first_match = None
        except Exception as error:
            last["error"] = type(error).__name__
            first_match = None
        remaining = deadline - monotonic()
        if remaining > 0:
            await asyncio.sleep(min(5, remaining))
    return last


def report(results):
    print("\nResults:")
    for row in results:
        print(f"  {row['mac']!r} {row.get('name', '')!r}: {row['outcome']} {row.get('image', '')}")
    if any(row["outcome"] == "lan_available" for row in results):
        print("  LAN returned after serving the local image; unchanged HID does not prove installed flash bytes.")


async def run(args):
    images = load_catalog(ROOT / "firmware")
    async with AsyncExitStack() as resources:
        api = None
        if args.region:
            print(cloud.TLS_WARNING)
            username = os.environ.get("GREE_USERNAME") or input("Gree+ username: ")
            password = os.environ.get("GREE_PASSWORD") or getpass.getpass("Gree+ password: ")
            if not username or not password:
                raise cloud.Blocked("Gree+ username and password are required")
            api = await resources.enter_async_context(cloud.login(args.region, username, password))
        print("Discovering LAN devices…")
        devices = await lan_inventory()
        if api is not None:
            devices = merge_inventory(devices, await cloud.inventory(api, args.region))
        if not devices:
            print("No devices replied. A silent LAN scan does not prove absence; use --region for cloud inventory.")
            return 0
        selectable = any(row.get("status") == "identified" and compatible_images(row["hid"], images) for row in devices)
        bind = hosting_address(args.bind, devices) if selectable else None
        plans, results = select_firmware(devices, images, bind)
        if not plans:
            report(results)
            return 0
        state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "gree-firmware-downgrader"
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=state))
        (run_dir / "selection.json").write_text(json.dumps(plans, indent=2) + "\n")
        print(f"Private operation records: {run_dir}")
        routes = {f"/firmware/{plan['sha256']}.bin": (plan["image"], plan["sha256"]) for plan in plans}
        server = resources.enter_context(firmware_http.host(routes, bind))
        result_by_mac = {row["mac"]: row for row in results}
        for plan in plans:
            row = result_by_mac[plan["mac"]]
            print(f"Installing {Path(plan['image']).name} on {plan['mac']} ({plan['route']})…")
            started = monotonic()
            try:
                if plan["route"] == "lan":
                    dispatched = await dispatch_lan(plan, run_dir)
                else:
                    cloud_plan = {key: value for key, value in plan.items() if key not in ("route", "ip")}
                    dispatched = await cloud.dispatch(api, args.region, cloud_plan, run_dir)
                row.update(dispatched)
                if dispatched["outcome"] == "install_rejected":
                    break
                row.update(await verify_install(plan, server, started))
            except Exception as error:
                recorded = os.path.lexists(run_dir / f"install-{plan['mac']}.json")
                reason = str(error) if isinstance(error, (cloud.Blocked, firmware_http.FirmwareError, OSError)) else type(error).__name__
                row.update(outcome="install_unconfirmed" if recorded else "blocked", reason=reason)
                print(f"Stopped: {reason!r}")
                break
            if row["outcome"] not in ("verified_reported_hid", "lan_available"):
                print("Installation is unresolved; no more upgrade requests will be sent.")
                break
        (run_dir / "results.json").write_text(json.dumps(results, indent=2) + "\n")
        report(results)
        unresolved = any(row["outcome"] in ("install_unconfirmed", "install_mismatch") for row in results)
        if unresolved:
            print("Firmware hosting remains available for the unresolved transfer. Ctrl-C stops hosting; do not resend an OTA blindly.")
            await asyncio.Event().wait()
        print("Stopping the temporary firmware host.")
        return 0 if all(row["outcome"] in ("kept_current", "already_target", "verified_reported_hid", "lan_available") for row in results) else 3


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", choices=sorted(cloud.GREE_CLOUD_SERVERS), help="also include devices in this Gree+ account region")
    parser.add_argument("--bind", help="LAN IPv4 to host firmware on TCP/80 (default: route to a discovered device)")
    args = parser.parse_args(argv)
    # SDK loggers created during binding can expose packets and device keys.
    logging.disable(logging.CRITICAL)
    try:
        return asyncio.run(run(args))
    except (cloud.Blocked, firmware_http.FirmwareError) as error:
        print(f"Stopped: {str(error)!r}")
    except (KeyboardInterrupt, EOFError):
        print("Stopped. Existing send ledgers must not be replayed; verify any dispatched device before another attempt.")
    except Exception as error:
        print(f"Operation failed ({type(error).__name__}); credentials and response bodies suppressed. Check private run records before any further attempt.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
