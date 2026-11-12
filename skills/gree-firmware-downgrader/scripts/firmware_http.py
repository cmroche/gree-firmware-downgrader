#!/usr/bin/env python3
"""Conservative firmware download preflight and temporary LAN serving."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import http.server
import ipaddress
import json
import os
import re
import socket
import stat
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit


class FirmwareError(Exception):
    """A safe, user-facing firmware operation error."""


_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}\Z")
_MD5_RE = re.compile(r"[0-9a-fA-F]{32}\Z")
_DNS_LABEL_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
_RFC1918 = tuple(
    ipaddress.ip_network(network)
    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
_CHUNK_SIZE = 64 * 1024
_HTTP_TIMEOUT = 15.0
_TOTAL_TIMEOUT = 300.0


def _expected_hash(value):
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise FirmwareError("Expected SHA-256 must be 64 hexadecimal characters.")
    return value.lower()


def _hash_fd(fd):
    sha256 = hashlib.sha256()
    md5 = hashlib.md5()
    size = 0
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
            raise FirmwareError("Firmware image must be a non-empty regular file.")
        os.lseek(fd, 0, os.SEEK_SET)
        while True:
            block = os.read(fd, _CHUNK_SIZE)
            if not block:
                break
            size += len(block)
            sha256.update(block)
            md5.update(block)
        after = os.fstat(fd)
        if (
            size != before.st_size
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
            or after.st_ctime_ns != before.st_ctime_ns
        ):
            raise FirmwareError("Firmware image changed while it was being checked.")
        os.lseek(fd, 0, os.SEEK_SET)
        return {"length": size, "sha256": sha256.hexdigest(), "md5": md5.hexdigest()}
    except OSError:
        raise FirmwareError("Could not read firmware image.") from None


def _open_image(image):
    if not isinstance(image, (str, Path)):
        raise FirmwareError("Firmware image path must be a string or pathlib.Path.")
    try:
        return os.open(os.fspath(image), os.O_RDONLY)
    except (OSError, TypeError, ValueError):
        raise FirmwareError("Could not open firmware image.") from None


def verify_image(image, sha256):
    """Verify a non-empty regular image and return its safe hash metadata."""
    expected = _expected_hash(sha256)
    fd = _open_image(image)
    try:
        info = _hash_fd(fd)
        if info["sha256"] != expected:
            raise FirmwareError("Firmware image SHA-256 does not match.")
        return info
    finally:
        os.close(fd)


def _parse_url(url, allow_lan):
    if not isinstance(url, str):
        raise FirmwareError("Firmware URL must be a string.")
    try:
        encoded = url.encode("ascii")
    except UnicodeEncodeError:
        raise FirmwareError("Firmware URL must contain ASCII characters only.") from None
    if len(encoded) > 159:
        raise FirmwareError("Firmware URL exceeds the 159-byte device limit.")
    if any(
        ord(char) < 0x20 or ord(char) == 0x7F or char.isspace() for char in url
    ):
        raise FirmwareError(
            "Firmware URL must not contain whitespace or control characters."
        )
    if "\\" in url:
        raise FirmwareError("Firmware URL is invalid.")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        explicit_port = parsed.port
    except ValueError:
        raise FirmwareError("Firmware URL is invalid.") from None
    if (
        parsed.scheme.lower() != "http"
        or not parsed.netloc
        or host is None
        or parsed.username is not None
        or parsed.password is not None
        or "@" in parsed.netloc
        or ":" in parsed.netloc
        or explicit_port is not None
        or "?" in url
        or "#" in url
        or "%" in parsed.netloc
    ):
        raise FirmwareError(
            "Firmware URL must be plain HTTP without credentials, port, "
            "query, or fragment."
        )
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if (
            not allow_lan
            or not isinstance(literal, ipaddress.IPv4Address)
            or not any(literal in network for network in _RFC1918)
        ):
            raise FirmwareError(
                "Only an explicitly allowed RFC1918 IPv4 URL may use "
                "a literal address."
            )
        addresses = [str(literal)]
    else:
        labels = host.split(".")
        official = host.lower() == "gree.com" or host.lower().endswith(".gree.com")
        if not official or not all(
            _DNS_LABEL_RE.fullmatch(label) for label in labels
        ):
            raise FirmwareError("Public firmware URLs must use a valid gree.com host.")
        try:
            records = socket.getaddrinfo(host, 80, type=socket.SOCK_STREAM)
        except OSError:
            raise FirmwareError("Could not resolve firmware host.") from None
        addresses = []
        for family, _socktype, _proto, _canonname, sockaddr in records:
            if family != socket.AF_INET:
                raise FirmwareError(
                    "Firmware host must resolve only to public IPv4 addresses."
                )
            try:
                address = ipaddress.IPv4Address(sockaddr[0])
            except (ValueError, IndexError):
                raise FirmwareError(
                    "Firmware host returned an invalid IP address."
                ) from None
            if not address.is_global:
                raise FirmwareError(
                    "Firmware host must resolve only to public IPv4 addresses."
                )
            value = str(address)
            if value not in addresses:
                addresses.append(value)
        if not addresses:
            raise FirmwareError("Firmware host did not resolve to an IPv4 address.")
    path = parsed.path or "/"
    if not path.startswith("/"):
        raise FirmwareError("Firmware URL path is invalid.")
    return parsed.netloc, path, addresses


def _pace(last_start, deadline):
    now = time.monotonic()
    if now >= deadline:
        raise FirmwareError("Firmware preflight exceeded its time limit.")
    if last_start is not None and now - last_start < 1.0:
        delay = 1.0 - (now - last_start)
        if now + delay >= deadline:
            raise FirmwareError("Firmware preflight exceeded its time limit.")
        time.sleep(delay)
    return time.monotonic()


def _single_header(response, name, required=False):
    values = response.getheaders()
    matches = [
        value.strip() for key, value in values if key.lower() == name.lower()
    ]
    if not matches and required:
        raise FirmwareError("Firmware server returned invalid response headers.")
    if len(matches) > 1:
        raise FirmwareError("Firmware server returned invalid response headers.")
    return matches[0] if matches else None


def _fetch(
    connection_ip,
    request_path,
    host_header,
    expected,
    local_md5,
    deadline,
    last_start,
):
    conn = http.client.HTTPConnection(connection_ip, 80, timeout=_HTTP_TIMEOUT)
    try:
        last_start = _pace(last_start, deadline)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FirmwareError("Firmware preflight exceeded its time limit.")
        conn.timeout = min(_HTTP_TIMEOUT, remaining)
        conn.request(
            "GET",
            request_path,
            headers={
                "Host": host_header,
                "Accept-Encoding": "identity",
                "Connection": "close",
            },
        )
        response = conn.getresponse()
        if response.status != 200:
            raise FirmwareError("Firmware server did not return HTTP 200.")
        content_length = _single_header(response, "Content-Length", required=True)
        if (
            not re.fullmatch(r"[0-9]+", content_length)
            or int(content_length) != expected["length"]
        ):
            raise FirmwareError("Firmware server returned an unexpected content length.")
        transfer_encoding = _single_header(response, "Transfer-Encoding")
        if transfer_encoding is not None:
            raise FirmwareError("Firmware server used an unsupported transfer encoding.")
        content_encoding = _single_header(response, "Content-Encoding")
        if content_encoding and content_encoding.lower() != "identity":
            raise FirmwareError("Firmware server returned compressed firmware.")
        content_type = _single_header(response, "Content-Type")
        if content_type and content_type.split(";", 1)[0].strip().lower() in (
            "text/html",
            "application/xhtml+xml",
        ):
            raise FirmwareError("Firmware server returned an HTML page, not firmware.")
        file_md5 = _single_header(response, "FileMd5")
        if file_md5 is not None and not _MD5_RE.fullmatch(file_md5):
            raise FirmwareError("Firmware server returned an invalid FileMd5 header.")

        sha = hashlib.sha256()
        md5 = hashlib.md5()
        sample = bytearray()
        total = 0
        while total < expected["length"]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FirmwareError("Firmware preflight exceeded its time limit.")
            if conn.sock is not None:
                conn.sock.settimeout(min(_HTTP_TIMEOUT, remaining))
            block = response.read(min(_CHUNK_SIZE, expected["length"] - total))
            if not block:
                raise FirmwareError("Firmware server returned an incomplete image.")
            total += len(block)
            sha.update(block)
            md5.update(block)
            if len(sample) < 512:
                sample.extend(block[: 512 - len(sample)])
        if response.read(1):
            raise FirmwareError("Firmware server returned more data than declared.")
        if bytes(sample).lstrip(b"\xef\xbb\xbf \t\r\n").lower().startswith(
            (b"<html", b"<!doctype html")
        ):
            raise FirmwareError("Firmware server returned an HTML page, not firmware.")
        actual_sha = sha.hexdigest()
        actual_md5 = md5.hexdigest()
        if actual_sha != expected["sha256"]:
            raise FirmwareError("Firmware server image SHA-256 does not match.")
        if actual_md5 != local_md5 or (
            file_md5 is not None and file_md5.lower() != actual_md5
        ):
            raise FirmwareError("Firmware server FileMd5 does not match the image.")
        return file_md5.lower() if file_md5 else None, last_start
    except FirmwareError:
        raise
    except (OSError, http.client.HTTPException):
        raise FirmwareError("Firmware download request failed or timed out.") from None
    finally:
        conn.close()


def preflight(url, image, sha256, *, allow_lan=False):
    """Verify a firmware URL serves the exact approved image from pinned IPs."""
    expected_hash = _expected_hash(sha256)
    info = verify_image(image, expected_hash)
    host, request_path, addresses = _parse_url(url, allow_lan)
    expected = {"length": info["length"], "sha256": expected_hash}
    deadline = time.monotonic() + _TOTAL_TIMEOUT
    last_start = None
    checked_md5 = []
    optional_md5, last_start = _fetch(
        addresses[0],
        request_path,
        host,
        expected,
        info["md5"],
        deadline,
        last_start,
    )
    if optional_md5 is not None:
        checked_md5.append(optional_md5)
    for address in addresses:
        optional_md5, last_start = _fetch(
            address,
            request_path,
            address,
            expected,
            info["md5"],
            deadline,
            last_start,
        )
        if optional_md5 is not None and optional_md5 not in checked_md5:
            checked_md5.append(optional_md5)
    return {
        "length": info["length"],
        "sha256": expected_hash,
        "resolved_ips": addresses,
        "file_md5": checked_md5,
    }


def _is_rfc1918(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return isinstance(address, ipaddress.IPv4Address) and any(
        address in network for network in _RFC1918
    )


def _handler_for(fd, info, expected_hash):
    class FirmwareRequestHandler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_request(self, code="-", size="-"):
            target = "/firmware.bin" if self.path == "/firmware.bin" else "<rejected>"
            timestamp = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
            print(
                f"{timestamp} {self.client_address[0]} {target} {code}",
                file=sys.stderr,
            )

        def _respond(self, body):
            self.close_connection = True
            if self.path != "/firmware.bin":
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                return
            try:
                current = _hash_fd(fd)
            except FirmwareError:
                current = None
            if (
                current is None
                or current["sha256"] != expected_hash
                or current["length"] != info["length"]
            ):
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(info["length"]))
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("FileMd5", current["md5"])
            self.send_header("Connection", "close")
            self.end_headers()
            if body:
                sha = hashlib.sha256()
                md5 = hashlib.md5()
                offset = 0
                os.lseek(fd, 0, os.SEEK_SET)
                while offset < info["length"]:
                    block = os.read(fd, min(_CHUNK_SIZE, info["length"] - offset))
                    if not block:
                        self.close_connection = True
                        return
                    offset += len(block)
                    sha.update(block)
                    md5.update(block)
                    self.wfile.write(block)
                if (
                    sha.hexdigest() != expected_hash
                    or md5.hexdigest() != current["md5"]
                ):
                    self.close_connection = True

        def do_GET(self):
            self._respond(True)

        def do_HEAD(self):
            self._respond(False)

    return FirmwareRequestHandler


def serve(image, sha256, bind):
    """Serve only the verified image on fixed HTTP port 80 until interrupted."""
    expected_hash = _expected_hash(sha256)
    fd = _open_image(image)
    try:
        info = _hash_fd(fd)
        if info["sha256"] != expected_hash:
            raise FirmwareError("Firmware image SHA-256 does not match.")
        if not _is_rfc1918(bind):
            raise FirmwareError("Bind address must be a literal RFC1918 IPv4 address.")
        try:
            server = http.server.HTTPServer(
                (str(ipaddress.IPv4Address(bind)), 80),
                _handler_for(fd, info, expected_hash),
            )
        except OSError as error:
            raise FirmwareError(
                "Could not bind the approved LAN address on port 80; "
                "no privilege or firewall changes were attempted."
            ) from error
        print(
            "Keep this process running throughout the install; "
            "press Ctrl-C to stop.",
            file=sys.stderr,
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    finally:
        os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Preflight and serve an exact Gree firmware image."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser(
        "preflight", help="verify a firmware URL against the local image"
    )
    check.add_argument("--url", required=True)
    check.add_argument("--image", required=True)
    check.add_argument("--sha256", required=True)
    check.add_argument("--allow-lan", action="store_true")
    serve_parser = commands.add_parser(
        "serve", help="serve the verified image on LAN port 80"
    )
    serve_parser.add_argument("--image", required=True)
    serve_parser.add_argument("--sha256", required=True)
    serve_parser.add_argument("--bind", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "preflight":
            result = preflight(
                args.url, args.image, args.sha256, allow_lan=args.allow_lan
            )
            print(json.dumps(result, sort_keys=True))
        else:
            serve(args.image, args.sha256, args.bind)
    except FirmwareError as error:
        parser.exit(1, f"firmware_http.py: error: {error}\n")


if __name__ == "__main__":
    main()
