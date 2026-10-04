"""Mirror plan (what to mirror) and its oc-mirror v2 ImageSetConfiguration.

The plan holds only what the operator chose: platform release, selected packages and
channels, extra images. Dependencies are derived from the catalog summaries at render time,
so they follow the catalogs when those are refreshed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from .catalog import CatalogSummary, resolve_dependencies

PRESETS_FILE = Path(__file__).parent / "presets.yaml"


class Platform(BaseModel):
    channel: str = "stable-4.22"
    min_version: str | None = None
    max_version: str | None = None
    graph: bool = True  # graph data image for the OpenShift Update Service


class SelectedPackage(BaseModel):
    name: str
    channels: list[str] = Field(default_factory=list)  # empty: the package's default channel
    reason: str = "selected"  # "selected" or "preset <id>"


class CatalogSelection(BaseModel):
    image: str
    packages: list[SelectedPackage] = Field(default_factory=list)

    def get(self, name: str) -> SelectedPackage | None:
        return next((p for p in self.packages if p.name == name), None)


class ExtraCatalog(BaseModel):
    """An operator index added in the planner beyond the Red Hat, certified and community ones."""
    image: str
    insecure: bool = False  # skip TLS verification when scanning (lab registries)


class MirrorPlan(BaseModel):
    platform: Platform = Platform()
    extra_catalogs: list[ExtraCatalog] = Field(default_factory=list)
    catalogs: list[CatalogSelection] = Field(default_factory=list)
    additional_images: list[str] = Field(default_factory=list)
    archive_size_gb: int | None = None

    def catalog(self, image: str, create: bool = False) -> CatalogSelection | None:
        c = next((c for c in self.catalogs if c.image == image), None)
        if c is None and create:
            c = CatalogSelection(image=image)
            self.catalogs.append(c)
        return c

    def add(self, image: str, name: str, channels: list[str] | None = None, reason: str = "selected") -> None:
        cat = self.catalog(image, create=True)
        have = cat.get(name)
        if have:
            if channels:
                have.channels = channels
            return
        cat.packages.append(SelectedPackage(name=name, channels=channels or [], reason=reason))
        cat.packages.sort(key=lambda p: p.name)

    def remove(self, image: str, name: str) -> None:
        cat = self.catalog(image)
        if cat:
            cat.packages = [p for p in cat.packages if p.name != name]
            if not cat.packages:
                self.catalogs.remove(cat)

    @classmethod
    def load(cls, path: Path) -> MirrorPlan:
        return cls.model_validate(yaml.safe_load(path.read_text()) or {}) if path.exists() else cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(self.model_dump(exclude_none=True), sort_keys=False))


def load_presets(path: Path = PRESETS_FILE) -> dict[str, Any]:
    return yaml.safe_load(path.read_text())


def dependencies(plan: MirrorPlan, summaries: dict[str, CatalogSummary],
                 companions: dict[str, list[str]]) -> dict[str, dict[str, list[str]]]:
    """{catalog image: {dependency package: [packages needing it]}}"""
    out: dict[str, dict[str, list[str]]] = {}
    for cat in plan.catalogs:
        summary = summaries.get(cat.image)
        if summary:
            deps = resolve_dependencies(summary, [p.name for p in cat.packages], companions)
            if deps:
                out[cat.image] = deps
    return out


def mirrored_packages(plan: MirrorPlan, summaries: dict[str, CatalogSummary],
                      companions: dict[str, list[str]]) -> dict[str, list[tuple[str, list[str]]]]:
    """{catalog image: [(package, channels)]} for selected packages and their dependencies.
    An empty channel choice resolves to the package's default channel when the catalog is scanned."""
    deps = dependencies(plan, summaries, companions)
    out: dict[str, list[tuple[str, list[str]]]] = {}
    for cat in plan.catalogs:
        summary = summaries.get(cat.image)
        entries = [(p.name, p.channels) for p in cat.packages]
        entries += [(name, []) for name in sorted(deps.get(cat.image, {}))]
        resolved = []
        for name, chosen in sorted(entries):
            pkg = summary.package(name) if summary else None
            default = pkg.default_channel if pkg else None
            resolved.append((name, chosen or ([default] if default else [])))
        out[cat.image] = resolved
    return out


