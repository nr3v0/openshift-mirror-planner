"""An in-memory OCI registry for tests, served through httpx.MockTransport.

Supports bearer-token login (token service on another host), image indexes, gzip and
uncompressed layers, and request recording.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import tarfile
import time

import httpx

TOKEN = "t0ken"


def layer(files: dict[str, bytes | None], compress: bool = True) -> tuple[str, bytes]:
    """A tar layer; a None value adds an empty marker file (for whiteouts)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for path, data in files.items():
            info = tarfile.TarInfo(path)
            payload = data or b""
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    raw = buf.getvalue()
    if compress:
        return "application/vnd.oci.image.layer.v1.tar+gzip", gzip.compress(raw)
    return "application/vnd.oci.image.layer.v1.tar", raw


def catalog_files(*packages: str) -> dict[str, bytes]:
    files = {}
    for pkg in packages:
        objs = [{"schema": "olm.package", "name": pkg, "defaultChannel": "stable"},
                {"schema": "olm.channel", "package": pkg, "name": "stable", "entries": [{"name": f"{pkg}.v1.2.0"}]},
                {"schema": "olm.bundle", "name": f"{pkg}.v1.2.0", "package": pkg, "properties": [
                    {"type": "olm.package", "value": {"packageName": pkg, "version": "1.2.0"}}]}]
        files[f"configs/{pkg}/catalog.json"] = "\n".join(json.dumps(o) for o in objs).encode()
    return files


class FakeRegistry:
    def __init__(self, host: str = "registry.lab.example", user: str = "me", password: str = "secret",
                 require_auth: bool = True, delay: float = 0.0):
        self.host, self.user, self.password = host, user, password
        self.require_auth = require_auth
        self.delay = delay
        self.blobs: dict[str, bytes] = {}
        self.manifests: dict[tuple[str, str], tuple[str, bytes]] = {}
        self.requests: list[str] = []
        self.corrupt: set[str] = set()

    def authfile(self, path) -> str:
        auth = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        path.write_text(json.dumps({"auths": {self.host: {"auth": auth}}}))
        return str(path)

    def _put_blob(self, data: bytes) -> str:
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        self.blobs[digest] = data
        return digest

    def add_image(self, repo: str, tag: str, layers: list[tuple[str, bytes]],
                  label_path: str | None = "/configs", index: bool = True) -> None:
        labels = {"operators.operatorframework.io.index.configs.v1": label_path} if label_path else {}
        config = json.dumps({"architecture": "amd64", "os": "linux", "config": {"Labels": labels}}).encode()
        manifest = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "config": {"mediaType": "application/vnd.oci.image.config.v1+json",
                               "digest": self._put_blob(config), "size": len(config)},
                    "layers": [{"mediaType": media, "digest": self._put_blob(data), "size": len(data)}
                               for media, data in layers]}
        body = json.dumps(manifest).encode()
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        self.manifests[(repo, digest)] = (manifest["mediaType"], body)
        if not index:
            self.manifests[(repo, tag)] = (manifest["mediaType"], body)
            return
        other = json.dumps({"schemaVersion": 2, "layers": [], "config": {"digest": "sha256:" + "0" * 64}}).encode()
        idx = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json", "manifests": [
            {"digest": "sha256:" + hashlib.sha256(other).hexdigest(), "platform": {"os": "linux", "architecture": "arm64"}},
            {"digest": digest, "platform": {"os": "linux", "architecture": "amd64"}}]}
        self.manifests[(repo, tag)] = (idx["mediaType"], json.dumps(idx).encode())

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(f"{request.method} {request.url.host}{request.url.path}")
        if self.delay:
            time.sleep(self.delay)
        if request.url.host == "auth.lab.example":
            expected = "Basic " + base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
            if request.headers.get("authorization") != expected:
                return httpx.Response(401)
            return httpx.Response(200, json={"token": TOKEN})
        if self.require_auth and request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, headers={"www-authenticate":
                'Bearer realm="https://auth.lab.example/token",service="registry",scope="repository:x:pull"'})
        parts = request.url.path.split("/")  # /v2/<repo...>/(manifests|blobs)/<ref>
        kind, ref = parts[-2], parts[-1]
        repo = "/".join(parts[2:-2])
        if kind == "manifests" and (repo, ref) in self.manifests:
            media, body = self.manifests[(repo, ref)]
            return httpx.Response(200, content=body, headers={"content-type": media})
        if kind == "blobs" and ref in self.blobs:
            data = self.blobs[ref]
            return httpx.Response(200, content=data[:-1] + b"X" if ref in self.corrupt else data)
        return httpx.Response(404)
