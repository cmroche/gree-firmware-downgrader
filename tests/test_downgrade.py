import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import downgrade as app

MAC = "aabbccddeeff"
CODE = "362001068254"
OLD = CODE + "+U-4MWB65VRV2.12.bin"
TARGET = CODE + "+U-4MWB65VRV2.11.bin"
SHA = "a" * 64


def image(local=False, target=TARGET):
    filename = f"{CODE}-v2.11" + ("-local-only" if local else "") + ".bin"
    return {"firmware_code": CODE, "version": "2.11", "image": "/bundle/" + filename,
            "size": 24, "sha256": SHA, "target_hid": None if local else target, "local_only": local}


def device(mac=MAC, hid=OLD):
    return {"mac": mac, "name": "Bench", "hid": hid, "ip": "192.0.2.2", "port": 7000,
            "status": "identified", "route": "lan"}


def plan(local=False):
    return {**image(local), "mac": MAC, "current_hid": OLD, "route": "lan",
            "url": f"http://192.168.1.10/firmware/{SHA}.bin", "ip": "192.0.2.2"}


class SelectionTests(unittest.TestCase):
    def test_choices_are_per_device_and_never_cross_firmware_codes(self):
        other = device("112233445566", "362001067540+U-WB05WR13V2.04.bin")
        other_image = {**image(), "firmware_code": "362001067540", "version": "2.06",
                       "target_hid": "362001067540+U-WB05WR13V2.06.bin"}
        with patch("builtins.input", side_effect=["wrong", "1", "0"]), contextlib.redirect_stdout(io.StringIO()):
            plans, results = app.select_firmware([device(), other], [image(), image(True), other_image], "192.168.1.10")
        self.assertEqual([(p["mac"], p["local_only"]) for p in plans], [(MAC, True)])
        self.assertEqual([r["outcome"] for r in results], ["pending", "kept_current"])
        self.assertEqual(app.compatible_images(other["hid"], [image()]), [])
        self.assertEqual(app.compatible_images(other["hid"], [other_image]), [other_image])

    def test_same_version_local_image_is_installable_but_vendor_is_not_reflashed(self):
        devices = [device(hid=TARGET), device("112233445566", TARGET)]
        with patch("builtins.input", side_effect=["2", "1"]), contextlib.redirect_stdout(io.StringIO()):
            plans, results = app.select_firmware(devices, [image(target=TARGET), image(True)], "192.168.1.10")
        self.assertEqual([r["outcome"] for r in results], ["already_target", "pending"])
        self.assertEqual(len(plans), 1)
        self.assertTrue(plans[0]["local_only"])
        self.assertIsNone(plans[0]["target_hid"])

    def test_unknown_vendor_target_and_different_module_are_not_offered(self):
        self.assertEqual(app.compatible_images(OLD, [image(target=None)]), [])
        self.assertEqual(app.compatible_images(CODE + "+U-DIFFERENTV2.12.bin", [image()]), [])

    def test_cloud_and_lan_disagreement_blocks_selection(self):
        remote = {"mac": MAC, "name": "Cloud name", "hid": TARGET, "status": "identified"}
        merged = app.merge_inventory([device()], [remote])
        self.assertEqual(merged[0]["status"], "blocked_identity")
        self.assertEqual(len(merged), 1)

    def test_catalog_rejects_corruption_and_local_image_path_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = b"vendor firmware bytes"
            entry = {**image(), "image": f"{CODE}-v2.11.bin", "size": len(content),
                     "sha256": hashlib.sha256(content).hexdigest()}
            (root / entry["image"]).write_bytes(content)
            (root / "manifest.json").write_text(json.dumps({"images": [entry]}))
            self.assertEqual(app.load_catalog(root)[0]["sha256"], entry["sha256"])
            (root / entry["image"]).write_bytes(b"damaged")
            with self.assertRaises(app.firmware_http.FirmwareError):
                app.load_catalog(root)
            (root / entry["image"]).write_bytes(content)
            with tempfile.TemporaryDirectory() as outside:
                external = Path(outside) / "private.bin"
                external.write_bytes(b"not bundled")
                (root / f"{CODE}-v2.11-local-only.bin").symlink_to(external)
                with self.assertRaises(app.firmware_http.FirmwareError):
                    app.load_catalog(root)


