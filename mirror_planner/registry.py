"""Read files out of a container image straight from its registry, without `oc` or skopeo.

Implements the parts of the OCI distribution API a catalog scan needs: anonymous, basic and
bearer-token authentication (credentials from a pull secret / containers auth file), image
indexes (picks linux/amd64), and layer blobs, streamed, digest-checked and applied in order,
including whiteout files. Only paths under one directory (the catalog's /configs) are written.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import posixpath
import re
import shutil
import tarfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

import httpx

INDEX_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
)
MANIFEST_TYPES = (
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)
ACCEPT = ", ".join(INDEX_TYPES + MANIFEST_TYPES)
CONFIGS_LABEL = "operators.operatorframework.io.index.configs.v1"
SYSTEM_CA_BUNDLES = ("/etc/pki/tls/certs/ca-bundle.crt", "/etc/ssl/certs/ca-certificates.crt")

Progress = Callable[[int, int], None]  # (bytes done, bytes total)


class RegistryError(RuntimeError):
    pass


@dataclass(frozen=True)
class ImageRef:
    registry: str
    repository: str
    reference: str  # tag or sha256:... digest

    @classmethod
    def parse(cls, image: str) -> ImageRef:
        name, digest = (image.split("@", 1) + [""])[:2]
        host, _, path = name.partition("/")
        if not path or not ("." in host or ":" in host or host == "localhost"):
            raise RegistryError(f"{image}: expected registry.example.com/repository:tag")
        tag = ""
        if ":" in path.rsplit("/", 1)[-1]:
            path, tag = path.rsplit(":", 1)
        return cls(host, path, digest or tag or "latest")


def _system_ca() -> str | bool:
    for path in [os.environ.get("SSL_CERT_FILE", "")] + list(SYSTEM_CA_BUNDLES):
        if path and Path(path).is_file():
            return path
    return True  # httpx's bundled certificates


def load_credentials(authfile: str | None, registry: str) -> tuple[str, str] | None:
    """(user, password) for a registry from a pull secret / containers auth.json."""
    if not authfile or not Path(authfile).is_file():
        return None
    auths = json.loads(Path(authfile).read_text()).get("auths", {})
    for key in (registry, f"https://{registry}", f"http://{registry}"):
        entry = auths.get(key)
        if entry and entry.get("auth"):
            user, _, password = base64.b64decode(entry["auth"]).decode().partition(":")
            return user, password
    return None


class RegistryClient:
    def __init__(self, ref: ImageRef, authfile: str | None = None, insecure: bool = False,
                 transport: httpx.BaseTransport | None = None, timeout: float = 120.0):
        self.ref = ref
        self.creds = load_credentials(authfile, ref.registry)
        verify = False if insecure else _system_ca()
        self.http = httpx.Client(base_url=f"https://{ref.registry}/v2/{ref.repository}", verify=verify,
                                 transport=transport, timeout=timeout, follow_redirects=True)
        # token services live on other hosts (e.g. sso.redhat.com); same TLS policy and transport
        self.auth_http = httpx.Client(verify=verify, transport=transport, timeout=60, follow_redirects=True)
        self.token: str | None = None

    def close(self) -> None:
        self.http.close()
        self.auth_http.close()

    # ---------------------------------------------------------------- auth
    def _authorize(self, challenge: str) -> None:
        scheme, _, params = challenge.partition(" ")
        if scheme.lower() == "basic":
            if not self.creds:
                raise RegistryError(f"{self.ref.registry} needs credentials; add them to the pull secret")
            self.http.auth = httpx.BasicAuth(*self.creds)
            return
        fields = dict(re.findall(r'(\w+)="([^"]*)"', params))
        if "realm" not in fields:
            raise RegistryError(f"{self.ref.registry}: unsupported authentication challenge {challenge!r}")
        query = {"service": fields.get("service", ""),
                 "scope": fields.get("scope", f"repository:{self.ref.repository}:pull")}
        r = self.auth_http.get(fields["realm"], params=query,
                               auth=httpx.BasicAuth(*self.creds) if self.creds else None)
        if r.status_code in (401, 403):
            raise RegistryError(f"{self.ref.registry}: login refused; check the pull secret's entry for it")
        r.raise_for_status()
        body = r.json()
        self.token = body.get("token") or body.get("access_token")
        if not self.token:
            raise RegistryError(f"{self.ref.registry}: token service returned no token")

    def _get(self, path: str, accept: str | None = None, stream: bool = False) -> httpx.Response:
        for attempt in range(2):
            headers = {"Accept": accept} if accept else {}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            req = self.http.build_request("GET", path, headers=headers)
            r = self.http.send(req, stream=stream)
            if r.status_code == 401 and attempt == 0 and "www-authenticate" in r.headers:
                r.close()
                self._authorize(r.headers["www-authenticate"])
                continue
            if r.status_code >= 400:
                detail = r.read()[:300].decode(errors="replace") if stream else r.text[:300]
                r.close()
                if r.status_code in (401, 403):
                    raise RegistryError(f"{self.ref.registry}: access denied to {self.ref.repository} "
                                        "(is it in the pull secret?)")
                if r.status_code == 404:
                    raise RegistryError(f"{self.ref.registry}/{self.ref.repository}:{self.ref.reference} not found")
                raise RegistryError(f"GET {path}: HTTP {r.status_code} {detail}")
            return r
        raise RegistryError(f"{self.ref.registry}: authentication failed")

    # ---------------------------------------------------------------- content
    def manifest(self, platform: tuple[str, str] = ("linux", "amd64")) -> dict:
        r = self._get(f"/manifests/{self.ref.reference}", accept=ACCEPT)
        doc = r.json()
        media = doc.get("mediaType") or r.headers.get("content-type", "").split(";")[0]
        if media in INDEX_TYPES or "manifests" in doc:
            entries = doc.get("manifests", [])
            chosen = next((m for m in entries if (m.get("platform", {}).get("os"), m.get("platform", {}).get("architecture"))
                           == platform), entries[0] if entries else None)
            if chosen is None:
                raise RegistryError(f"{self.ref.repository}: image index lists no manifests")
            doc = self._get(f"/manifests/{chosen['digest']}", accept=ACCEPT).json()
        if "layers" not in doc:
            raise RegistryError(f"{self.ref.repository}: unsupported manifest type {media}")
        return doc

    def blob_json(self, digest: str) -> dict:
        return self._get(f"/blobs/{digest}").json()

    def stream_blob(self, digest: str, size: int, progress: Callable[[int], None]) -> Iterator[bytes]:
        """Yield the blob's bytes, checking its sha256 digest at the end."""
        algo, _, expected = digest.partition(":")
        if algo != "sha256":
            raise RegistryError(f"unsupported digest {digest}")
        h = hashlib.sha256()
        r = self._get(f"/blobs/{digest}", stream=True)
        try:
            for chunk in r.iter_bytes(1 << 20):
                h.update(chunk)
                progress(len(chunk))
                yield chunk
        finally:
            r.close()
        if h.hexdigest() != expected:
            raise RegistryError(f"layer {digest[:19]} failed its digest check")


