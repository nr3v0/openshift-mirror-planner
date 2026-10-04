"""mirror-planner command line.

    mirror-planner serve      [--mirror-dir DIR] [--listen ADDR] [--port 8088]   web UI
    mirror-planner imageset   [--mirror-dir DIR]                                 render imageset-config.yaml from plan.yaml
    mirror-planner mirror     [--mirror-dir DIR] [--cache-dir DIR] [--dry-run]   oc-mirror --v2 to disk

Defaults come from the environment, which the container image sets:
    MIRROR_DIR           plan.yaml, imageset-config.yaml, catalog scans and the bundle   (./mirror)
    OC_MIRROR_CACHE      oc-mirror's image cache (oc-mirror's own setting)               ($MIRROR_DIR/../oc-mirror-cache)
    PULL_SECRET_FILE     pull secret for registry.redhat.io and any added catalogs (REGISTRY_AUTH_FILE also works)
    OC, OC_MIRROR        client binaries                                                 (oc, oc-mirror on PATH)
    OCP_CHANNEL, OCP_VERSION   release for a new plan                                    (stable-4.22, newest)
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path

from . import __version__, imageset
from .catalog import DEFAULT_CATALOGS, CatalogCache, catalog_image


def env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name) or default


def catalog_cache(a: argparse.Namespace) -> CatalogCache:
    return CatalogCache(Path(a.mirror_dir) / ".catalogs", oc=a.oc, authfile=a.authfile)


def cmd_serve(a: argparse.Namespace) -> int:
    import uvicorn

    from .app import create_mirror_app

    app = create_mirror_app(Path(a.mirror_dir), catalog_cache(a), ocp_channel=a.channel, ocp_version=a.version)
    print(f"OpenShift mirror planner on http://{a.listen}:{a.port}/ (plan in {a.mirror_dir})", flush=True)
    uvicorn.run(app, host=a.listen, port=a.port, log_level="warning")
    return 0


def cmd_imageset(a: argparse.Namespace) -> int:
    mirror_dir = Path(a.mirror_dir)
    plan_file = mirror_dir / "plan.yaml"
    if not plan_file.exists():
        print(f"no {plan_file}; create one with `mirror-planner serve`", file=sys.stderr)
        return 1
    plan = imageset.MirrorPlan.load(plan_file)
    cache = catalog_cache(a)
    images = {c.image for c in plan.catalogs}
    ver = plan.platform.max_version or plan.platform.min_version
    if ver:
        images |= {catalog_image(n, ver) for n in DEFAULT_CATALOGS}
    summaries = {img: s for img in images if (s := cache.load(img))}
    for c in plan.catalogs:
        if c.image not in summaries:
            print(f"warning: {c.image} not scanned; its dependencies are not resolved", file=sys.stderr)
    for img, sm in summaries.items():
        if sm.stale:
            print(f"warning: {img} was scanned by an older version; rescan it to include must-gather images",
                  file=sys.stderr)
    presets = imageset.load_presets()
    print(imageset.write(plan, summaries, presets.get("companions", {}), mirror_dir, presets.get("base_images", [])))
    return 0


def cmd_mirror(a: argparse.Namespace) -> int:
    """oc-mirror --v2 mirror-to-disk into the mirror directory, with the flags the plan implies."""
    if cmd_imageset(a) != 0:
        return 1
    mirror_dir = Path(a.mirror_dir).resolve()
    flags_file = mirror_dir / "oc-mirror.flags"
    flags = shlex.split(flags_file.read_text()) if flags_file.exists() else []
    cmd = [a.oc_mirror, "--v2", "-c", str(mirror_dir / "imageset-config.yaml"), *flags]
    if a.cache_dir:  # oc-mirror reads OC_MIRROR_CACHE itself and rejects it together with --cache-dir
        cmd += ["--cache-dir", str(Path(a.cache_dir).resolve())]
    if a.authfile:
        cmd += ["--authfile", a.authfile]
    if a.dry_run:
        cmd.append("--dry-run")
    cmd.append(f"file://{mirror_dir}")
    print("+", " ".join(shlex.quote(c) for c in cmd), flush=True)
    # oc-mirror's embedded registry reads every REGISTRY_* variable as its own configuration
    # (REGISTRY_AUTH_FILE breaks it); the pull secret goes in as --authfile instead.
    child_env = {k: v for k, v in os.environ.items() if not k.startswith("REGISTRY_")}
    return subprocess.call(cmd, env=child_env)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mirror-planner", description="Plan disconnected OpenShift content and run oc-mirror.")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--mirror-dir", default=env("MIRROR_DIR", "mirror"))
        p.add_argument("--authfile", default=env("PULL_SECRET_FILE", env("REGISTRY_AUTH_FILE")),
                       help="pull secret (registry auth file)")
        p.add_argument("--oc", default=env("OC", "oc"))

    p = sub.add_parser("serve", help="run the web UI")
    common(p)
    p.add_argument("--channel", default=env("OCP_CHANNEL", "stable-4.22"))
    p.add_argument("--ocp-version", dest="version", default=env("OCP_VERSION"), help="pin a release for a new plan")
    p.add_argument("--listen", default=env("LISTEN", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(env("PORT", "8088")))
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("imageset", help="render imageset-config.yaml from plan.yaml")
    common(p)
    p.set_defaults(fn=cmd_imageset)

    p = sub.add_parser("mirror", help="run oc-mirror --v2 mirror-to-disk for the plan")
    common(p)
    p.add_argument("--oc-mirror", default=env("OC_MIRROR", "oc-mirror"))
    p.add_argument("--cache-dir", default=None)
    p.add_argument("--dry-run", action="store_true", help="resolve images without downloading them")
    p.set_defaults(fn=cmd_mirror)

    a = ap.parse_args(argv)
    if getattr(a, "cache_dir", "unset") is None and not os.environ.get("OC_MIRROR_CACHE"):
        a.cache_dir = str(Path(a.mirror_dir).resolve().parent / "oc-mirror-cache")
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
