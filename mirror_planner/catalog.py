"""Operator catalog indexes: extract, parse and summarize file-based catalogs (FBC).

`oc image extract <index> --path /configs/:<dir>` gives one directory per package holding
olm.package, olm.channel and olm.bundle objects as JSON streams or YAML. This module turns
that into a compact summary per package: channels with their head versions, display
metadata from the default channel's head bundle, the disconnected annotation, and
dependencies, so the mirror UI can search and resolve without re-reading 100+ MB of JSON.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import yaml
from pydantic import BaseModel, Field

DEFAULT_CATALOGS = (
    "redhat-operator-index",
    "certified-operator-index",
    "community-operator-index",
)


def catalog_image(name: str, ocp_version: str) -> str:
    """registry.redhat.io/redhat/<name>:v<major>.<minor>"""
    minor = ".".join(ocp_version.split(".")[:2])
    return f"registry.redhat.io/redhat/{name}:v{minor}"


IMAGE_REF = re.compile(
    r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]+)?"        # registry host[:port]
    r"(/[a-z0-9]+([._-][a-z0-9]+)*)+"                    # repository path
    r"(:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}|@sha256:[0-9a-f]{64})$")  # tag or digest


def valid_image_ref(image: str) -> bool:
    """Fully qualified image reference with a tag or digest, e.g. quay.example.com/ops/index:v1."""
    if not IMAGE_REF.match(image):
        return False
    host = image.split("/")[0]
    return "." in host or ":" in host or host == "localhost"  # a registry host, not a Docker Hub path


def catalog_slug(image: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", image.split("/")[-1].replace(":", "-"))


# --------------------------------------------------------------------------- models


SUMMARY_SCHEMA = 2  # bump when Package/Channel gain fields; older summaries need a rescan


class Channel(BaseModel):
    name: str
    head: str  # head bundle version
    versions: list[str] = Field(default_factory=list)  # newest first
    must_gather: list[str] = Field(default_factory=list)  # images from the head bundle


class Package(BaseModel):
    name: str
    display_name: str | None = None
    description: str | None = None
    provider: str | None = None
    keywords: list[str] = Field(default_factory=list)
    default_channel: str
    channels: list[Channel]
    disconnected: bool | None = None  # None: the bundle doesn't say
    infrastructure_features: list[str] = Field(default_factory=list)
    requires_packages: list[str] = Field(default_factory=list)
    requires_gvks: list[str] = Field(default_factory=list)  # group/version/kind
    provides_gvks: list[str] = Field(default_factory=list)
    deprecated: bool = False

    def channel(self, name: str) -> Channel | None:
        return next((c for c in self.channels if c.name == name), None)


class CatalogSummary(BaseModel):
    image: str
    extracted_at: str
    packages: list[Package]
    schema_version: int = 1

    @property
    def stale(self) -> bool:
        return self.schema_version < SUMMARY_SCHEMA

    def package(self, name: str) -> Package | None:
        return next((p for p in self.packages if p.name == name), None)


# --------------------------------------------------------------------------- parsing


def _json_stream(text: str) -> Iterator[dict[str, Any]]:
    dec = json.JSONDecoder()
    i, n = 0, len(text)
    while True:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            return
        obj, i = dec.raw_decode(text, i)
        if isinstance(obj, list):
            yield from obj
        else:
            yield obj


def read_objects(package_dir: Path) -> Iterator[dict[str, Any]]:
    for f in sorted(package_dir.rglob("*")):
        if not f.is_file():
            continue
        if f.suffix == ".json":
            yield from _json_stream(f.read_text())
        elif f.suffix in (".yaml", ".yml"):
            yield from (d for d in yaml.safe_load_all(f.read_text()) if isinstance(d, dict))


def version_key(v: str) -> tuple:
    """Sort key for semver-ish strings; pre-releases sort before releases."""
    core, _, pre = v.lstrip("v").partition("-")
    nums = tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.+]", core))
    return nums + ((1,) if not pre else (0, pre))


def _short_description(text: str | None, limit: int = 280) -> str | None:
    if not text:
        return None
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if para.startswith("#"):  # headings are titles, not descriptions
            continue
        if para.startswith(("![", "<", "|", "```")):
            continue
        para = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", para)  # [text](url) -> text
        para = re.sub(r"(\*\*|__|`)", "", para)
        para = re.sub(r"\s+", " ", para).strip()
        if para:
            return para if len(para) <= limit else para[: limit - 1].rstrip() + "…"
    return None


MUST_GATHER_ANNOTATION = "operators.openshift.io/must-gather-image"
_MUST_GATHER = re.compile(r"must-?gather", re.I)


def must_gather_images(bundle: dict[str, Any]) -> list[str]:
    """Must-gather images a bundle declares: the console's CSV annotation, then relatedImages
    entries named like a must-gather. Digest-pinned references as published."""
    found: list[str] = []
    for p in bundle.get("properties", []):
        if p.get("type") == "olm.csv.metadata":
            img = (p["value"].get("annotations") or {}).get(MUST_GATHER_ANNOTATION)
            if img:
                found.append(img.strip())
    for rel in bundle.get("relatedImages", []) or []:
        img = rel.get("image", "")
        if img and _MUST_GATHER.search(rel.get("name", "") + " " + img):
            found.append(img)
    return list(dict.fromkeys(found))


def _gvk(v: dict[str, Any]) -> str:
    return f"{v.get('group', '')}/{v.get('version', '')}/{v.get('kind', '')}"


def parse_package(package_dir: Path) -> Package | None:
    pkg: dict[str, Any] | None = None
    channels: list[dict[str, Any]] = []
    bundles: dict[str, dict[str, Any]] = {}
    deprecated = False
    for o in read_objects(package_dir):
        schema = o.get("schema")
        if schema == "olm.package":
            pkg = o
        elif schema == "olm.channel":
            channels.append(o)
        elif schema == "olm.bundle":
            bundles[o["name"]] = o
        elif schema == "olm.deprecations":
            deprecated = any(e.get("reference", {}).get("schema") == "olm.package"
                             for e in o.get("entries", []))
    if pkg is None or not channels:
        return None

    def bundle_version(name: str) -> str:
        for p in bundles.get(name, {}).get("properties", []):
            if p.get("type") == "olm.package":
                return p["value"]["version"]
        return name.rsplit(".v", 1)[-1]

    chans: list[Channel] = []
    heads: dict[str, str] = {}
    for ch in channels:
        entries = ch.get("entries", [])
        replaced = {e.get("replaces") for e in entries} | {s for e in entries for s in e.get("skips", [])}
        candidates = [e["name"] for e in entries if e["name"] not in replaced] or [e["name"] for e in entries]
        head = max(candidates, key=lambda b: version_key(bundle_version(b)))
        heads[ch["name"]] = head
        versions = sorted({bundle_version(e["name"]) for e in entries}, key=version_key, reverse=True)
        chans.append(Channel(name=ch["name"], head=bundle_version(head), versions=versions,
                             must_gather=must_gather_images(bundles.get(head, {}))))
    chans.sort(key=lambda c: version_key(c.head), reverse=True)

    default = pkg.get("defaultChannel") or chans[0].name
    head_bundle = bundles.get(heads.get(default, ""), {})
    meta: dict[str, Any] = {}
    req_pkgs: list[str] = []
    req_gvks: list[str] = []
    prov_gvks: list[str] = []
    for p in head_bundle.get("properties", []):
        t, v = p.get("type"), p.get("value", {})
        if t == "olm.csv.metadata":
            meta = v
        elif t == "olm.package.required":
            req_pkgs.append(v["packageName"])
        elif t == "olm.gvk.required":
            req_gvks.append(_gvk(v))
        elif t == "olm.gvk":
            prov_gvks.append(_gvk(v))

    ann = meta.get("annotations", {}) or {}
    disconnected: bool | None = None
    if "features.operators.openshift.io/disconnected" in ann:
        disconnected = ann["features.operators.openshift.io/disconnected"] == "true"
    infra: list[str] = []
    try:
        infra = json.loads(ann.get("operators.openshift.io/infrastructure-features", "[]"))
    except (ValueError, TypeError):
        pass
    if disconnected is None and infra:
        disconnected = any(str(f).lower() == "disconnected" for f in infra)

    return Package(
        name=pkg["name"],
        display_name=meta.get("displayName"),
        description=_short_description(meta.get("description")),
        provider=(meta.get("provider") or {}).get("name"),
        keywords=meta.get("keywords") or [],
        default_channel=default,
        channels=chans,
        disconnected=disconnected,
        infrastructure_features=[str(f) for f in infra],
        requires_packages=sorted(set(req_pkgs)),
        requires_gvks=sorted(set(req_gvks)),
        provides_gvks=sorted(set(prov_gvks)),
        deprecated=deprecated,
    )


Progress = Callable[[str, float | None, str], None]  # (phase, fraction or None, detail)


def parse_configs(configs_dir: Path, image: str, progress: Progress | None = None) -> CatalogSummary:
    dirs = [d for d in sorted(configs_dir.iterdir()) if d.is_dir()]
    packages = []
    for i, d in enumerate(dirs, 1):
        if (p := parse_package(d)):
            packages.append(p)
        if progress and (i % 10 == 0 or i == len(dirs)):
            progress("parsing", i / len(dirs), f"{i} of {len(dirs)} packages")
    return CatalogSummary(image=image, extracted_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                          packages=packages, schema_version=SUMMARY_SCHEMA)


# --------------------------------------------------------------------------- cache


class CatalogCache:
    """<cache>/<slug>/summary.json, refreshed from the registry with `oc image extract`."""

    def __init__(self, cache_dir: Path, oc: str = "oc", authfile: str | None = None):
        self.dir = cache_dir
        self.oc = oc
        self.authfile = authfile

    def summary_path(self, image: str) -> Path:
        return self.dir / catalog_slug(image) / "summary.json"

    def load(self, image: str) -> CatalogSummary | None:
        p = self.summary_path(image)
        return CatalogSummary.model_validate_json(p.read_text()) if p.exists() else None

    def forget(self, image: str) -> None:
        shutil.rmtree(self.dir / catalog_slug(image), ignore_errors=True)

    def refresh(self, image: str, insecure: bool = False, progress: Progress | None = None) -> CatalogSummary:
        """Extract the catalog's /configs and summarize it. `progress` gets the extracted size
        while `oc image extract` runs (its total isn't known in advance), then parse progress."""
        configs = self.dir / catalog_slug(image) / "configs"
        shutil.rmtree(configs, ignore_errors=True)
        configs.mkdir(parents=True)
        cmd = [self.oc, "image", "extract", image, "--path", f"/configs/:{configs}", "--confirm"]
        if self.authfile:
            cmd += ["-a", self.authfile]
        if insecure:
            cmd += ["--insecure=true"]
        if progress:
            progress("extracting", None, "contacting registry")
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        while proc.poll() is None:
            if progress:
                size = sum(f.stat().st_size for f in configs.rglob("*") if f.is_file())
                if size:
                    progress("extracting", None, f"{size / 1e6:.0f} MB extracted")
            time.sleep(0.5)
        stderr = proc.stderr.read() if proc.stderr else ""
        if proc.returncode != 0:
            raise RuntimeError(f"oc image extract {image} failed: {stderr.strip()[-500:]}")
        summary = parse_configs(configs, image, progress)
        self.summary_path(image).write_text(summary.model_dump_json())
        shutil.rmtree(configs)  # the summary is all later reads need
        return summary


# --------------------------------------------------------------------------- dependencies


def resolve_dependencies(summary: CatalogSummary, selected: list[str],
                         companions: dict[str, list[str]] | None = None) -> dict[str, list[str]]:
    """Packages that selected packages need, within the same catalog.

    Returns {dependency package: [packages that need it]}. Uses olm.package.required and
    olm.gvk.required on each default channel head; GVKs map to the package providing them.
    `companions` adds relationships the catalog doesn't declare, such as an operator that
    creates its own subscriptions at install time (odf-operator -> odf-dependencies).
    """
    companions = companions or {}
    providers: dict[str, str] = {}
    for p in summary.packages:
        for g in p.provides_gvks:
            providers.setdefault(g, p.name)
    needed: dict[str, list[str]] = {}
    queue = list(selected)
    seen = set(selected)
    while queue:
        name = queue.pop()
        pkg = summary.package(name)
        if pkg is None:
            continue
        deps = set(pkg.requires_packages) | set(companions.get(name, []))
        deps |= {providers[g] for g in pkg.requires_gvks if g in providers}
        deps.discard(name)
        for d in sorted(deps):
            if d not in selected:
                needed.setdefault(d, []).append(name)
            if d not in seen:
                seen.add(d)
                queue.append(d)
    return needed
