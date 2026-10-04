"""mirror-planner command line.

    mirror-planner serve      [--mirror-dir DIR] [--listen ADDR] [--port 8088]   web UI
    mirror-planner imageset   [--mirror-dir DIR]                                 render imageset-config.yaml from plan.yaml

Defaults come from the environment, which the container image sets:
    MIRROR_DIR           plan.yaml, imageset-config.yaml and catalog scans               (./mirror)
    PULL_SECRET_FILE     pull secret for registry.redhat.io and any added catalogs (REGISTRY_AUTH_FILE also works)
    SSL_CERT_FILE        CA bundle for registries with a private CA                      (system bundle)
    OCP_CHANNEL, OCP_VERSION   release for a new plan                                    (stable-4.22, newest)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__, imageset
from .catalog import DEFAULT_CATALOGS, CatalogCache, catalog_image


def env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name) or default


def catalog_cache(a: argparse.Namespace) -> CatalogCache:
    return CatalogCache(Path(a.mirror_dir) / ".catalogs", authfile=a.authfile)


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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mirror-planner",
                                 description="Build oc-mirror ImageSetConfigurations for disconnected OpenShift.")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--mirror-dir", default=env("MIRROR_DIR", "mirror"))
        p.add_argument("--authfile", default=env("PULL_SECRET_FILE", env("REGISTRY_AUTH_FILE")),
                       help="pull secret (registry auth file)")

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

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
