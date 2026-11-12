#!/usr/bin/env python3
"""Read Gree cloud identities or dispatch one explicitly approved OTA request."""

import argparse
import asyncio
import base64
import getpass
import json
import logging
import os
import re
import secrets
import socket
import ssl
import sys
from datetime import datetime, timezone
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
import aiomqtt
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from greeclimate_davo22.cipher import CipherV1, CipherV2
from greeclimate_davo22.cloud_api import GREE_CLOUD_SERVERS, GreeCloudApi
from greeclimate_davo22.cloud_gateway import GreeCloudGateway, parse_protocol_version
from greeclimate_davo22.mqtt_client import GreeMqttClient
from paho.mqtt.client import MQTTv311

from firmware_http import FirmwareError, preflight

for _name in tuple(logging.Logger.manager.loggerDict):
    if _name == "greeclimate_davo22" or _name.startswith("greeclimate_davo22."):
        logging.getLogger(_name).disabled = True

TIMEOUT = 10
TLS_WARNING = (
    "WARNING: Gree cloud TLS certificate/hostname validation is disabled. "
    "An active network attacker could steal cloud credentials or alter replies. "
    "This exception does not apply to firmware acquisition."
)


class Blocked(Exception):
    """A safe, user-facing refusal, without cloud response data or credentials."""


def canonical_mac(value):
    if not isinstance(value, str):
        raise Blocked("Missing physical-module MAC")
    mac = re.sub(r"[:.\-]", "", value).lower()
    if not re.fullmatch(r"[0-9a-f]{12}", mac):
        raise Blocked("Expected a physical-module MAC, not a logical child ID")
    return mac


def valid_hid(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9]{12}\+[ -~]{1,200}", value)


def cloud_tls():
    # Gree's private certificates are an explicitly accepted cloud-only exception.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


async def account_devices(api):
    # get_all_devices() discards physical gateway records in favour of children.
    records = []
    for home in await api.get_homes():
        records.extend(await api.get_devices(home.id))
    return records


def module_record(records, mac):
    matches = []
    for info in records:
        try:
            if canonical_mac(info.mac) == mac:
                matches.append(info)
        except Blocked:
            continue
    signatures = {
        (info.key, info.version, canonical_mac(info.parent_mac) if info.parent_mac else None)
        for info in matches
    }
    if len(signatures) != 1:
        raise Blocked("Physical module is missing or has conflicting cloud records")
    return matches[0]


def envelope(mac, uid, cid, command, cipher):
    pack, tag = cipher.encrypt(command)
    result = {"cid": cid, "i": 0, "pack": pack, "t": "pack", "tcid": mac, "uid": uid}
    if tag:
        result["tag"] = tag
    return json.dumps(result, separators=(",", ":")).encode()


def decode_reply(raw, mac, cid, cipher, upgrade=False):
    if len(raw) > 65536:
        return None
    try:
        outer = json.loads(raw)
        if (
            not isinstance(outer, dict)
            or outer.get("t") != "pack"
            or outer.get("tcid") != cid
            or canonical_mac(outer.get("cid")) != mac
        ):
            return None
        encrypted = base64.b64decode(outer["pack"], validate=True)
        tag = outer.get("tag")
        candidates = [cipher]
        if upgrade:
            candidates.append(CipherV2() if tag else CipherV1())
        for candidate in candidates:
            try:
                if isinstance(candidate, CipherV2):
                    if not isinstance(tag, str):
                        continue
                    aes = AES.new(candidate.key.encode(), AES.MODE_GCM, nonce=CipherV2.GCM_NONCE)
                    aes.update(CipherV2.GCM_AEAD)
                    clear = aes.decrypt_and_verify(encrypted, base64.b64decode(tag, validate=True))
                else:
                    if tag:
                        continue
                    clear = unpad(AES.new(candidate.key.encode(), AES.MODE_ECB).decrypt(encrypted), 16)
                inner = json.loads(clear)
                expected = "ret" if upgrade else "dat"
                if isinstance(inner, dict) and inner.get("t") == expected:
                    return inner
            except (ValueError, TypeError, UnicodeError):
                continue
    except (ValueError, TypeError, KeyError, Blocked):
        pass
    return None


