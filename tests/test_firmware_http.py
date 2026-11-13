import contextlib
import hashlib
import http.client
import http.server
import os
import socket
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import firmware_http


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

    @contextlib.contextmanager
    def _host(self, images, wrap_handler=None):
        real_server = http.server.HTTPServer

        def make_server(_address, handler):
            if wrap_handler is not None:
                handler = wrap_handler(handler)
            return real_server(("127.0.0.1", 0), handler)

        with patch.object(firmware_http.http.server, "HTTPServer", make_server):
            with firmware_http.host(images, "192.168.1.9") as server:
                yield server

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

    def test_host_serves_distinct_pinned_routes_and_tracks_complete_downloads(self):
        other_body = b"a different approved image"
        other_image = Path(self.tempdir.name) / "other.bin"
        other_image.write_bytes(other_body)
        other_sha256 = hashlib.sha256(other_body).hexdigest()
        route = f"/firmware/{self.sha256}.bin"
        other_route = f"/firmware/{other_sha256}.bin"
        images = {
            route: (self.image, self.sha256),
            other_route: (other_image, other_sha256),
        }

        with self._host(images) as server:
            replacement = Path(self.tempdir.name) / "replacement.bin"
            replacement.write_bytes(b"replacement bytes")
            os.replace(replacement, self.image)

            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1])
            conn.request("HEAD", other_route)
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"")
            conn.close()
            self.assertNotIn(("127.0.0.1", other_route), server.completed_downloads)

            for image_route, body, digest in (
                (route, self.body, self.sha256),
                (other_route, other_body, other_sha256),
            ):
                conn = http.client.HTTPConnection(
                    "127.0.0.1", server.server_address[1]
                )
                conn.request("GET", image_route)
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader("Content-Length"), str(len(body)))
                self.assertEqual(
                    response.getheader("FileMd5"), hashlib.md5(body).hexdigest()
                )
                self.assertEqual(response.read(), body)
                conn.close()
                completion = server.completed_downloads[("127.0.0.1", image_route)]
                self.assertGreater(completion, 0)
                self.assertLessEqual(completion, time.monotonic())
                self.assertEqual(image_route, f"/firmware/{digest}.bin")

            for path in ("/firmware.bin", f"{route}?x=1", f"{route}/../other.bin"):
                conn = http.client.HTTPConnection(
                    "127.0.0.1", server.server_address[1]
                )
                conn.request("GET", path)
                response = conn.getresponse()
                self.assertEqual(response.status, 404)
                self.assertEqual(response.read(), b"")
                conn.close()

        self.assertEqual(server.fileno(), -1)

    def test_host_rejects_changed_pinned_image_without_tracking_completion(self):
        route = f"/firmware/{self.sha256}.bin"
        with self._host({route: (self.image, self.sha256)}) as server:
            self.image.write_bytes(b"changed image bytes")
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1])
            conn.request("GET", route)
            response = conn.getresponse()
            self.assertEqual(response.status, 503)
            self.assertEqual(response.read(), b"")
            conn.close()
            self.assertNotIn(("127.0.0.1", route), server.completed_downloads)

    def test_host_refuses_nonprivate_bind_addresses(self):
        route = f"/firmware/{self.sha256}.bin"
        for bind in ("127.0.0.1", "8.8.8.8", "example.invalid"):
            with self.subTest(bind=bind):
                with self.assertRaises(firmware_http.FirmwareError):
                    with firmware_http.host(
                        {route: (self.image, self.sha256)}, bind
                    ):
                        self.fail("host must reject a non-RFC1918 bind")

    def test_host_closes_pinned_images_when_server_startup_fails(self):
        opened = []
        open_image = firmware_http._open_image

        def track_open(image):
            fd = open_image(image)
            opened.append(fd)
            return fd

        route = f"/firmware/{self.sha256}.bin"
        with (
            patch.object(firmware_http, "_open_image", track_open),
            patch.object(
                firmware_http.http.server,
                "HTTPServer",
                side_effect=OSError("port unavailable"),
            ),
            self.assertRaises(firmware_http.FirmwareError),
        ):
            with firmware_http.host({route: (self.image, self.sha256)}, "192.168.1.9"):
                self.fail("server startup should fail")
        self.assertEqual(len(opened), 1)
        with self.assertRaises(OSError):
            os.fstat(opened[0])

    def test_host_does_not_track_a_partial_tcp_download(self):
        body = b"\xa5" * (8 * 1024 * 1024)
        self.image.write_bytes(body)
        sha256 = hashlib.sha256(body).hexdigest()
        route = f"/firmware/{sha256}.bin"
        started = threading.Event()
        finished = threading.Event()

        def observe_handler(handler):
            class ObservedHandler(handler):
                def _respond(self, send_body):
                    if send_body:
                        started.set()
                    try:
                        return super()._respond(send_body)
                    finally:
                        finished.set()

            return ObservedHandler

        with self._host({route: (self.image, sha256)}, observe_handler) as server:
            conn = socket.create_connection(server.server_address, timeout=3)
            conn.setsockopt(
                socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
            )
            try:
                conn.sendall(
                    f"GET {route} HTTP/1.1\r\nHost: 192.168.1.9\r\n\r\n".encode()
                )
                self.assertTrue(started.wait(3))
            finally:
                conn.close()
            self.assertTrue(finished.wait(5))
            self.assertNotIn(("127.0.0.1", route), server.completed_downloads)

    def test_server_keeps_headers_with_initial_firmware_wrapper(self):
        payload = b"\xa5" * (192 * 1024)
        body = (
            b"\x12\x34\x00\x00"
            + len(payload).to_bytes(4, "big")
            + hashlib.md5(payload).digest()
            + payload
        )
        self.image.write_bytes(body)
        sha256 = hashlib.sha256(body).hexdigest()
        route = f"/firmware/{sha256}.bin"
        headers_ready = threading.Event()
        release_body = threading.Event()

        def delay_body(handler):
            class DelayedBodyHandler(handler):
                def end_headers(self):
                    super().end_headers()
                    headers_ready.set()
                    release_body.wait()

            return DelayedBodyHandler

        with self._host({route: (self.image, sha256)}, delay_body) as server:
            try:
                with socket.create_connection(server.server_address, timeout=3) as conn:
                    conn.sendall(
                        f"GET {route} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode()
                    )
                    self.assertTrue(headers_ready.wait(3))
                    conn.settimeout(0.1)
                    with self.assertRaises(TimeoutError):
                        conn.recv(1024)
                    release_body.set()
                    conn.settimeout(3)
                    initial = conn.recv(1024)
                    headers, separator, received = initial.partition(b"\r\n\r\n")
                    self.assertEqual(separator, b"\r\n\r\n")
                    self.assertTrue(headers.startswith(b"HTTP/1.1 200 "))
                    self.assertEqual(received[:24], body[:24])
                    received = bytearray(received)
                    while block := conn.recv(65536):
                        received.extend(block)
                    self.assertEqual(received, body)
            finally:
                release_body.set()


if __name__ == "__main__":
    unittest.main()