class _ChunkReader:
    """File-like object over an iterator of byte chunks, for tarfile in stream mode."""

    def __init__(self, chunks: Iterator[bytes]):
        self.chunks = chunks
        self.buf = b""

    def read(self, n: int = -1) -> bytes:
        while n < 0 or len(self.buf) < n:
            try:
                self.buf += next(self.chunks)
            except StopIteration:
                break
        if n < 0:
            out, self.buf = self.buf, b""
        else:
            out, self.buf = self.buf[:n], self.buf[n:]
        return out


def _member_path(name: str, prefix: str) -> str | None:
    """Path of a tar member relative to `prefix`, or None when outside it (or unsafe)."""
    path = posixpath.normpath("/" + name.lstrip("./").lstrip("/"))
    if path == prefix:
        return ""
    if not path.startswith(prefix + "/"):
        return None
    rel = path[len(prefix) + 1:]
    return None if rel.startswith("..") else rel


def extract_path(image: str, path: str | None, dest: Path, authfile: str | None = None,
                 insecure: bool = False, progress: Progress | None = None,
                 transport: httpx.BaseTransport | None = None) -> str:
    """Copy one directory of an image's filesystem into dest (like `oc image extract --path`).

    With path=None, the catalog's configs directory is read from the image label (default
    /configs). Returns the directory used. Layers apply in order, honouring whiteouts.
    """
    client = RegistryClient(ImageRef.parse(image), authfile=authfile, insecure=insecure, transport=transport)
    try:
        manifest = client.manifest()
        if path is None:
            config = client.blob_json(manifest["config"]["digest"])
            path = (config.get("config", {}).get("Labels") or {}).get(CONFIGS_LABEL, "/configs")
        prefix = posixpath.normpath("/" + path.strip("/"))
        layers = manifest["layers"]
        total = sum(int(layer.get("size", 0)) for layer in layers)
        done = 0

        def tick(n: int) -> None:
            nonlocal done
            done += n
            if progress:
                progress(done, total)

        dest.mkdir(parents=True, exist_ok=True)
        root = dest.resolve()
        for layer in layers:
            media = layer.get("mediaType", "")
            if "zstd" in media:
                raise RegistryError(f"layer {layer['digest'][:19]} is zstd-compressed, which isn't supported")
            raw = _ChunkReader(client.stream_blob(layer["digest"], int(layer.get("size", 0)), tick))
            stream = gzip.GzipFile(fileobj=raw) if "gzip" in media else raw  # else an uncompressed tar
            try:
                _apply_layer(stream, prefix, root)
            except (tarfile.TarError, EOFError, OSError, zlib.error) as e:
                raise RegistryError(f"layer {layer['digest'][:19]} is unreadable ({e}); corrupt download?") from e
            # drain the rest of the blob so its digest is checked
            while raw.read(1 << 20):
                pass
        return prefix
    finally:
        client.close()


def _apply_layer(stream, prefix: str, root: Path) -> None:
    """Apply one layer's tar stream to root: files under prefix only, whiteouts honoured."""
    with tarfile.open(fileobj=stream, mode="r|") as tar:
        for member in tar:
            rel = _member_path(member.name, prefix)
            if rel is None or rel == "":
                continue
            target = (root / rel).resolve()
            if root not in target.parents and target != root:
                continue  # never write outside dest
            base = posixpath.basename(rel)
            if base == ".wh..wh..opq":  # opaque dir: drop what lower layers put there
                for child in target.parent.iterdir() if target.parent.exists() else ():
                    shutil.rmtree(child) if child.is_dir() else child.unlink()
                continue
            if base.startswith(".wh."):  # whiteout: delete the named file or directory
                victim = target.parent / base[len(".wh."):]
                if victim.is_dir():
                    shutil.rmtree(victim)
                elif victim.exists() or victim.is_symlink():
                    victim.unlink()
                continue
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.is_dir():
                    shutil.rmtree(target)
                src = tar.extractfile(member)
                with open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
            # links and devices are not needed for catalog configs