class CloudModule:
    def __init__(self, api, region, info):
        self.api = api
        self.region = region
        self.mac = canonical_mac(info.mac)
        if info.parent_mac and canonical_mac(info.parent_mac) != self.mac:
            raise Blocked("Logical child: select its physical module record instead")
        if not isinstance(info.key, str) or len(info.key.encode()) != 16:
            raise Blocked("Cloud record has no usable device key")
        version = parse_protocol_version(info.version)
        if version is None or len(version) < 2:
            raise Blocked("Cloud protocol version is unknown; transport not guessed")
        self.transport = "mqtt" if version >= (3, 2) else "gateway"
        self.key = info.key.encode()
        self.cipher = None
        self.mqtt = None
        self.gateway = GreeCloudGateway(api, server_cache_ttl=0) if self.transport == "gateway" else None

    async def __aenter__(self):
        if self.transport == "mqtt":
            self.mqtt = aiomqtt.Client(
                hostname=GreeMqttClient.MQTT_SERVERS[self.region],
                port=1984,
                username=str(self.api.user_id),
                password=self.api.token,
                identifier="app_" + secrets.token_hex(8),
                protocol=MQTTv311,
                clean_session=True,
                tls_context=cloud_tls(),
                timeout=TIMEOUT,
                max_queued_incoming_messages=64,
            )
            await self.mqtt.__aenter__()
            try:
                for prefix in ("response", "status"):
                    await self.mqtt.subscribe(f"{prefix}/{self.mac}/#", qos=0)
            except BaseException:
                await self.mqtt.__aexit__(*sys.exc_info())
                raise
        return self

    async def __aexit__(self, *exc):
        if self.mqtt is not None:
            await self.mqtt.__aexit__(*exc)

    async def exchange(self, command, cipher, upgrade=False):
        cid = str(secrets.randbelow(9000000000) + 1000000000)
        payload = envelope(self.mac, self.api.user_id, cid, command, cipher)

        async def request():
            if self.mqtt is not None:
                # QoS 0, non-retained, clean session: no client retransmission/replay.
                await self.mqtt.publish(f"request/{self.mac}", payload, qos=0, retain=False)
                async for message in self.mqtt.messages:
                    if message.retain or not any(
                        message.topic.matches(f"{prefix}/{self.mac}/#")
                        for prefix in ("response", "status")
                    ):
                        continue
                    reply = decode_reply(message.payload, self.mac, cid, cipher, upgrade)
                    if reply is not None:
                        return reply
            else:
                server = await self.gateway.server_for(self.mac, refresh=True)
                if not server.ssl:
                    raise Blocked("Cloud gateway requires plaintext TCP; TLS exception is not plaintext consent")
                addresses = await asyncio.get_running_loop().getaddrinfo(
                    server.host, server.port, type=socket.SOCK_STREAM
                )
                if not addresses or any(not ip_address(a[4][0]).is_global for a in addresses):
                    raise Blocked("Cloud gateway must resolve only to public addresses")
                reader, writer = await asyncio.open_connection(
                    addresses[0][4][0], server.port,
                    ssl=cloud_tls(), server_hostname=server.host
                )
                try:
                    writer.write(payload + b"\n")
                    await writer.drain()
                    while line := await reader.readline():
                        reply = decode_reply(line, self.mac, cid, cipher, upgrade)
                        if reply is not None:
                            return reply
                finally:
                    writer.close()
                    try:
                        await asyncio.wait_for(writer.wait_closed(), timeout=1)
                    except (OSError, asyncio.TimeoutError):
                        pass
            raise Blocked("Cloud connection closed without a matching device response")

        return await asyncio.wait_for(request(), timeout=TIMEOUT)

    async def fresh_hid(self):
        candidates = [self.cipher] if self.cipher else [CipherV1(self.key)]
        if self.cipher is None and self.transport == "mqtt":
            candidates.append(CipherV2(self.key))
        for cipher in candidates:
            try:
                reply = await self.exchange({"t": "status", "mac": self.mac, "cols": ["hid"]}, cipher)
            except asyncio.TimeoutError:
                continue
            cols, data = reply.get("cols"), reply.get("dat")
            if (
                reply.get("r", 200) == 200
                and isinstance(cols, list)
                and isinstance(data, list)
                and len(cols) == len(data)
                and cols.count("hid") == 1
            ):
                hid = data[cols.index("hid")]
                if valid_hid(hid):
                    self.cipher = cipher
                    return hid
        raise Blocked("No fresh, correlated full HID from the physical module")

    async def upgrade(self, url):
        if self.cipher is None:
            raise Blocked("A fresh cloud identity is required before OTA")
        return await self.exchange(
            {"t": "upgrade", "mac": self.mac, "url": url}, self.cipher, upgrade=True
        )


