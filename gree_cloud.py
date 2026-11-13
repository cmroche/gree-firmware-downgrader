#!/usr/bin/env python3
"""Gree cloud inventory and single-dispatch helpers."""

import asyncio
import base64
import json
import logging
import os
import re
import secrets
import socket
import ssl
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from ipaddress import ip_address, ip_network
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

_RFC1918_NETWORKS = tuple(
    ip_network(network) for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
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


def _dispatch_plan(plan):
    required = {"mac", "current_hid", "target_hid", "image", "sha256", "url", "local_only"}
    optional = {"size", "version", "provenance", "source_url"}
    if not isinstance(plan, dict) or not required <= plan.keys() or plan.keys() - required - optional:
        raise Blocked("Dispatch plan does not match the approved selection")
    approved = dict(plan)
    approved["mac"] = canonical_mac(plan["mac"])
    current = plan["current_hid"]
    target = plan["target_hid"]
    local_only = plan["local_only"]
    if not valid_hid(current) or type(local_only) is not bool:
        raise Blocked("Dispatch plan requires a full current HID and local-only flag")
    code = current.split("+", 1)[0]
    if local_only:
        if target is not None and (not valid_hid(target) or target.split("+", 1)[0] != code):
            raise Blocked("Local-only target HID must be unknown or match the physical module")
        if plan.get("source_url") is not None:
            raise Blocked("Local-only firmware must not claim an official source URL")
    else:
        if not valid_hid(target) or target.split("+", 1)[0] != code:
            raise Blocked("Current and target firmware codes differ")
        if target == current:
            raise Blocked("Already reports the target HID; no installation sent")
    if not isinstance(plan["image"], str) or not Path(plan["image"]).is_absolute():
        raise Blocked("Dispatch plan requires an absolute firmware image path")
    if not isinstance(plan["sha256"], str) or not re.fullmatch(r"[0-9a-fA-F]{64}", plan["sha256"]):
        raise Blocked("Plan SHA-256 must contain 64 hexadecimal digits")
    if not isinstance(plan["url"], str):
        raise Blocked("Dispatch plan requires a firmware URL")
    try:
        parsed_url = urlsplit(plan["url"])
        address = ip_address(parsed_url.hostname)
    except (TypeError, ValueError):
        raise Blocked("Cloud OTA URL must use an RFC1918 IPv4 address") from None
    if (
        parsed_url.scheme != "http"
        or address.version != 4
        or not any(address in network for network in _RFC1918_NETWORKS)
    ):
        raise Blocked("Cloud OTA URL must use an RFC1918 IPv4 HTTP address")
    approved["sha256"] = plan["sha256"].lower()
    if "size" in plan and (type(plan["size"]) is not int or plan["size"] < 0):
        raise Blocked("Dispatch plan size must be a non-negative integer")
    for field in ("version", "provenance"):
        if field in plan and not isinstance(plan[field], str):
            raise Blocked(f"Dispatch plan {field} must be text")
    if "source_url" in plan:
        source_url = plan["source_url"]
        if source_url is not None:
            try:
                source = urlsplit(source_url)
                source_port = source.port
            except (TypeError, ValueError):
                source = None
                source_port = None
            if (
                source is None
                or not isinstance(source_url, str)
                or source.scheme not in ("http", "https")
                or not source.hostname
                or not (source.hostname == "gree.com" or source.hostname.endswith(".gree.com"))
                or source.username is not None
                or source.password is not None
                or source_port is not None
                or source.fragment
                or source.query
                or any(c.isspace() or ord(c) < 32 for c in source_url)
            ):
                raise Blocked("Original source URL must be an official Gree HTTP(S) URL")
    return approved


def ledger_path(run_dir, mac):
    mac = canonical_mac(mac)
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
    for record in records:
        raw_mac = record.mac if isinstance(record.mac, str) else None
        row = {
            "mac": raw_mac,
            "name": record.name if isinstance(record.name, str) else None,
            "protocol": record.version if isinstance(record.version, str) else None,
        }
        try:
            mac = canonical_mac(record.mac)
        except Blocked as error:
            row.update(status="blocked_identity", reason=str(error))
            rows.append(row)
            continue
        if mac in seen:
            continue
        seen.add(mac)
        row["mac"] = mac
        try:
            info = module_record(records, mac)
            module = CloudModule(api, region, info)
        except Blocked as error:
            row.update(status="blocked_identity", reason=str(error))
        except Exception as error:
            row.update(status="blocked_cloud", error=type(error).__name__)
        else:
            try:
                async with module:
                    row.update(
                        transport=module.transport,
                        hid=await module.fresh_hid(),
                        status="identified",
                    )
            except Blocked as error:
                row.update(status="blocked_unreachable", reason=str(error))
            except Exception as error:
                row.update(status="blocked_unreachable", error=type(error).__name__)
        rows.append(row)
    return rows


async def dispatch(api, region, plan, run_dir):
    approved = _dispatch_plan(plan)
    try:
        ledger = ledger_path(run_dir, approved["mac"])
    except (Blocked, FirmwareError):
        raise
    except Exception as error:
        raise Blocked(f"Could not prepare the private installation ledger ({type(error).__name__})") from None

    recorded = False
    transport = None
    try:
        await asyncio.to_thread(
            preflight, approved["url"], approved["image"], approved["sha256"], allow_lan=True
        )
        records = await account_devices(api)
        info = module_record(records, approved["mac"])
        async with CloudModule(api, region, info) as module:
            if await module.fresh_hid() != approved["current_hid"]:
                raise Blocked("Current cloud HID differs from the approved selection; no installation sent")
            transport = module.transport
            record_dispatch(ledger, approved, transport)
            recorded = True
            try:
                reply = await module.upgrade(approved["url"])
            except Exception as error:
                return {
                    "outcome": "install_unconfirmed",
                    "ledger": str(ledger),
                    "transport": transport,
                    "error": type(error).__name__,
                }
            result = reply.get("r") if isinstance(reply, dict) else None
            result_code = result if type(result) is int else None
            return {
                "outcome": "install_requested" if result_code == 200 else "install_rejected",
                "ledger": str(ledger),
                "transport": transport,
                "result": result_code,
            }
    except (Blocked, FirmwareError) as error:
        if recorded:
            return {
                "outcome": "install_unconfirmed",
                "ledger": str(ledger),
                "transport": transport,
                "error": type(error).__name__,
            }
        raise
    except Exception as error:
        if recorded:
            return {
                "outcome": "install_unconfirmed",
                "ledger": str(ledger),
                "transport": transport,
                "error": type(error).__name__,
            }
        raise Blocked(
            f"Cloud dispatch preflight failed ({type(error).__name__}); no installation sent"
        ) from None


async def refuse_cloud_redirect(_session, _context, _params):
    raise Blocked("Cloud API redirect refused; credentials were not forwarded")


@asynccontextmanager
async def login(region, username, password):
    if region not in GREE_CLOUD_SERVERS:
        raise Blocked("Unknown Gree cloud region")
    if not isinstance(username, str) or not username or not isinstance(password, str) or not password:
        raise Blocked("Gree+ username and password are required")
    api = GreeCloudApi.for_server(region, username, password)
    session = None
    try:
        # The SDK's default session trusts environment proxies and redirects.
        await api.close()
        trace = aiohttp.TraceConfig()
        trace.on_request_redirect.append(refuse_cloud_redirect)
        session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=cloud_tls()),
            timeout=aiohttp.ClientTimeout(total=30, connect=10),
            trust_env=False,
            trace_configs=[trace],
        )
        api._session = session
        async with api:
            await api.login()
            yield api
    finally:
        if session is not None and not session.closed:
            try:
                await api.close()
            finally:
                if not session.closed:
                    await session.close()
