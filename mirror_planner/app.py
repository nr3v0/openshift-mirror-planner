"""OpenShift mirror planner: pick the release and operators to mirror for a disconnected
cluster and render the oc-mirror v2 ImageSetConfiguration. Runs on a connected build host.
Server-rendered HTML forms plus a little dependency-free JavaScript; no JavaScript build tooling.

Every change saves mirror/plan.yaml and regenerates mirror/imageset-config.yaml.
"""

from __future__ import annotations

import contextvars
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import quote, urlencode

import yaml
from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import imageset
from .catalog import (DEFAULT_CATALOGS, CatalogCache, CatalogSummary, Package, catalog_image, catalog_name,
                      valid_image_ref)
from .releases import ReleaseGraph, version_key

HERE = Path(__file__).parent
# True while handling a request sent by the page's script (header X-FH-Fetch: 1); actions then
# answer with JSON for the page to apply instead of redirecting.
FETCH: contextvars.ContextVar[bool] = contextvars.ContextVar("fh_fetch", default=False)
TEMPLATES = Jinja2Templates(directory=str(HERE / "templates"))
TEMPLATES.env.filters["catalog_name"] = catalog_name
ALL_CATALOGS = "__all__"


def _rank(p: Package, q: str, disconnected_only: bool) -> int | None:
    if disconnected_only and p.disconnected is False:
        return None
    if not q:
        return 3
    if p.name == q:
        return 0
    if q in p.name or q in (p.display_name or "").lower():
        return 1
    hay = " ".join([p.description or "", p.provider or "", " ".join(p.keywords)]).lower()
    return 2 if q in hay else None


def search_many(summaries: dict[str, CatalogSummary], q: str, disconnected_only: bool,
                limit: int = 100) -> list[tuple[str, Package]]:
    """[(catalog image, package)] best matches first, across the given catalogs."""
    q = q.strip().lower()
    ranked = [(r, p.name, img, p) for img, summary in summaries.items() for p in summary.packages
              if (r := _rank(p, q, disconnected_only)) is not None]
    return [(img, p) for _, _, img, p in sorted(ranked, key=lambda t: t[:3])[:limit]]


def search(summary: CatalogSummary, q: str, disconnected_only: bool, limit: int = 100) -> list[Package]:
    return [p for _, p in search_many({summary.image: summary}, q, disconnected_only, limit)]


@dataclass
class Job:
    """A background catalog scan. The page polls /jobs/<id> and follows `result_url` when done."""
    id: str
    title: str
    state: str = "running"  # running | done | failed
    phase: str = "starting"
    fraction: float | None = None  # None: progress unknown
    detail: str = ""
    started: float = field(default_factory=time.time)
    result_url: str = "/"
    msg: str | None = None
    err: str | None = None

    def view(self) -> dict:
        return asdict(self) | {"elapsed": round(time.time() - self.started)}


