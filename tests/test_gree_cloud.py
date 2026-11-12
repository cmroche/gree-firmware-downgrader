import asyncio
import base64
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/gree-firmware-downgrader/scripts"))

import gree_cloud as cloud
from greeclimate_davo22.cloud_api import CloudDeviceInfo, CloudHome

MAC = "aabbccddeeff"
KEY = "0123456789abcdef"
OLD_HID = "362001068254+U-4MWB65VRV2.12.bin"
TARGET_HID = "362001068254+U-4MWB65VRV2.11.bin"
URL = "http://eu.grih.gree.com/wifiModule/image/10141/145680"


def info(**changes):
    return CloudDeviceInfo(**{
        "name": "Bench module", "mac": MAC, "key": KEY, "version": "V3.5.0", **changes
    })


def reply(cipher, cid, inner, mac=MAC):
    pack, tag = cipher.encrypt(inner)
    outer = {"t": "pack", "cid": mac, "tcid": cid, "pack": pack}
    if tag:
        outer["tag"] = tag
    return json.dumps(outer).encode()


class Messages:
    def __init__(self):
        self.queue = asyncio.Queue()

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.queue.get()


class Broker:
    def __init__(self, status_noise=False):
        self.messages = Messages()
        self.published = []
        self.status_noise = status_noise

    async def publish(self, topic, payload, **options):
        outer = json.loads(payload)
        inner = cloud.CipherV1(KEY.encode()).decrypt(outer["pack"])
        self.published.append((topic, inner, options))
        if inner["t"] == "status":
            response = {"t": "dat", "r": 200, "cols": ["hid"], "dat": [OLD_HID]}
            if self.status_noise:
                stale = {**response, "dat": [TARGET_HID]}
                for raw, retained in (
                    (reply(cloud.CipherV1(KEY.encode()), "old-request", stale), False),
                    (reply(cloud.CipherV1(KEY.encode()), outer["cid"], stale, "112233445566"), False),
                    (reply(cloud.CipherV1(KEY.encode()), outer["cid"], stale), True),
                ):
                    self.enqueue(raw, retained)
            self.enqueue(reply(cloud.CipherV1(KEY.encode()), outer["cid"], response))
        else:
            self.enqueue(reply(cloud.CipherV1(), outer["cid"], {"t": "ret", "r": 200, "mac": MAC}))

    def enqueue(self, payload, retained=False):
        self.messages.queue.put_nowait(SimpleNamespace(
            topic=cloud.aiomqtt.Topic(f"response/{MAC}/app"), payload=payload, retain=retained
        ))


class CloudProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_hid_ignores_retained_wrong_request_and_wrong_module(self):
        module = cloud.CloudModule(SimpleNamespace(user_id=123, token="private"), "Europe", info())
        module.mqtt = Broker(status_noise=True)
        self.assertEqual(await module.fresh_hid(), OLD_HID)
        self.assertEqual(module.mqtt.published[0][1], {"t": "status", "mac": MAC, "cols": ["hid"]})

    async def test_static_key_ack_and_single_nonretained_upgrade(self):
        module = cloud.CloudModule(SimpleNamespace(user_id=123, token="private"), "Europe", info())
        module.mqtt = Broker()
        await module.fresh_hid()
        self.assertEqual((await module.upgrade(URL))["r"], 200)
        self.assertEqual(module.mqtt.published, [
            (f"request/{MAC}", {"t": "status", "mac": MAC, "cols": ["hid"]}, {"qos": 0, "retain": False}),
            (f"request/{MAC}", {"t": "upgrade", "mac": MAC, "url": URL}, {"qos": 0, "retain": False}),
        ])

    async def test_cipher_negotiation_is_readonly_and_reuses_proven_cipher(self):
        module = cloud.CloudModule(SimpleNamespace(user_id=123), "Europe", info())
        module.exchange = AsyncMock(side_effect=[
            asyncio.TimeoutError(),
            {"t": "dat", "cols": ["hid"], "dat": [OLD_HID]},
            {"t": "dat", "cols": ["hid"], "dat": [OLD_HID]},
        ])
        self.assertEqual(await module.fresh_hid(), OLD_HID)
        self.assertIsInstance(module.cipher, cloud.CipherV2)
        self.assertEqual(await module.fresh_hid(), OLD_HID)
        self.assertTrue(all(call.args[0]["t"] == "status" for call in module.exchange.call_args_list))
        self.assertIs(module.exchange.call_args_list[-1].args[1], module.cipher)

    async def test_inventory_keeps_physical_gateway_and_blocks_logical_children(self):
        api = SimpleNamespace(
            get_homes=AsyncMock(return_value=[CloudHome(id=1, name="Bench")]),
            get_devices=AsyncMock(return_value=[info(), info(mac=MAC + "00", parent_mac=MAC)]),
        )
        module = MagicMock()
        module.mac, module.transport = MAC, "mqtt"
        module.fresh_hid = AsyncMock(return_value=OLD_HID)
        module.__aenter__.return_value = module
        original = cloud.CloudModule

        def create(api, region, record):
            original(api, region, record)
            return module

        output = io.StringIO()
        with patch.object(cloud, "CloudModule", side_effect=create), contextlib.redirect_stdout(output):
            await cloud.inventory(api, "Europe")
        rows = json.loads(output.getvalue())
        self.assertEqual([(r["mac"], r["status"]) for r in rows], [(MAC, "identified"), (MAC + "00", "blocked_identity")])
        self.assertNotIn(KEY, output.getvalue())

    async def test_gateway_does_not_turn_tls_exception_into_plaintext_consent(self):
        module = cloud.CloudModule(SimpleNamespace(user_id=123), "Europe", info(version="V2.0.0"))
        module.gateway.server_for = AsyncMock(return_value=SimpleNamespace(ssl=False))
        with patch.object(cloud.asyncio, "open_connection", AsyncMock()) as connect:
            with self.assertRaises(cloud.Blocked):
                await module.fresh_hid()
            connect.assert_not_called()