def load_plan(path):
    path = Path(path)
    plan = json.loads(path.read_text())
    fields = {"mac", "current_hid", "target_hid", "image", "sha256", "source_url", "url"}
    if not isinstance(plan, dict) or set(plan) != fields or not all(isinstance(v, str) for v in plan.values()):
        raise Blocked("Plan must contain exactly: " + ", ".join(sorted(fields)))
    plan["mac"] = canonical_mac(plan["mac"])
    if not valid_hid(plan["current_hid"]) or not valid_hid(plan["target_hid"]):
        raise Blocked("Plan requires full current and target HIDs")
    if plan["current_hid"].split("+", 1)[0] != plan["target_hid"].split("+", 1)[0]:
        raise Blocked("Current and target firmware codes differ")
    if plan["current_hid"] == plan["target_hid"]:
        raise Blocked("Already reports the target HID; verify LAN access without reflashing")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", plan["sha256"]):
        raise Blocked("Plan SHA-256 must contain 64 hexadecimal digits")
    plan["sha256"] = plan["sha256"].lower()
    source = urlsplit(plan["source_url"])
    if (
        source.scheme not in ("http", "https")
        or not source.hostname
        or not (source.hostname == "gree.com" or source.hostname.endswith(".gree.com"))
        or source.username is not None
        or source.password is not None
        or source.port is not None
        or source.fragment
        or any(c.isspace() or ord(c) < 32 for c in plan["source_url"])
    ):
        raise Blocked("Original source URL must be an official Gree HTTP(S) URL")
    image = Path(plan["image"])
    plan["image"] = str((path.parent / image).resolve() if not image.is_absolute() else image.resolve())
    return plan


def ledger_path(run_dir, mac):
    run_dir = Path(run_dir)
    run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt" and run_dir.stat().st_mode & 0o077:
        raise Blocked("Run directory must be private (permissions 0700)")
    path = run_dir / f"install-{mac}.json"
    if os.path.lexists(path):
        raise Blocked("Installation ledger already exists; verify the previous attempt, do not resend")
    return path