def create_mirror_app(mirror_dir: Path, cache: CatalogCache, ocp_channel: str, ocp_version: str | None,
                      presets_file: Path = imageset.PRESETS_FILE, graph: ReleaseGraph | None = None) -> FastAPI:
    app = FastAPI(title="OpenShift mirror planner")
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.middleware("http")
    async def mark_fetch(request: Request, call_next):
        token = FETCH.set(request.headers.get("x-fh-fetch") == "1")
        try:
            return await call_next(request)
        finally:
            FETCH.reset(token)
    graph = graph or ReleaseGraph()
    plan_path = mirror_dir / "plan.yaml"
    presets = imageset.load_presets(presets_file)
    companions: dict[str, list[str]] = presets.get("companions", {})
    base_images: list[str] = presets.get("base_images", [])

    def load_plan() -> imageset.MirrorPlan:
        if plan_path.exists():
            return imageset.MirrorPlan.load(plan_path)
        return imageset.MirrorPlan(platform=imageset.Platform(
            channel=ocp_channel, min_version=ocp_version, max_version=ocp_version))

    def version(plan: imageset.MirrorPlan) -> str:
        return plan.platform.max_version or plan.platform.min_version or ocp_channel.rsplit("-", 1)[-1]

    def catalogs(plan: imageset.MirrorPlan) -> dict[str, str]:
        """{key: image}: the three Red Hat indexes for the plan's OpenShift version (keyed by index
        name), then added catalogs and any other image the plan selects from (keyed by image)."""
        out = {name: catalog_image(name, version(plan)) for name in DEFAULT_CATALOGS}
        for img in [e.image for e in plan.extra_catalogs] + [c.image for c in plan.catalogs]:
            if img not in out.values():
                out[img] = img
        return out

    def insecure(plan: imageset.MirrorPlan, image: str) -> bool:
        return any(e.image == image and e.insecure for e in plan.extra_catalogs)

    def summaries(plan: imageset.MirrorPlan) -> dict[str, CatalogSummary]:
        return {img: s for img in catalogs(plan).values() if (s := cache.load(img))}

    def save(plan: imageset.MirrorPlan) -> None:
        imageset.write(plan, summaries(plan), companions, mirror_dir, base_images)

    jobs: dict[str, Job] = {}
    lock = threading.Lock()

    def active_job() -> Job | None:
        return next((j for j in jobs.values() if j.state == "running"), None)

    def start_scan(image: str, insecure_tls: bool, title: str, result_page: str) -> Job | None:
        """Run a scan in a thread; None when another scan is still running."""
        with lock:
            if active_job():
                return None
            job = Job(id=uuid.uuid4().hex[:12], title=title)
            jobs[job.id] = job

        def progress(phase: str, fraction: float | None, detail: str) -> None:
            job.phase, job.fraction, job.detail = phase, fraction, detail

        def run() -> None:
            try:
                s = cache.refresh(image, insecure=insecure_tls, progress=progress)
                save(load_plan())
                n = len(s.packages)
                job.msg = f"{image}: {n} operator{'' if n == 1 else 's'}"
                job.result_url = with_notice(result_page, msg=job.msg)
                job.state, job.phase, job.fraction = "done", "done", 1.0
            except Exception as e:  # noqa: BLE001 - reported to the page
                job.err = str(e)
                job.result_url = with_notice(result_page, err=job.err)
                job.state, job.phase, job.detail = "failed", "failed", str(e)

        threading.Thread(target=run, daemon=True, name=f"scan-{job.id}").start()
        return job

    def with_notice(url: str, **notice: str) -> str:
        if not url.startswith("/"):
            url = "/"
        return url + ("&" if "?" in url else "?") + urlencode(notice) if notice else url

    def with_job(url: str, job: Job):
        if FETCH.get():
            return JSONResponse({"job": job.id, "title": job.title, "location": url})
        return RedirectResponse(with_notice(url, job=job.id), 303)

    @app.get("/api/releases/versions")
    def release_versions(channel: str):
        return {"channel": channel, "versions": graph.versions(channel), "error": graph.last_error}

    @app.get("/jobs/{job_id}")
    def job_status(job_id: str):
        job = jobs.get(job_id)
        if job is None:
            return JSONResponse({"state": "unknown", "result_url": "/"}, status_code=404)
        return job.view()

    def back(url: str, **notice: str):
        notice = {k: v for k, v in notice.items() if v}
        if FETCH.get():  # the page applies the change itself: no navigation
            return JSONResponse({"msg": notice.get("msg"), "err": notice.get("err"), "warn": notice.get("warn"),
                                 "location": url if url.startswith("/") else "/"})
        if notice:
            url += ("&" if "?" in url else "?") + urlencode(notice)
        return RedirectResponse(url if url.startswith("/") else "/", 303)

    def page_url(request: Request) -> str:
        """The page URL for this search, without one-off parameters; forms return here."""
        keep = [(k, v) for k, v in request.query_params.multi_items() if k not in ("job", "msg", "err", "warn")]
        return "/" + (("?" + urlencode(keep)) if keep else "")

    def search_context(request: Request, plan: imageset.MirrorPlan, q: str, catalog: str,
                       disconnected_only: bool) -> dict:
        cats = catalogs(plan)
        sums = summaries(plan)
        if catalog == ALL_CATALOGS:
            image = ALL_CATALOGS
            results = search_many(sums, q, disconnected_only)
        else:
            image = cats.get(catalog, cats["redhat-operator-index"])
            results = search_many({image: sums[image]}, q, disconnected_only) if image in sums else []
        return {"cats": cats, "sums": sums, "catalog": catalog, "image": image, "q": q,
                "disconnected_only": disconnected_only, "results": results, "all_key": ALL_CATALOGS,
                "selected": {(c.image, p.name): p for c in plan.catalogs for p in c.packages},
                "names": {img: key if key in DEFAULT_CATALOGS else catalog_name(img) for key, img in cats.items()},
                "here": page_url(request)}

    @app.get("/search", response_class=HTMLResponse)
    def search_fragment(request: Request, q: str = "", catalog: str = "redhat-operator-index",
                        disconnected_only: bool = True):
        """Just the results section, for the page's live search."""
        return TEMPLATES.TemplateResponse(request, "_operator_results.html",
                                          search_context(request, load_plan(), q, catalog, disconnected_only))

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, q: str = "", catalog: str = "redhat-operator-index",
              disconnected_only: bool = True, msg: str | None = None, err: str | None = None,
              warn: str | None = None):
        plan = load_plan()
        ctx = search_context(request, plan, q, catalog, disconnected_only)
        cats, sums = ctx["cats"], ctx["sums"]
        channels = graph.channels(plan.platform.channel)
        if plan.platform.channel not in channels:
            channels = [plan.platform.channel, *channels]
        versions = graph.versions(plan.platform.channel)
        deps = imageset.dependencies(plan, sums, companions)
        dep_rows = [(img, name, needed_by) for img, d in deps.items() for name, needed_by in sorted(d.items())]
        isc = yaml.safe_dump(imageset.render(plan, sums, companions, base_images), sort_keys=False)
        auto_images = imageset.automatic_images(plan, sums, companions, base_images)
        stale = [img for img, sm in sums.items() if sm.stale]
        extra = {e.image: e for e in plan.extra_catalogs}
        running = active_job()
        return TEMPLATES.TemplateResponse(request, "index.html", ctx | {
            "plan": plan, "extra": extra, "channels": channels, "versions": versions,
            "graph_error": graph.last_error, "dep_rows": dep_rows, "auto_images": auto_images, "stale": stale,
            "presets": presets.get("presets", {}), "isc": isc,
            "msg": msg, "err": err, "warn": warn, "mirror_dir": mirror_dir,
            "job_id": request.query_params.get("job") or (running.id if running else ""),
            "job_title": running.title if running else "",
        })

    @app.post("/platform")
    def platform(channel: str = Form(...), min_version: str = Form(""), max_version: str = Form(""),
                 update_graph: bool = Form(False), back_to: str = Form("/")):
        lo, hi = min_version.strip() or None, max_version.strip() or None
        if lo and hi and version_key(lo) > version_key(hi):
            return back(back_to, err=f"min version {lo} is newer than max version {hi}")
        plan = load_plan()
        plan.platform = imageset.Platform(channel=channel.strip(), min_version=lo, max_version=hi, graph=update_graph)
        save(plan)
        return back(back_to, msg="Release saved")

    @app.post("/catalogs/refresh")
    def refresh(image: str = Form(...), back_to: str = Form("/")):
        job = start_scan(image, insecure(load_plan(), image), f"Scanning {image}", back_to)
        if job is None:
            return back(back_to, err="another scan is still running")
        return with_job(back_to, job)

    @app.post("/catalogs/add")
    def add_catalog(image: str = Form(...), insecure_tls: bool = Form(False), scan: bool = Form(False),
                    back_to: str = Form("/")):
        image = image.strip()
        if not valid_image_ref(image):
            return back(back_to, err=f"not a full image reference with a tag or digest: {image}")
        plan = load_plan()
        if image in catalogs(plan).values() and not any(e.image == image for e in plan.extra_catalogs):
            return back(back_to, err=f"{image} is already listed")
        plan.extra_catalogs = [e for e in plan.extra_catalogs if e.image != image]
        plan.extra_catalogs.append(imageset.ExtraCatalog(image=image, insecure=insecure_tls))
        save(plan)
        if scan:
            page = f"/?catalog={quote(image, safe='')}"
            job = start_scan(image, insecure_tls, f"Adding and scanning {image}", page)
            if job is None:
                return back(page, msg=f"added {image}; scan it after the running scan finishes")
            return with_job(page, job)
        return back(back_to, msg=f"added {image}")

    @app.post("/catalogs/remove")
    def remove_catalog(image: str = Form(...), back_to: str = Form("/")):
        if active_job():
            return back(back_to, err="wait for the running scan to finish")
        plan = load_plan()
        if not any(e.image == image for e in plan.extra_catalogs):
            return back(back_to, err="only added catalogs can be removed")
        plan.extra_catalogs = [e for e in plan.extra_catalogs if e.image != image]
        plan.catalogs = [c for c in plan.catalogs if c.image != image]
        cache.forget(image)
        save(plan)
        return back("/", msg=f"removed {image} and its selections")

    @app.post("/packages/add")
    def add(image: str = Form(...), name: str = Form(...), channel: str = Form(""), back_to: str = Form("/")):
        plan = load_plan()
        updating = plan.catalog(image) is not None and plan.catalog(image).get(name) is not None
        plan.add(image, name, [channel] if channel else None)
        save(plan)
        return back(back_to, msg=f"{'Updated' if updating else 'Added'} {name}" + (f" ({channel})" if channel else ""))

    @app.post("/packages/remove")
    def remove(image: str = Form(...), name: str = Form(...), back_to: str = Form("/")):
        plan = load_plan()
        plan.remove(image, name)
        save(plan)
        return back(back_to, msg=f"Removed {name}")

    @app.post("/presets/{preset_id}")
    def apply_preset(preset_id: str, back_to: str = Form("/")):
        preset = presets.get("presets", {}).get(preset_id)
        if not preset:
            return back(back_to, err=f"no preset {preset_id}")
        plan = load_plan()
        cats = catalogs(plan)
        missing, unscanned = [], []
        for index_name, names in preset.get("operators", {}).items():
            image = cats.get(index_name) or catalog_image(index_name, version(plan))
            summary = cache.load(image)
            if summary is None:
                unscanned.append(index_name)
            for name in names:
                if summary is not None and summary.package(name) is None:
                    missing.append(f"{name} ({index_name})")
                    continue
                plan.add(image, name, reason=f"preset {preset_id}")
        save(plan)
        notes = []
        if unscanned:
            notes.append("not scanned yet, added unchecked: " + ", ".join(unscanned))
        if missing:
            notes.append("not in catalog, skipped: " + ", ".join(missing))
        if notes:
            return back(back_to, err=f"{preset['title']} applied; " + "; ".join(notes))
        return back(back_to, msg=f"{preset['title']} applied")

    @app.post("/import")
    async def import_file(file: UploadFile, back_to: str = Form("/")):
        """Replace the plan with the contents of an uploaded ImageSetConfiguration."""
        if active_job():
            return back(back_to, err="wait for the running scan to finish")
        raw = await file.read()
        if len(raw) > 1_000_000:
            return back(back_to, err=f"{file.filename} is larger than 1 MB; is it an ImageSetConfiguration?")
        try:
            plan, warnings = imageset.import_isc(raw.decode("utf-8", errors="replace"))
        except imageset.ISCImportError as e:
            return back(back_to, err=f"{file.filename}: {e}")
        builtin = {catalog_image(n, version(plan)) for n in DEFAULT_CATALOGS}
        plan.extra_catalogs = [imageset.ExtraCatalog(image=c.image) for c in plan.catalogs if c.image not in builtin]
        sums = summaries(plan)
        unscanned = [c.image for c in plan.catalogs if c.image not in sums]
        folded = imageset.fold_dependencies(plan, sums, companions)
        automatic = {img for img, _ in imageset.automatic_images(plan, sums, companions, base_images)}
        plan.additional_images = [i for i in plan.additional_images if i not in automatic]
        if unscanned:
            warnings.append("scan " + ", ".join(unscanned) + " to resolve dependencies and recognise must-gather images")
        save(plan)
        n_ops = sum(len(c.packages) for c in plan.catalogs)
        msg = (f"Imported {file.filename}: {plan.platform.channel} "
               f"{plan.platform.min_version or ''}{'-' + plan.platform.max_version if plan.platform.max_version else ''}, "
               f"{n_ops} operator{'' if n_ops == 1 else 's'} from {len(plan.catalogs)} catalog{'' if len(plan.catalogs) == 1 else 's'}"
               + (f" plus {len(folded)} as dependencies" if folded else "") + ", "
               f"{len(plan.additional_images)} own image{'' if len(plan.additional_images) == 1 else 's'}")
        return back("/", msg=msg, warn="; ".join(warnings))

    @app.post("/images")
    def images(additional_images: str = Form(""), back_to: str = Form("/")):
        plan = load_plan()
        plan.additional_images = [line.strip() for line in additional_images.splitlines() if line.strip()]
        save(plan)
        return back(back_to, msg="Additional images saved")

    return app
