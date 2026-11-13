import asyncio
import base64
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gree_cloud as cloud
from greeclimate_davo22.cloud_api import CloudDeviceInfo, CloudHome

MAC = "aabbccddeeff"
KEY = "0123456789abcdef"
OLD_HID = "362001068254+U-4MWB65VRV2.12.bin"
TARGET_HID = "362001068254+U-4MWB65VRV2.11.bin"
URL = "http://eu.grih.gree.com/wifiModule/image/10141/145680"
LOCAL_CURRENT_HID = "362001000762+unit-test-current"
LOCAL_IMAGE = str(Path(__file__).resolve().parents[1] / "firmware/362001000762-v3.33-local-only.bin")
LOCAL_URL = "http://192.168.1.10/firmware/" + "a" * 64 + ".bin"


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

    async def test_inventory_keeps_physical_children_and_unreachable_rows(self):
        unreachable_mac = "112233445566"
        api = SimpleNamespace(
            get_homes=AsyncMock(return_value=[CloudHome(id=1, name="Bench")]),
            get_devices=AsyncMock(return_value=[
                info(), info(mac=MAC + "00", parent_mac=MAC), info(mac=unreachable_mac),
            ]),
        )
        module = MagicMock()
        module.mac, module.transport = MAC, "mqtt"
        module.fresh_hid = AsyncMock(return_value=OLD_HID)
        module.__aenter__.return_value = module
        original = cloud.CloudModule

        def create(api, region, record):
            original(api, region, record)
            if record.mac == unreachable_mac:
                failed = MagicMock()
                failed.transport = "mqtt"
                failed.fresh_hid = AsyncMock(side_effect=asyncio.TimeoutError())
                failed.__aenter__.return_value = failed
                return failed
            return module

        with patch.object(cloud, "CloudModule", side_effect=create):
            rows = await cloud.inventory(api, "Europe")
        self.assertEqual(
            [(row["mac"], row["status"]) for row in rows],
            [
                (MAC, "identified"),
                (MAC + "00", "blocked_identity"),
                (unreachable_mac, "blocked_unreachable"),
            ],
        )
        self.assertNotIn(KEY, json.dumps(rows))

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

    def test_local_only_selection_uses_flat_filename_and_allows_unknown_or_same_hid(self):
        plan = {
            "mac": MAC, "current_hid": LOCAL_CURRENT_HID, "target_hid": None,
            "image": LOCAL_IMAGE, "sha256": "a" * 64, "url": LOCAL_URL,
            "local_only": True,
        }
        self.assertIsNone(cloud._dispatch_plan(plan)["target_hid"])
        self.assertEqual(
            cloud._dispatch_plan({**plan, "target_hid": LOCAL_CURRENT_HID})["target_hid"],
            LOCAL_CURRENT_HID,
        )
        self.assertNotIn("source_url", cloud._dispatch_plan(plan))
        with self.assertRaises(cloud.Blocked):
            cloud._dispatch_plan({**plan, "source_url": URL})
        with self.assertRaises(cloud.Blocked):
            cloud._dispatch_plan({**plan, "url": URL})

    def test_vendor_selection_rejects_cross_code_and_same_hid(self):
        plan = {
            "mac": MAC, "current_hid": OLD_HID, "target_hid": TARGET_HID,
            "image": "/private/vendor.bin", "sha256": "a" * 64, "url": LOCAL_URL,
            "local_only": False,
        }
        with self.assertRaises(cloud.Blocked):
            cloud._dispatch_plan({**plan, "target_hid": OLD_HID})
        with self.assertRaises(cloud.Blocked):
            cloud._dispatch_plan({**plan, "target_hid": "362001067541+U-WB05WR11V2.04.bin"})

    def test_duplicate_cloud_names_are_not_duplicate_identity_and_conflicts_block(self):
        self.assertEqual(cloud.module_record([info(), info(name="Other home")], MAC).mac, MAC)
        self.assertEqual(cloud.module_record([info(mac="AA:BB:CC:DD:EE:FF")], MAC).mac, "AA:BB:CC:DD:EE:FF")
        with self.assertRaises(cloud.Blocked):
            cloud.module_record([info(), info(key="fedcba9876543210")], MAC)


class CloudLoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_login_disables_proxy_inherits_tls_exception_and_closes_after_failure(self):
        session = SimpleNamespace(closed=False, close=AsyncMock())

        async def close_session():
            session.closed = True

        session.close.side_effect = close_session
        api = MagicMock()
        api.close = AsyncMock()
        api.login = AsyncMock()
        api.__aenter__.return_value = api
        api.__aexit__.return_value = None
        with (
            patch.object(cloud.GreeCloudApi, "for_server", return_value=api),
            patch.object(cloud.aiohttp, "ClientSession", return_value=session) as make_session,
        ):
            with self.assertRaisesRegex(RuntimeError, "body"):
                async with cloud.login("Europe", "account", "secret"):
                    raise RuntimeError("body")
        options = make_session.call_args.kwargs
        self.assertFalse(options["trust_env"])
        self.assertFalse(options["connector"]._ssl.check_hostname)
        self.assertEqual(options["connector"]._ssl.verify_mode, cloud.ssl.CERT_NONE)
        self.assertIn(cloud.refuse_cloud_redirect, options["trace_configs"][0].on_request_redirect)
        api.login.assert_awaited_once_with()
        self.assertTrue(session.closed)
        with self.assertRaises(cloud.Blocked):
            await cloud.refuse_cloud_redirect(None, None, None)


