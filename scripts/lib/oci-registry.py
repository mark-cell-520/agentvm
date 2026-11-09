#!/usr/bin/env python3
"""Minimal OCI Distribution API registry that serves a locally-downloaded image.

Why this exists
---------------
Some networks cannot reach ghcr.io / Docker Hub at usable speeds (or at all),
but *can* reach api.github.com or a package mirror. In that situation the usual
``tart pull ghcr.io/...`` is hopeless, yet the image bytes are perfectly
obtainable by other means (a friend, a jump host, a download manager).

``tart clone`` does **not** read a local blob cache: it streams OCI layers
straight into ``~/.tart/tmp/<uuid>/disk.img``. So dropping files into Tart's
cache directory does nothing. The only reliable way to feed it an image you
already have on disk is to serve them over HTTP and let Tart pull normally.

This script implements just enough of the OCI Distribution spec for that:

    GET /v2/                        -> 200 (spec-mandated ping)
    GET /v2/<name>/manifests/<ref>  -> the image manifest
    GET /v2/<name>/blobs/<digest>   -> blob bytes (HEAD + Range supported)

Anything else returns a spec-shaped 404. No auth, no push, no tags API.

Layout expected on disk (exactly what ``agentvm-fetch`` produces)::

    <root>/manifest.json          OCI image manifest
    <root>/blobs/sha256/<digest>  one file per referenced object

Only digests referenced by the manifest are served, so a half-populated
directory fails loudly instead of silently serving truncated layers.

Usage
-----
    oci-registry.py [ROOT] [PORT] [--repo NAME]

ROOT defaults to ./image, PORT to 5000, --repo to the repository name Tart
should ask for (default ``debian``). Then::

    tart clone 127.0.0.1:5000/<repo>:latest <vm-name> --insecure
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BLOB_RE = re.compile(r"^/v2/(?P<name>[^/]+)/blobs/(?P<digest>sha256:[0-9a-f]{64})$")
MANIFEST_RE = re.compile(r"^/v2/(?P<name>[^/]+)/manifests/(?P<ref>[^/]+)$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
MEDIA_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
CHUNK = 1 << 20


def load_image(root: Path) -> tuple[bytes, set[str]]:
    """Read the manifest and return (manifest_bytes, set_of_referenced_digests)."""
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        sys.exit(f"error: no manifest.json under {root}")

    raw = manifest_path.read_bytes()
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as exc:
        sys.exit(f"error: manifest.json is not valid JSON: {exc}")

    digests: set[str] = set()
    config = manifest.get("config") or {}
    if config.get("digest"):
        digests.add(config["digest"].removeprefix("sha256:"))
    for layer in manifest.get("layers", []):
        if layer.get("digest"):
            digests.add(layer["digest"].removeprefix("sha256:"))

    if not digests:
        sys.exit("error: manifest references no blobs")
    return raw, digests


class RegistryHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "agentvm-oci-registry"
    root: Path = Path()
    digests: set[str] = set()
    manifest_bytes: bytes = b""
    quiet = False

    def log_message(self, fmt: str, *args) -> None:
        if not self.quiet:
            sys.stderr.write("[oci-registry] %s\n" % (fmt % args))

    # -- helpers ---------------------------------------------------------
    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _blob_path(self, digest: str) -> Path | None:
        hexd = digest.removeprefix("sha256:")
        if not DIGEST_RE.match(hexd) or hexd not in self.digests:
            return None
        path = self.root / "blobs" / "sha256" / hexd
        return path if path.is_file() else None

    def _stream(self, fh, length: int) -> None:
        """Write exactly ``length`` bytes from ``fh`` to the client."""
        remaining = length
        while remaining > 0:
            chunk = fh.read(min(CHUNK, remaining))
            if not chunk:
                break
            try:
                self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                return
            remaining -= len(chunk)

    def _send_file(self, path: Path) -> None:
        size = path.stat().st_size
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng.strip())
            if not m:
                self._json(416, {"errors": [{"code": "BAD_RANGE"}]})
                return
            start_s, end_s = m.group(1), m.group(2)
            # suffix range（`bytes=-1024`，要最后 1 KB）不能当成从头取。
            # 原来 start_s 为空就直接 start = 0，返回的是文件头 1024 字节——
            # 静默给错数据比报错更糟。Tart 用的是正向 Range 所以碰不到，
            # 但任何用 curl -r -1024 调试的人都会拿到错内容。
            if start_s:
                start = int(start_s)
                end = int(end_s) if end_s else size - 1
            else:
                start = size - int(end_s)
                end = size - 1
            end = min(end, size - 1)
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(length))
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            if self.command != "HEAD":
                with path.open("rb") as fh:
                    fh.seek(start)
                    self._stream(fh, length)
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        if self.command != "HEAD":
            with path.open("rb") as fh:
                self._stream(fh, size)

    # -- routes ----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        self._route()

    def do_HEAD(self) -> None:  # noqa: N802
        self._route()

    def _route(self) -> None:
        path = self.path.split("?", 1)[0]

        if path in ("/v2/", "/v2"):
            self._json(200, {})
            return

        m = MANIFEST_RE.match(path)
        if m:
            if m.group("ref") != "latest":
                self._json(404, {"errors": [{"code": "NAME_UNKNOWN"}]})
                return
            self.send_response(200)
            self.send_header("Content-Type", MEDIA_MANIFEST)
            self.send_header("Content-Length", str(len(self.manifest_bytes)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(self.manifest_bytes)
            return

        m = BLOB_RE.match(path)
        if m:
            blob = self._blob_path(m.group("digest"))
            if blob is None:
                self._json(404, {"errors": [{"code": "BLOB_UNKNOWN"}]})
                return
            self._send_file(blob)
            return

        self._json(404, {"errors": [{"code": "NOT_FOUND"}]})


def main() -> int:
    ap = argparse.ArgumentParser(description="Serve a local OCI image over HTTP for Tart.")
    ap.add_argument("root", nargs="?", default="image", help="directory holding manifest.json + blobs/")
    ap.add_argument("port", nargs="?", type=int, default=5000)
    ap.add_argument("--repo", default="debian", help="repository name clients should request")
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    root = Path(args.root).expanduser().resolve()
    manifest_bytes, digests = load_image(root)

    RegistryHandler.root = root
    RegistryHandler.digests = digests
    RegistryHandler.manifest_bytes = manifest_bytes
    RegistryHandler.quiet = args.quiet

    srv = ThreadingHTTPServer((args.bind, args.port), RegistryHandler)
    srv.daemon_threads = True
    print(f"[oci-registry] serving {root} on http://{args.bind}:{args.port}", file=sys.stderr)
    print(f"[oci-registry] {len(digests)} blobs referenced", file=sys.stderr)
    print(f"[oci-registry] tart clone {args.bind}:{args.port}/{args.repo}:latest <vm> --insecure", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[oci-registry] stopped", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