def automatic_images(plan: MirrorPlan, summaries: dict[str, CatalogSummary], companions: dict[str, list[str]],
                     base_images: list[str]) -> list[tuple[str, str]]:
    """[(image, why)]: the base images, then the must-gather image of every mirrored operator
    channel, in order and without duplicates."""
    seen: dict[str, str] = {img: "base image" for img in base_images}
    for image, packages in mirrored_packages(plan, summaries, companions).items():
        summary = summaries.get(image)
        for name, channels in packages:
            pkg = summary.package(name) if summary else None
            for ch in (pkg.channel(c) for c in channels) if pkg else ():
                for mg in ch.must_gather if ch else ():
                    seen.setdefault(mg, f"must-gather for {name} ({ch.name})")
    return list(seen.items())


def render(plan: MirrorPlan, summaries: dict[str, CatalogSummary],
           companions: dict[str, list[str]], base_images: list[str] | None = None) -> dict[str, Any]:
    """oc-mirror v2 ImageSetConfiguration. Channels without versions mirror only the head."""
    channel: dict[str, Any] = {"name": plan.platform.channel, "type": "ocp"}
    if plan.platform.min_version:
        channel["minVersion"] = plan.platform.min_version
    if plan.platform.max_version:
        channel["maxVersion"] = plan.platform.max_version
    mirror: dict[str, Any] = {"platform": {"channels": [channel], "graph": plan.platform.graph}}

    operators = []
    for image, entries in mirrored_packages(plan, summaries, companions).items():
        summary = summaries.get(image)
        packages = []
        for name, chans in entries:
            pkg = summary.package(name) if summary else None
            default = pkg.default_channel if pkg else None
            entry: dict[str, Any] = {"name": name}
            if chans:
                entry["channels"] = [{"name": c} for c in chans]
            if default and chans and default not in chans:
                # oc-mirror v2 needs the default channel, or an explicit replacement for it
                entry["defaultChannel"] = chans[0]
            packages.append(entry)
        if packages:
            operators.append({"catalog": image, "packages": packages})
    if operators:
        mirror["operators"] = operators
    auto = [img for img, _ in automatic_images(plan, summaries, companions, base_images or [])]
    images = list(dict.fromkeys(auto + plan.additional_images))
    if images:
        mirror["additionalImages"] = [{"name": i} for i in images]

    isc: dict[str, Any] = {"kind": "ImageSetConfiguration", "apiVersion": "mirror.openshift.io/v2alpha1"}
    if plan.archive_size_gb:
        isc["archiveSize"] = plan.archive_size_gb
    isc["mirror"] = mirror
    return isc


def write(plan: MirrorPlan, summaries: dict[str, CatalogSummary], companions: dict[str, list[str]],
          mirror_dir: Path, base_images: list[str] | None = None) -> Path:
    plan.save(mirror_dir / "plan.yaml")
    # extra oc-mirror flags the plan implies; `make mirror` reads this file
    flags = ["--src-tls-verify=false"] if any(e.insecure for e in plan.extra_catalogs) else []
    (mirror_dir / "oc-mirror.flags").write_text(" ".join(flags) + "\n")
    out = mirror_dir / "imageset-config.yaml"
    out.write_text("# Generated by `fh mirror-ui` / `make imageset` from plan.yaml; edit the plan, not this file.\n"
                   + yaml.safe_dump(render(plan, summaries, companions, base_images), sort_keys=False))
    return out


# --------------------------------------------------------------------------- import

ISC_API_VERSIONS = ("mirror.openshift.io/v2alpha1", "mirror.openshift.io/v1alpha2")


class ISCImportError(ValueError):
    """The file isn't an ImageSetConfiguration the planner can read."""


