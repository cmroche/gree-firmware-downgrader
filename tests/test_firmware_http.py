import hashlib
import http.client
import http.server
import os
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(
    0,
    str(
        Path(__file__).resolve().parents[1]
        / "skills"
        / "gree-firmware-downgrader"
        / "scripts"
    ),
)

import firmware_http  # noqa: E402


class FirmwareHttpTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.image = Path(self.tempdir.name) / "firmware.bin"
        self.body = b"unchanged firmware bytes\x00\x01"
        self.image.write_bytes(self.body)
        self.sha256 = hashlib.sha256(self.body).hexdigest()

    def _response_server(self, body, *, status=200, headers=(), body_for_ip=None):
        requests = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                requests.append(self.headers.get("Host"))
                payload = (
                    body_for_ip
                    if (
                        self.headers.get("Host") == "8.8.8.8"
                        and body_for_ip is not None
                    )
                    else body
                )
                self.send_response(status)
                for name, value in headers:
                    self.send_header(name, value)
                if (
                    not any(name.lower() == "content-length" for name, _ in headers)
                    and not any(
                        name.lower() == "transfer-encoding" for name, _ in headers
                    )
                ):
                    self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "close")
                self.end_headers()
                if payload:
                    self.wfile.write(payload)

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        return server.server_address[1], requests

    def _preflight(self, port, *, url="http://fw.gree.com/fw.bin", **kwargs):
        real_connection = http.client.HTTPConnection

        class RoutedConnection(real_connection):
            def connect(self):
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.sock.settimeout(self.timeout)
                self.sock.connect(("127.0.0.1", port))

        with (
            patch.object(
                firmware_http.socket,
                "getaddrinfo",
                return_value=[
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80))
                ],
            ),
            patch.object(
                firmware_http.http.client, "HTTPConnection", RoutedConnection
            ),
            patch.object(firmware_http.time, "sleep", lambda _delay: None),
        ):
            return firmware_http.preflight(url, self.image, self.sha256, **kwargs)

    def test_verify_hash_and_public_preflight_preserve_bytes_and_host_checks(self):
        before = self.image.read_bytes()
        self.assertEqual(
            firmware_http.verify_image(self.image, self.sha256)["length"], len(before)
        )
        port, hosts = self._response_server(
            self.body,
            headers=(("FileMd5", hashlib.md5(self.body).hexdigest()),),
        )
        result = self._preflight(port)
        self.assertEqual(self.image.read_bytes(), before)
        self.assertEqual(result["sha256"], self.sha256)
        self.assertEqual(hosts, ["fw.gree.com", "8.8.8.8"])
        self.assertEqual(
            result["file_md5"], [hashlib.md5(self.body).hexdigest()]
        )
        with self.assertRaises(firmware_http.FirmwareError):
            firmware_http.verify_image(self.image, "0" * 64)
        self.assertEqual(self.image.read_bytes(), before)

    def test_private_urls_are_narrowly_opted_in(self):
        with self.assertRaises(firmware_http.FirmwareError):
            firmware_http.preflight(
                "http://192.168.1.9/fw.bin", self.image, self.sha256
            )
        for address in ("127.0.0.1", "169.254.1.2", "100.64.0.1", "8.8.8.8"):
            with self.subTest(address=address):
                with self.assertRaises(firmware_http.FirmwareError):
                    firmware_http._parse_url(f"http://{address}/fw.bin", allow_lan=True)
        port, hosts = self._response_server(self.body)
        result = self._preflight(
            port, url="http://192.168.1.9/fw.bin", allow_lan=True
        )
        self.assertEqual(result["resolved_ips"], ["192.168.1.9"])
        self.assertEqual(hosts, ["192.168.1.9", "192.168.1.9"])

    def test_public_dns_must_not_resolve_to_private_addresses(self):
        with patch.object(
            firmware_http.socket,
            "getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))
            ],
        ), self.assertRaises(firmware_http.FirmwareError):
            firmware_http.preflight(
                "http://fw.gree.com/fw.bin", self.image, self.sha256
            )

    def test_preflight_rejects_redirect_errors_html_bad_hash_length_and_chunking(self):
        cases = (
            (b"", 302, (("Location", "http://other.invalid/"),)),
            (b"error", 503, ()),
            (self.body, 200, (("Content-Type", "text/html"),)),
            (b"x" * len(self.body), 200, ()),
            (self.body, 200, (("Content-Length", str(len(self.body) + 1)),)),
            (self.body, 200, (("Transfer-Encoding", "chunked"),)),
            (
                self.body,
                200,
                (("FileMd5", "0" * 32),),
            ),
        )
        for body, status, headers in cases:
            with self.subTest(status=status, headers=headers):
                port, _hosts = self._response_server(
                    body, status=status, headers=headers
                )
                with self.assertRaises(firmware_http.FirmwareError):
                    self._preflight(port)

    def test_ip_host_compatibility_get_must_match_the_local_image(self):
        port, hosts = self._response_server(
            self.body, body_for_ip=b"x" * len(self.body)
        )
        with self.assertRaises(firmware_http.FirmwareError):
            self._preflight(port)
        self.assertEqual(hosts, ["fw.gree.com", "8.8.8.8"])

    def test_server_exposes_only_verified_image_and_survives_path_replacement(self):
        fd = firmware_http._open_image(self.image)
        info = firmware_http._hash_fd(fd)
        replacement = Path(self.tempdir.name) / "replacement.bin"
        replacement.write_bytes(b"different file")
        os.replace(replacement, self.image)
        server = http.server.HTTPServer(
            ("127.0.0.1", 0), firmware_http._handler_for(fd, info, self.sha256)
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1])
            conn.request("GET", "/firmware.bin", headers={"Host": "192.168.1.9"})
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Content-Length"), str(len(self.body)))
            self.assertEqual(
                response.getheader("FileMd5"), hashlib.md5(self.body).hexdigest()
            )
            self.assertEqual(response.read(), self.body)
            conn.close()
            for path in ("/", "/firmware.bin?x=1", "/firmware.bin/../replacement.bin"):
                conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1])
                conn.request("GET", path)
                response = conn.getresponse()
                self.assertEqual(response.status, 404)
                self.assertEqual(response.read(), b"")
                conn.close()
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1])
            conn.request("HEAD", "/firmware.bin", headers={"Host": "192.168.1.9"})
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"")
            conn.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
            os.close(fd)


if __name__ == "__main__":
    unittest.main()