class PlanTests(unittest.TestCase):
    def test_gcm_authentication_rejects_modified_message(self):
        cipher = cloud.CipherV2(KEY.encode())
        raw = reply(cipher, "requested", {"t": "dat", "cols": ["hid"], "dat": [OLD_HID]})
        self.assertEqual(cloud.decode_reply(raw, MAC, "requested", cipher)["dat"], [OLD_HID])
        outer = json.loads(raw)
        altered = bytearray(base64.b64decode(outer["pack"]))
        altered[0] ^= 1
        outer["pack"] = base64.b64encode(altered).decode()
        self.assertIsNone(cloud.decode_reply(json.dumps(outer).encode(), MAC, "requested", cipher))

    def test_plan_rejects_logical_child_cross_code_reinstall_and_false_provenance(self):
        good = {"mac": MAC, "current_hid": OLD_HID, "target_hid": TARGET_HID,
                "image": "original.bin", "sha256": "a" * 64, "source_url": URL, "url": URL}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            path.write_text(json.dumps(good))
            (Path(directory) / "original.bin").write_bytes(b"plan-relative image")
            self.assertEqual(Path(cloud.load_plan(path)["image"]).read_bytes(), b"plan-relative image")
            for change in (
                {"mac": MAC + "00"},
                {"target_hid": "362001067541+U-WB05WR11V2.04.bin"},
                {"target_hid": OLD_HID},
                {"source_url": "http://gree.com.example.org/image.bin"},
                {"source_url": "http://secret@grih.gree.com/image.bin"},
            ):
                with self.subTest(change=change):
                    path.write_text(json.dumps({**good, **change}))
                    with self.assertRaises(cloud.Blocked):
                        cloud.load_plan(path)

    def test_duplicate_cloud_names_are_not_duplicate_identity_and_conflicts_block(self):
        self.assertEqual(cloud.module_record([info(), info(name="Other home")], MAC).mac, MAC)
        self.assertEqual(cloud.module_record([info(mac="AA:BB:CC:DD:EE:FF")], MAC).mac, "AA:BB:CC:DD:EE:FF")
        with self.assertRaises(cloud.Blocked):
            cloud.module_record([info(), info(key="fedcba9876543210")], MAC)


class InstallationTests(unittest.IsolatedAsyncioTestCase):
    def module(self, hids=(OLD_HID, OLD_HID)):
        module = MagicMock()
        module.__aenter__.return_value = module
        module.transport = "mqtt"
        module.fresh_hid = AsyncMock(side_effect=hids)
        module.upgrade = AsyncMock(return_value={"t": "ret", "r": 200})
        return module

    async def attempt(self, directory, module, approval=None):
        plan = {"mac": MAC, "current_hid": OLD_HID, "target_hid": TARGET_HID,
                "image": "/private/original.bin", "sha256": "a" * 64, "source_url": URL, "url": URL}
        args = SimpleNamespace(region="Europe", allow_lan_url=False)
        ledger = cloud.ledger_path(directory, MAC)
        phrase = f"FLASH {MAC} {'a' * 64}" if approval is None else approval
        with (
            patch.object(cloud, "account_devices", AsyncMock(return_value=[info()])),
            patch.object(cloud, "CloudModule", return_value=module),
            patch("builtins.input", return_value=phrase),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            return await cloud.install(SimpleNamespace(), args, plan, ledger)

    async def test_approval_and_fresh_identity_gate_every_dispatch(self):
        for module, approval in (
            (self.module(), "no"),
            (self.module(hids=(TARGET_HID,)), None),
            (self.module(hids=(OLD_HID, TARGET_HID)), None),
        ):
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(cloud.Blocked):
                    await self.attempt(directory, module, approval)
                module.upgrade.assert_not_called()
                self.assertEqual(list(Path(directory).iterdir()), [])

    async def test_uncertain_send_is_durably_recorded_and_cannot_be_replayed(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.module()
            ledger = Path(directory) / f"install-{MAC}.json"

            async def uncertain_send(url):
                record = json.loads(ledger.read_text())
                self.assertEqual((record["mac"], record["url"], record["outcome"]), (MAC, URL, "install_requested"))
                self.assertFalse(record["cloud_tls_verified"])
                if sys.platform != "win32":
                    self.assertEqual(ledger.stat().st_mode & 0o777, 0o600)
                raise asyncio.TimeoutError()

            module.upgrade.side_effect = uncertain_send
            self.assertEqual(await self.attempt(directory, module), 3)
            module.upgrade.assert_awaited_once_with(URL)
            with self.assertRaises(cloud.Blocked):
                cloud.ledger_path(directory, MAC)
            ledger.write_text("")
            with self.assertRaises(cloud.Blocked):
                cloud.ledger_path(directory, MAC)

    async def test_fsync_failure_prevents_the_network_send(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.module()
            with patch.object(cloud.os, "fsync", side_effect=OSError("disk failed")):
                with self.assertRaises(OSError):
                    await self.attempt(directory, module)
            module.upgrade.assert_not_called()


if __name__ == "__main__":
    unittest.main()