class LanDispatchTests(unittest.IsolatedAsyncioTestCase):
    def module(self):
        module = MagicMock()
        module.bind = AsyncMock()
        module.send = AsyncMock()
        return module

    async def test_changed_hid_stops_before_any_upgrade(self):
        module = self.module()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(app.firmware_http, "preflight"),
            patch.object(app, "rediscover", AsyncMock(return_value=SimpleNamespace(ip="192.0.2.2"))),
            patch.object(app, "Device", return_value=module),
            patch.object(app, "fresh_hid", AsyncMock(return_value=TARGET)),
        ):
            with self.assertRaises(app.cloud.Blocked):
                await app.dispatch_lan(plan(), directory)
            module.send.assert_not_awaited()
            self.assertEqual(list(Path(directory).iterdir()), [])

    async def test_uncertain_send_has_a_durable_ledger_and_is_not_replayed(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / f"install-{MAC}.json"
            async def send(request):
                record = json.loads(ledger.read_text())
                self.assertEqual((record["mac"], record["url"]), (MAC, plan()["url"]))
                self.assertEqual(request["pack"], {"t": "upgrade", "mac": MAC, "url": plan()["url"]})
                raise TimeoutError()
            module.send.side_effect = send
            with (
                patch.object(app.firmware_http, "preflight"),
                patch.object(app, "rediscover", AsyncMock(return_value=SimpleNamespace(ip="192.0.2.2"))),
                patch.object(app, "Device", return_value=module),
                patch.object(app, "fresh_hid", AsyncMock(return_value=OLD)),
            ):
                result = await app.dispatch_lan(plan(), directory)
                self.assertEqual(result["outcome"], "install_unconfirmed")
                with self.assertRaises(app.cloud.Blocked):
                    await app.dispatch_lan(plan(), directory)
            module.send.assert_awaited_once()


class VerificationTests(unittest.IsolatedAsyncioTestCase):
    async def verify(self, downloaded_at, local=True):
        clock = [20.0]
        async def sleep(delay):
            clock[0] += delay
        selected = plan(local)
        endpoint = SimpleNamespace(ip="192.0.2.2")
        server = SimpleNamespace(completed_downloads={("192.0.2.2", f"/firmware/{SHA}.bin"): downloaded_at})
        with (
            patch.object(app, "monotonic", side_effect=lambda: clock[0]),
            patch.object(app.asyncio, "sleep", side_effect=sleep),
            patch.object(app, "rediscover", AsyncMock(return_value=endpoint)),
            patch.object(app, "observe", AsyncMock(return_value=OLD)),
        ):
            return await app.verify_install(selected, server, 10.0)

    async def test_completed_local_download_and_fresh_lan_do_not_claim_flash_identity(self):
        result = await self.verify(15.0)
        self.assertEqual(result["outcome"], "lan_available")
        self.assertEqual(result["after_hid"], OLD)
        self.assertTrue(result["download_served"])

    async def test_earlier_download_or_unchanged_vendor_hid_is_not_installation_proof(self):
        self.assertEqual((await self.verify(5.0))["outcome"], "install_unconfirmed")
        self.assertEqual((await self.verify(15.0, local=False))["outcome"], "install_unconfirmed")

    async def test_canceling_later_device_choice_sends_nothing_to_earlier_device(self):
        args = SimpleNamespace(region=None, bind="192.168.1.10")
        with (
            patch.object(app, "load_catalog", return_value=[image()]),
            patch.object(app, "lan_inventory", AsyncMock(return_value=[device(), device("112233445566")])),
            patch("builtins.input", side_effect=["1", EOFError()]),
            patch.object(app, "dispatch_lan", AsyncMock()) as dispatch,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(EOFError):
                await app.run(args)
            dispatch.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