def import_isc(text: str) -> tuple[MirrorPlan, list[str]]:
    """Build a plan from an ImageSetConfiguration. Returns the plan and warnings about content the
    plan can't represent. additionalImages are all kept here; the caller drops the ones the planner
    adds automatically (base images, must-gathers)."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ISCImportError(f"not valid YAML: {e}") from e
    if not isinstance(doc, dict) or doc.get("kind") != "ImageSetConfiguration":
        raise ISCImportError("not an ImageSetConfiguration (kind: ImageSetConfiguration is missing)")
    warnings: list[str] = []
    api = doc.get("apiVersion", "")
    if api not in ISC_API_VERSIONS:
        warnings.append(f"apiVersion {api or '(none)'} is not one the planner knows; read as v2alpha1")
    if "storageConfig" in doc:
        warnings.append("storageConfig (oc-mirror v1) ignored; v2 keeps state in its workspace")

    mirror = doc.get("mirror") or {}
    plan = MirrorPlan()
    if doc.get("archiveSize"):
        plan.archive_size_gb = int(doc["archiveSize"])

    platform = mirror.get("platform") or {}
    channels = platform.get("channels") or []
    if channels:
        first = channels[0]
        plan.platform = Platform(channel=first["name"], min_version=first.get("minVersion"),
                                 max_version=first.get("maxVersion"), graph=bool(platform.get("graph", False)))
        if first.get("type", "ocp") != "ocp":
            warnings.append(f"platform channel type {first['type']} read as ocp")
        for extra in channels[1:]:
            warnings.append(f"platform channel {extra.get('name')} ignored; the planner mirrors one channel")
        if first.get("shortestPath") or first.get("full"):
            warnings.append(f"shortestPath/full on {first['name']} ignored")
    elif platform:
        warnings.append("platform section has no channels; using the default release")

    for op in mirror.get("operators") or []:
        image = op.get("catalog")
        if not image:
            warnings.append("an operators entry without catalog was skipped")
            continue
        for key in ("full", "targetCatalog", "targetTag", "skipDependencies", "originalRef"):
            if op.get(key) not in (None, False):
                warnings.append(f"{key} on {image} ignored")
        packages = op.get("packages") or []
        if not packages:
            warnings.append(f"{image} lists no packages (it would mirror the whole catalog); nothing selected from it")
            plan.catalog(image, create=True)
        for pkg in packages:
            chans = []
            for ch in pkg.get("channels") or []:
                chans.append(ch["name"])
                if ch.get("minVersion") or ch.get("maxVersion"):
                    warnings.append(f"{pkg['name']} {ch['name']}: version range ignored; the head is mirrored")
            if pkg.get("minVersion") or pkg.get("maxVersion"):
                warnings.append(f"{pkg['name']}: version range ignored; channel heads are mirrored")
            plan.add(image, pkg["name"], chans or None, reason="imported")

    plan.additional_images = [i["name"] if isinstance(i, dict) else str(i) for i in mirror.get("additionalImages") or []]
    for key in ("helm", "blockedImages", "samples"):
        if mirror.get(key):
            warnings.append(f"mirror.{key} ignored; the planner doesn't manage it")
    if doc.get("delete"):
        warnings.append("delete section ignored")
    return plan, warnings


def fold_dependencies(plan: MirrorPlan, summaries: dict[str, CatalogSummary],
                      companions: dict[str, list[str]]) -> list[str]:
    """Turn imported selections that are just dependencies back into automatic ones.

    A package stops being a selection when the packages still selected pull it in anyway and it
    is mirrored on its default channel, so the rendered ImageSetConfiguration stays the same.
    Packages are tried one at a time, in name order, so a dependency loop keeps one member as a
    selection. Catalogs that aren't scanned are left alone. Returns the folded package names.
    """
    folded: list[str] = []
    for cat in plan.catalogs:
        summary = summaries.get(cat.image)
        if summary is None:
            continue
        for pkg in sorted(cat.packages, key=lambda p: p.name):
            info = summary.package(pkg.name)
            if info is None or (pkg.channels and pkg.channels != [info.default_channel]):
                continue  # unknown here, or a deliberate channel choice: keep it selected
            others = [p.name for p in cat.packages if p.name != pkg.name]
            if pkg.name in resolve_dependencies(summary, others, companions):
                cat.packages.remove(pkg)
                folded.append(pkg.name)
    plan.catalogs = [c for c in plan.catalogs if c.packages or c.image not in summaries]
    return folded