def record_dispatch(path, plan, transport):
    record = {
        **plan,
        "outcome": "install_requested",
        "transport": transport,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "cloud_tls_verified": False,
    }
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(record, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


async def inventory(api, region):
    rows, seen = [], set()
    records = await account_devices(api)
    for info in records:
        row = {"mac": info.mac, "name": info.name, "protocol": info.version}
        try:
            mac = canonical_mac(info.mac)
            if mac in seen:
                continue
            seen.add(mac)
            info = module_record(records, mac)
            async with CloudModule(api, region, info) as module:
                row.update(mac=module.mac, transport=module.transport, hid=await module.fresh_hid(), status="identified")
        except Blocked as error:
            row.update(status="blocked_identity", reason=str(error))
        except Exception as error:
            row.update(status="blocked_cloud", error=type(error).__name__)
        rows.append(row)
    print(json.dumps(rows, indent=2))


async def install(api, args, plan, ledger):
    records = await account_devices(api)
    info = module_record(records, plan["mac"])
    async with CloudModule(api, args.region, info) as module:
        if await module.fresh_hid() != plan["current_hid"]:
            raise Blocked("Current cloud HID differs from the plan; no installation sent")
        print(json.dumps({**plan, "transport": module.transport, "allow_lan_url": args.allow_lan_url}, indent=2))
        print("Flashing can brick the module or lose pairing/settings and cloud/local access.")
        phrase = f"FLASH {plan['mac']} {plan['sha256']}"
        if input(f"Stable power and recovery ready? Type {phrase}: ") != phrase:
            raise Blocked("Installation not approved; no upgrade sent")
        if await module.fresh_hid() != plan["current_hid"]:
            raise Blocked("Firmware changed after approval; no installation sent")
        record_dispatch(ledger, plan, module.transport)
        try:
            reply = await module.upgrade(plan["url"])
            outcome = "install_requested" if reply.get("r") == 200 else "install_rejected"
            print(json.dumps({"outcome": outcome, "device_result": reply.get("r"), "ledger": str(ledger)}))
        except Exception as error:
            print(json.dumps({"outcome": "install_unconfirmed", "error": type(error).__name__, "ledger": str(ledger)}))
            return 3
        finally:
            print("Do not resend. Perform fresh same-MAC target-HID and LAN-status verification.", file=sys.stderr)
        return 0 if outcome == "install_requested" else 3


async def refuse_cloud_redirect(_session, _context, _params):
    raise Blocked("Cloud API redirect refused; credentials were not forwarded")


async def run(args):
    plan = ledger = None
    if args.command == "install":
        plan = load_plan(args.plan)
        ledger = ledger_path(args.run_dir, plan["mac"])
        await asyncio.to_thread(preflight, plan["url"], plan["image"], plan["sha256"], allow_lan=args.allow_lan_url)
    username = os.environ.get("GREE_USERNAME") or input("Gree+ username: ")
    password = os.environ.get("GREE_PASSWORD") or getpass.getpass("Gree+ password: ")
    if not username or not password:
        raise Blocked("Gree+ username and password are required")
    print(TLS_WARNING, file=sys.stderr)
    api = GreeCloudApi.for_server(args.region, username, password)
    # Pinned SDK has no session injection; replace only this client's REST session.
    await api.close()
    trace = aiohttp.TraceConfig()
    trace.on_request_redirect.append(refuse_cloud_redirect)
    api._session = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(ssl=cloud_tls()),
        timeout=aiohttp.ClientTimeout(total=30, connect=10),
        trust_env=False,
        trace_configs=[trace],
    )
    async with api:
        await api.login()
        if args.command == "inventory":
            await inventory(api, args.region)
            return 0
        return await install(api, args, plan, ledger)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("inventory", "install"):
        command = commands.add_parser(name)
        command.add_argument("--region", required=True, choices=sorted(GREE_CLOUD_SERVERS))
        if name == "install":
            command.add_argument("--plan", required=True, type=Path)
            command.add_argument("--run-dir", required=True, type=Path)
            command.add_argument("--allow-lan-url", action="store_true", help="Allow an explicitly approved RFC1918 IPv4 image URL")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        return asyncio.run(run(args))
    except (Blocked, FirmwareError) as error:
        print(str(error), file=sys.stderr)
    except (KeyboardInterrupt, EOFError):
        print("Interrupted. If an installation ledger exists, dispatch is uncertain: verify, do not resend.", file=sys.stderr)
    except Exception as error:
        # SDK exceptions may contain decrypted authentication responses.
        print(f"Operation failed ({type(error).__name__}); credentials and response bodies suppressed. If a ledger exists, verify before any further attempt.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