class CloudDispatchTests(unittest.IsolatedAsyncioTestCase):
    def plan(self, **changes):
        return {
            "mac": MAC, "current_hid": LOCAL_CURRENT_HID, "target_hid": None,
            "image": LOCAL_IMAGE, "sha256": "a" * 64, "url": LOCAL_URL,
            "local_only": True, **changes,
        }

    def module(self, hids=(LOCAL_CURRENT_HID,)):
        module = MagicMock()
        module.__aenter__.return_value = module
        module.transport = "mqtt"
        module.fresh_hid = AsyncMock(side_effect=hids)
        module.upgrade = AsyncMock(return_value={"t": "ret", "r": 200})
        return module

    async def attempt(self, directory, module, plan=None):
        with (
            patch.object(cloud, "account_devices", AsyncMock(return_value=[info()])),
            patch.object(cloud, "CloudModule", return_value=module),
            patch.object(cloud, "preflight", return_value={"sha256": "a" * 64}) as check_image,
            patch("builtins.input") as prompt,
        ):
            result = await cloud.dispatch(
                SimpleNamespace(), "Europe", plan or self.plan(), directory
            )
        prompt.assert_not_called()
        if result["outcome"] != "install_unconfirmed":
            check_image.assert_called_once_with(LOCAL_URL, LOCAL_IMAGE, "a" * 64, allow_lan=True)
        return result

    async def test_identity_mismatch_blocks_without_ledger_or_send(self):
        for hid in (TARGET_HID, "not-a-full-hid"):
            with tempfile.TemporaryDirectory() as directory:
                module = self.module(hids=(hid,))
                with self.assertRaises(cloud.Blocked):
                    await self.attempt(directory, module)
                module.upgrade.assert_not_called()
                self.assertEqual(list(Path(directory).iterdir()), [])

    async def test_local_only_same_hid_dispatches_after_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.module()
            plan = self.plan(target_hid=LOCAL_CURRENT_HID)
            result = await self.attempt(directory, module, plan)
        self.assertEqual(result["outcome"], "install_requested")
        module.upgrade.assert_awaited_once_with(LOCAL_URL)

    async def test_durable_ledger_is_fsynced_before_one_send_and_prevents_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.module()
            ledger = Path(directory) / f"install-{MAC}.json"
            original_fsync = cloud.os.fsync

            async def send(url):
                record = json.loads(ledger.read_text())
                self.assertEqual((record["mac"], record["url"], record["outcome"]), (MAC, LOCAL_URL, "install_requested"))
                self.assertTrue(record["local_only"])
                self.assertIsNone(record["target_hid"])
                self.assertNotIn("source_url", record)
                self.assertFalse(record["cloud_tls_verified"])
                fsync.assert_called_once()
                if sys.platform != "win32":
                    self.assertEqual(ledger.stat().st_mode & 0o777, 0o600)
                return {"r": 200}

            module.upgrade.side_effect = send
            with patch.object(cloud.os, "fsync", wraps=original_fsync) as fsync:
                result = await self.attempt(directory, module)
            self.assertEqual(result["outcome"], "install_requested")
            module.fresh_hid.assert_awaited_once_with()
            module.upgrade.assert_awaited_once_with(LOCAL_URL)
            with self.assertRaises(cloud.Blocked):
                await self.attempt(directory, module)
            module.upgrade.assert_awaited_once_with(LOCAL_URL)

    async def test_uncertain_send_stays_durable_and_is_not_replayed(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.module()
            ledger = Path(directory) / f"install-{MAC}.json"

            async def uncertain_send(url):
                record = json.loads(ledger.read_text())
                self.assertEqual((record["mac"], record["url"], record["outcome"]), (MAC, LOCAL_URL, "install_requested"))
                raise asyncio.TimeoutError()

            module.upgrade.side_effect = uncertain_send
            result = await self.attempt(directory, module)
            self.assertEqual((result["outcome"], result["error"]), ("install_unconfirmed", "TimeoutError"))
            module.upgrade.assert_awaited_once_with(LOCAL_URL)
            with self.assertRaises(cloud.Blocked):
                await self.attempt(directory, module)
            module.upgrade.assert_awaited_once_with(LOCAL_URL)


    async def test_device_rejection_is_reported_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.module()
            module.upgrade.return_value = {"r": 403}
            result = await self.attempt(directory, module)
        self.assertEqual((result["outcome"], result["result"]), ("install_rejected", 403))
        module.upgrade.assert_awaited_once_with(LOCAL_URL)

    async def test_fsync_failure_prevents_the_network_send(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.module()
            with patch.object(cloud.os, "fsync", side_effect=OSError("disk failed")):
                with self.assertRaises(cloud.Blocked):
                    await self.attempt(directory, module)
            module.upgrade.assert_not_called()



if __name__ == "__main__":
    unittest.main()
