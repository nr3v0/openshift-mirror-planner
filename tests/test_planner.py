import json
import re
import time

import httpx
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import yaml
from fastapi.testclient import TestClient

from mirror_planner import imageset
from mirror_planner.catalog import CatalogCache, catalog_image, parse_configs, resolve_dependencies
from mirror_planner.app import create_mirror_app
from mirror_planner.releases import ReleaseGraph


def offline_graph() -> ReleaseGraph:
    """Update-service stand-in: stable/fast/eus-4.22 and candidate-4.23 exist."""
    data = {"stable-4.22": ["4.21.9", "4.22.14", "4.22.15"], "fast-4.22": ["4.22.15", "4.22.16"],
            "eus-4.22": ["4.22.15"], "candidate-4.23": ["4.23.0-rc.1"]}

    def handler(request: httpx.Request) -> httpx.Response:
        nodes = [{"version": v} for v in data.get(request.url.params["channel"], [])]
        return httpx.Response(200, json={"nodes": nodes, "edges": []})

    return ReleaseGraph(client=httpx.Client(transport=httpx.MockTransport(handler)))

IMAGE = catalog_image("redhat-operator-index", "4.22.15")


def bundle(pkg, version, props=(), disconnected="true", replaces=None, mg_annotation=None, related=()):
    ann = {"features.operators.openshift.io/disconnected": disconnected}
    if mg_annotation:
        ann["operators.openshift.io/must-gather-image"] = mg_annotation
    meta = {"displayName": pkg.title(), "description": f"# {pkg}\n\n{pkg} does things.\n\nMore.",
            "provider": {"name": "Red Hat"}, "annotations": ann}
    return {"schema": "olm.bundle", "name": f"{pkg}.v{version}", "package": pkg, "properties": [
        {"type": "olm.package", "value": {"packageName": pkg, "version": version}},
        {"type": "olm.csv.metadata", "value": meta}, *props],
        "relatedImages": [{"name": n, "image": i} for n, i in related]}


LVMS_MG = "registry.redhat.io/lvms4/lvms-must-gather-rhel9@sha256:" + "1" * 64
LVMS_MG_421 = "registry.redhat.io/lvms4/lvms-must-gather-rhel9@sha256:" + "2" * 64
MCG_MG = "registry.redhat.io/odf4/mcg-mustgather-rhel9@sha256:" + "3" * 64


def write_fbc(root: Path) -> Path:
    configs = root / "configs"
    # one catalog.json stream: two channels, head is the unreplaced entry
    d = configs / "lvms-operator"
    d.mkdir(parents=True)
    objs = [
        {"schema": "olm.package", "name": "lvms-operator", "defaultChannel": "stable-4.22"},
        {"schema": "olm.channel", "package": "lvms-operator", "name": "stable-4.22",
         "entries": [{"name": "lvms-operator.v4.22.0"}, {"name": "lvms-operator.v4.22.1", "replaces": "lvms-operator.v4.22.0"}]},
        {"schema": "olm.channel", "package": "lvms-operator", "name": "stable-4.21",
         "entries": [{"name": "lvms-operator.v4.21.3"}]},
        bundle("lvms-operator", "4.22.0"),
        bundle("lvms-operator", "4.22.1", mg_annotation=LVMS_MG,
               related=[("must-gather", LVMS_MG), ("operator", "registry.redhat.io/lvms4/lvms-rhel9-operator@sha256:" + "4" * 64)]),
        bundle("lvms-operator", "4.21.3", mg_annotation=LVMS_MG_421),
    ]
    (d / "catalog.json").write_text("\n".join(json.dumps(o) for o in objs))
    # split layout with a declared package dependency and a GVK dependency
    d = configs / "odf-dependencies"
    (d / "channels").mkdir(parents=True)
    (d / "package.json").write_text(json.dumps({"schema": "olm.package", "name": "odf-dependencies", "defaultChannel": "stable-4.22"}))
    (d / "channels" / "stable-4.22.json").write_text(json.dumps(
        {"schema": "olm.channel", "package": "odf-dependencies", "name": "stable-4.22", "entries": [{"name": "odf-dependencies.v4.22.5"}]}))
    (d / "bundles.json").write_text(json.dumps(bundle("odf-dependencies", "4.22.5", props=[
        {"type": "olm.package.required", "value": {"packageName": "mcg-operator", "versionRange": ">=4.22.0"}},
        {"type": "olm.gvk.required", "value": {"group": "ceph.rook.io", "version": "v1", "kind": "CephCluster"}}])))
    # YAML layout, providing the GVK
    for name, provides in (("mcg-operator", None), ("rook-ceph-operator", ("ceph.rook.io", "v1", "CephCluster")), ("odf-operator", None)):
        d = configs / name
        d.mkdir()
        props = [{"type": "olm.gvk", "value": {"group": provides[0], "version": provides[1], "kind": provides[2]}}] if provides else []
        related = [("mcg-mustgather", MCG_MG)] if name == "mcg-operator" else []
        docs = [{"schema": "olm.package", "name": name, "defaultChannel": "stable-4.22"},
                {"schema": "olm.channel", "package": name, "name": "stable-4.22", "entries": [{"name": f"{name}.v4.22.5"}]},
                bundle(name, "4.22.5", props=props, related=related)]
        (d / "catalog.yaml").write_text(yaml.safe_dump_all(docs))
    # an operator that needs internet
    d = configs / "online-only"
    d.mkdir()
    (d / "catalog.json").write_text("\n".join(json.dumps(o) for o in [
        {"schema": "olm.package", "name": "online-only", "defaultChannel": "alpha"},
        {"schema": "olm.channel", "package": "online-only", "name": "alpha", "entries": [{"name": "online-only.v1.0.0"}]},
        bundle("online-only", "1.0.0", disconnected="false")]))
    return configs


def test_parse_layouts_heads_and_metadata(tmp_path):
    s = parse_configs(write_fbc(tmp_path), IMAGE)
    assert sorted(p.name for p in s.packages) == [
        "lvms-operator", "mcg-operator", "odf-dependencies", "odf-operator", "online-only", "rook-ceph-operator"]
    lvms = s.package("lvms-operator")
    assert lvms.default_channel == "stable-4.22"
    assert lvms.channel("stable-4.22").head == "4.22.1"
    assert lvms.channel("stable-4.22").versions == ["4.22.1", "4.22.0"]
    assert lvms.disconnected is True
    assert lvms.description == "lvms-operator does things."
    assert s.package("online-only").disconnected is False


def test_dependencies_follow_declarations_gvks_and_companions(tmp_path):
    s = parse_configs(write_fbc(tmp_path), IMAGE)
    deps = resolve_dependencies(s, ["odf-operator"], {"odf-operator": ["odf-dependencies"]})
    assert deps == {"odf-dependencies": ["odf-operator"], "mcg-operator": ["odf-dependencies"],
                    "rook-ceph-operator": ["odf-dependencies"]}
    assert resolve_dependencies(s, ["odf-operator"]) == {}


def test_render_imageset(tmp_path):
    s = parse_configs(write_fbc(tmp_path), IMAGE)
    plan = imageset.MirrorPlan(platform=imageset.Platform(channel="stable-4.22", min_version="4.22.15", max_version="4.22.15"))
    plan.add(IMAGE, "lvms-operator", ["stable-4.21"])
    plan.add(IMAGE, "odf-operator")
    isc = imageset.render(plan, {IMAGE: s}, {"odf-operator": ["odf-dependencies"]})
    assert isc["apiVersion"] == "mirror.openshift.io/v2alpha1"
    assert isc["mirror"]["platform"] == {"channels": [{"name": "stable-4.22", "type": "ocp", "minVersion": "4.22.15",
                                                       "maxVersion": "4.22.15"}], "graph": True}
    pkgs = {p["name"]: p for p in isc["mirror"]["operators"][0]["packages"]}
    assert set(pkgs) == {"lvms-operator", "odf-operator", "odf-dependencies", "mcg-operator", "rook-ceph-operator"}
    # a non-default channel needs defaultChannel set for oc-mirror v2
    assert pkgs["lvms-operator"] == {"name": "lvms-operator", "channels": [{"name": "stable-4.21"}], "defaultChannel": "stable-4.21"}
    assert pkgs["mcg-operator"] == {"name": "mcg-operator", "channels": [{"name": "stable-4.22"}]}


def app_client(tmp_path) -> tuple[TestClient, Path]:
    cache = CatalogCache(tmp_path / "mirror" / ".catalogs")
    summary = parse_configs(write_fbc(tmp_path), IMAGE)
    cache.summary_path(IMAGE).parent.mkdir(parents=True)
    cache.summary_path(IMAGE).write_text(summary.model_dump_json())
    presets = tmp_path / "presets.yaml"
    presets.write_text(yaml.safe_dump({
        "base_images": ["registry.redhat.io/ubi9/ubi:latest"],
        "companions": {"odf-operator": ["odf-dependencies"]},
        "presets": {"hub": {"title": "Hub", "description": "d",
                            "operators": {"redhat-operator-index": ["odf-operator", "not-a-package"]}}}}))
    app = create_mirror_app(tmp_path / "mirror", cache, "stable-4.22", "4.22.15", presets_file=presets, graph=offline_graph())
    return TestClient(app), tmp_path / "mirror"


def test_ui_search_filters_disconnected(tmp_path):
    c, _ = app_client(tmp_path)
    page = c.get("/", params={"q": "online", "disconnected_only": "true"}).text
    assert "online-only" not in page.split("Find operators")[1].split("OpenShift release")[0]
    page = c.get("/", params={"q": "online", "disconnected_only": "false"}).text
    assert "Needs internet" in page


def test_ui_add_preset_and_write_imageset(tmp_path):
    c, mirror = app_client(tmp_path)
    c.post("/packages/add", data={"image": IMAGE, "name": "lvms-operator", "channel": "stable-4.22"})
    r = c.post("/presets/hub", data={"back_to": "/"}, follow_redirects=False)
    assert "not-a-package" in r.headers["location"]
    plan = yaml.safe_load((mirror / "plan.yaml").read_text())
    assert [p["name"] for p in plan["catalogs"][0]["packages"]] == ["lvms-operator", "odf-operator"]
    isc = yaml.safe_load((mirror / "imageset-config.yaml").read_text())
    names = [p["name"] for p in isc["mirror"]["operators"][0]["packages"]]
    assert "odf-dependencies" in names and "rook-ceph-operator" in names
    assert "Added as dependencies" in c.get("/").text
    c.post("/packages/remove", data={"image": IMAGE, "name": "odf-operator"})
    isc = yaml.safe_load((mirror / "imageset-config.yaml").read_text())
    assert [p["name"] for p in isc["mirror"]["operators"][0]["packages"]] == ["lvms-operator"]


def fake_oc(tmp_path: Path) -> Path:
    """Stand-in for `oc image extract`: writes a one-package catalog and records its arguments."""
    script = tmp_path / "oc"
    script.write_text(f"""#!/usr/bin/bash
echo "$@" >> {tmp_path}/oc.args
dest=$(printf '%s\\n' "$@" | sed -n 's|^/configs/:||p')
mkdir -p "$dest/acme-operator"
cat > "$dest/acme-operator/catalog.json" <<'EOF'
{{"schema": "olm.package", "name": "acme-operator", "defaultChannel": "stable"}}
{{"schema": "olm.channel", "package": "acme-operator", "name": "stable", "entries": [{{"name": "acme-operator.v1.2.0"}}]}}
{{"schema": "olm.bundle", "name": "acme-operator.v1.2.0", "package": "acme-operator", "properties": [{{"type": "olm.package", "value": {{"packageName": "acme-operator", "version": "1.2.0"}}}}]}}
EOF
""")
    script.chmod(0o755)
    return script


def wait_for_job(c: TestClient, location: str) -> dict:
    job_id = parse_qs(urlsplit(location).query)["job"][0]
    for _ in range(100):
        job = c.get(f"/jobs/{job_id}").json()
        if job["state"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_add_scan_select_and_remove_custom_catalog(tmp_path):
    custom = "registry.lab.example:5000/ops/acme-index:v1"
    cache = CatalogCache(tmp_path / "mirror" / ".catalogs", oc=str(fake_oc(tmp_path)))
    c = TestClient(create_mirror_app(tmp_path / "mirror", cache, "stable-4.22", "4.22.15", graph=offline_graph()))
    mirror = tmp_path / "mirror"

    r = c.post("/catalogs/add", data={"image": "acme-index", "back_to": "/"}, follow_redirects=False)
    assert "not+a+full+image+reference" in r.headers["location"]

    r = c.post("/catalogs/add", data={"image": custom, "insecure_tls": ["false", "true"], "scan": ["false", "true"],
                                      "back_to": "/"}, follow_redirects=False)
    job = wait_for_job(c, r.headers["location"])
    assert job["state"] == "done" and "1+operator" in job["result_url"] and job["msg"].endswith("1 operator")
    assert "--insecure=true" in (tmp_path / "oc.args").read_text()
    assert not (cache.dir / "registry.lab.example-5000-ops-acme-index-v1" / "configs").exists()  # raw extract dropped

    page = c.get("/", params={"catalog": custom, "q": "acme", "disconnected_only": "false"}).text
    assert "acme-operator" in page and "TLS not verified" in page

    c.post("/packages/add", data={"image": custom, "name": "acme-operator", "channel": "stable"})
    isc = yaml.safe_load((mirror / "imageset-config.yaml").read_text())
    assert {"catalog": custom, "packages": [{"name": "acme-operator", "channels": [{"name": "stable"}]}]} \
        in isc["mirror"]["operators"]
    assert (mirror / "oc-mirror.flags").read_text().strip() == "--src-tls-verify=false"

    c.post("/catalogs/remove", data={"image": custom})
    plan = yaml.safe_load((mirror / "plan.yaml").read_text())
    assert plan.get("extra_catalogs", []) == [] and plan.get("catalogs", []) == []
    assert (mirror / "oc-mirror.flags").read_text().strip() == ""
    assert cache.load(custom) is None


def test_builtin_catalog_cannot_be_removed(tmp_path):
    c, _ = app_client(tmp_path)
    r = c.post("/catalogs/remove", data={"image": IMAGE, "back_to": "/"}, follow_redirects=False)
    assert "only+added+catalogs" in r.headers["location"]


def test_one_scan_at_a_time_and_page_shows_running_job(tmp_path):
    custom = "registry.lab.example/ops/acme-index:v1"
    oc = fake_oc(tmp_path)
    oc.write_text(oc.read_text().replace("#!/usr/bin/bash\n", "#!/usr/bin/bash\nsleep 1\n"))
    cache = CatalogCache(tmp_path / "mirror" / ".catalogs", oc=str(oc))
    c = TestClient(create_mirror_app(tmp_path / "mirror", cache, "stable-4.22", "4.22.15", graph=offline_graph()))

    first = c.post("/catalogs/add", data={"image": custom, "scan": "true", "back_to": "/"}, follow_redirects=False)
    running = c.get(f"/jobs/{parse_qs(urlsplit(first.headers['location']).query)['job'][0]}").json()
    assert running["state"] == "running" and running["phase"] in ("starting", "extracting")

    second = c.post("/catalogs/refresh", data={"image": custom, "back_to": "/"}, follow_redirects=False)
    assert "another+scan+is+still+running" in second.headers["location"]
    page = c.get("/", params={"msg": "old", "job": "stale"}).text
    assert 'data-title="Adding and scanning' in page          # dialog opens for the running job
    assert "job=stale" not in page and "msg=old" not in page     # forms don't carry them back

    assert wait_for_job(c, first.headers["location"])["state"] == "done"
    assert c.get("/jobs/nope").status_code == 404


def test_failed_scan_reports_error(tmp_path):
    oc = tmp_path / "oc"
    oc.write_text("#!/usr/bin/bash\necho 'unauthorized: authentication required' >&2\nexit 1\n")
    oc.chmod(0o755)
    cache = CatalogCache(tmp_path / "mirror" / ".catalogs", oc=str(oc))
    c = TestClient(create_mirror_app(tmp_path / "mirror", cache, "stable-4.22", "4.22.15", graph=offline_graph()))
    r = c.post("/catalogs/refresh", data={"image": IMAGE, "back_to": "/"}, follow_redirects=False)
    job = wait_for_job(c, r.headers["location"])
    assert job["state"] == "failed" and "authentication required" in job["detail"]
    assert "err=" in job["result_url"]


BASE = ["registry.redhat.io/ubi9/ubi:latest", "registry.redhat.io/rhel9/support-tools:latest"]


def test_must_gather_per_channel_head(tmp_path):
    s = parse_configs(write_fbc(tmp_path), IMAGE)
    lvms = s.package("lvms-operator")
    assert lvms.channel("stable-4.22").must_gather == [LVMS_MG]          # annotation and relatedImages, once
    assert lvms.channel("stable-4.21").must_gather == [LVMS_MG_421]
    assert s.package("mcg-operator").channel("stable-4.22").must_gather == [MCG_MG]   # relatedImages only
    assert s.package("odf-operator").channel("stable-4.22").must_gather == []
    assert not s.stale


def test_additional_images_include_base_and_must_gathers(tmp_path):
    s = parse_configs(write_fbc(tmp_path), IMAGE)
    plan = imageset.MirrorPlan(additional_images=["quay.io/me/tool:1", BASE[0]])
    plan.add(IMAGE, "lvms-operator", ["stable-4.21"])
    plan.add(IMAGE, "odf-operator")
    isc = imageset.render(plan, {IMAGE: s}, {"odf-operator": ["odf-dependencies"]}, BASE)
    assert [i["name"] for i in isc["mirror"]["additionalImages"]] == [
        *BASE, LVMS_MG_421, MCG_MG, "quay.io/me/tool:1"]   # chosen channel's head; mcg via dependencies; no duplicates
    auto = dict(imageset.automatic_images(plan, {IMAGE: s}, {"odf-operator": ["odf-dependencies"]}, BASE))
    assert auto[MCG_MG] == "must-gather for mcg-operator (stable-4.22)"


def test_old_summary_flagged_for_rescan(tmp_path):
    s = parse_configs(write_fbc(tmp_path / "fixture"), IMAGE)
    old = s.model_copy(update={"schema_version": 1})
    assert old.stale
    c, mirror = app_client(tmp_path)
    cache_file = mirror / ".catalogs" / "redhat-operator-index-v4.22" / "summary.json"
    cache_file.write_text(old.model_dump_json())
    assert "scanned by an older version" in c.get("/").text


def test_release_channels_and_versions_from_update_service(tmp_path):
    g = offline_graph()
    assert g.channels("stable-4.22") == ["candidate-4.23", "stable-4.22", "fast-4.22", "eus-4.22"]
    assert g.versions("stable-4.22") == ["4.22.15", "4.22.14", "4.21.9"]
    c, _ = app_client(tmp_path)
    page = c.get("/").text
    assert re.search(r"<option\s+selected>stable-4\.22</option>", page)
    assert re.search(r"<option\s*>candidate-4\.23</option>", page)
    assert re.search(r"<option\s+selected>4\.22\.15</option>", page)   # saved max version
    assert c.get("/api/releases/versions", params={"channel": "fast-4.22"}).json()["versions"] == ["4.22.16", "4.22.15"]


def test_release_min_must_not_exceed_max(tmp_path):
    c, mirror = app_client(tmp_path)
    r = c.post("/platform", data={"channel": "stable-4.22", "min_version": "4.22.15", "max_version": "4.21.9",
                                  "back_to": "/"}, follow_redirects=False)
    assert "newer+than+max" in r.headers["location"]
    c.post("/platform", data={"channel": "fast-4.22", "min_version": "4.22.15", "max_version": "4.22.16",
                              "update_graph": ["false", "true"]})
    plan = yaml.safe_load((mirror / "plan.yaml").read_text())
    assert plan["platform"] == {"channel": "fast-4.22", "min_version": "4.22.15", "max_version": "4.22.16", "graph": True}


def test_search_all_catalogs(tmp_path):
    c, _ = app_client(tmp_path)
    page = c.get("/", params={"catalog": "__all__", "q": "operator", "disconnected_only": "false"}).text
    assert "All catalogs" in page and "across 1 scanned catalogs" in page and "lvms-operator" in page


def test_search_fragment_for_live_search(tmp_path):
    c, _ = app_client(tmp_path)
    params = {"q": "lvms", "catalog": "redhat-operator-index", "disconnected_only": "true"}
    frag = c.get("/search", params=params).text
    assert "<html" not in frag and "Find operators" not in frag     # just the results section
    assert "lvms-operator" in frag and "mcg-operator" not in frag
    # forms inside the fragment return to the page URL for this search, not /search
    assert 'name="back_to" value="/?q=lvms&amp;catalog=redhat-operator-index&amp;disconnected_only=true"' in frag
    page = c.get("/", params=params).text
    assert 'id="operator-results"' in page and 'id="search-form"' in page
    assert frag.split("result(s)")[0][-40:] in page                  # the page renders the same results
    assert "No operators found" in c.get("/search", params={"q": "zzz-nothing"}).text


def test_import_round_trip_drops_automatic_images(tmp_path):
    s = parse_configs(write_fbc(tmp_path), IMAGE)
    plan = imageset.MirrorPlan(platform=imageset.Platform(channel="stable-4.22", min_version="4.22.14",
                                                          max_version="4.22.15", graph=False),
                               additional_images=["quay.io/me/tool:1"], archive_size_gb=8)
    plan.add(IMAGE, "lvms-operator", ["stable-4.21"])
    plan.add(IMAGE, "odf-operator")
    text = yaml.safe_dump(imageset.render(plan, {IMAGE: s}, {"odf-operator": ["odf-dependencies"]}, BASE))

    back, warnings = imageset.import_isc(text)
    assert warnings == []
    assert back.platform == plan.platform and back.archive_size_gb == 8
    names = {p.name: p.channels for p in back.catalogs[0].packages}
    assert names["lvms-operator"] == ["stable-4.21"]
    # dependencies come back as explicit selections from a generated file
    assert {"odf-operator", "odf-dependencies", "mcg-operator", "rook-ceph-operator"} <= set(names)
    assert BASE[0] in back.additional_images and "quay.io/me/tool:1" in back.additional_images


V1_ISC = """
kind: ImageSetConfiguration
apiVersion: mirror.openshift.io/v1alpha2
storageConfig:
  local: {path: ./metadata}
mirror:
  platform:
    graph: true
    channels:
      - {name: eus-4.22, minVersion: 4.22.10, maxVersion: 4.22.15}
      - {name: stable-4.21}
  operators:
    - catalog: registry.redhat.io/redhat/redhat-operator-index:v4.22
      packages:
        - name: lvms-operator
          channels: [{name: stable-4.22, minVersion: 4.22.0}]
        - name: kubevirt-hyperconverged
    - catalog: quay.example.com/ops/partner-index:v1
      packages: [{name: partner-operator}]
  additionalImages:
    - name: registry.redhat.io/ubi9/ubi:latest
    - name: quay.example.com/ops/tool:2
  helm:
    repositories: [{name: x, url: https://example.com}]
"""


def test_import_v1_file_with_warnings():
    plan, warnings = imageset.import_isc(V1_ISC)
    assert plan.platform == imageset.Platform(channel="eus-4.22", min_version="4.22.10", max_version="4.22.15", graph=True)
    assert [c.image for c in plan.catalogs] == ["registry.redhat.io/redhat/redhat-operator-index:v4.22",
                                                "quay.example.com/ops/partner-index:v1"]
    assert plan.catalogs[0].get("kubevirt-hyperconverged").channels == []
    joined = " | ".join(warnings)
    for expected in ("storageConfig", "stable-4.21 ignored", "version range ignored", "mirror.helm ignored"):
        assert expected in joined, expected


def test_import_rejects_other_files():
    import pytest
    for bad in ("kind: Pod\napiVersion: v1\n", "{{ not yaml", "- just\n- a list\n"):
        with pytest.raises(imageset.ISCImportError):
            imageset.import_isc(bad)


def test_import_endpoint(tmp_path):
    c, mirror = app_client(tmp_path)
    r = c.post("/import", files={"file": ("isc.yaml", V1_ISC, "application/yaml")}, data={"back_to": "/"},
               headers={"X-FH-Fetch": "1"})
    note = r.json()
    assert note["msg"].startswith("Imported isc.yaml: eus-4.22 4.22.10-4.22.15, 3 operators from 2 catalogs")
    assert "scan quay.example.com/ops/partner-index:v1" in note["warn"]
    plan = yaml.safe_load((mirror / "plan.yaml").read_text())
    assert plan["extra_catalogs"] == [{"image": "quay.example.com/ops/partner-index:v1", "insecure": False}]
    # the base image is added automatically, so only the partner tool stays a manual image
    assert plan["additional_images"] == ["quay.example.com/ops/tool:2"]
    page = c.get("/").text
    assert "Import ImageSetConfiguration" in page and page.index("Import ImageSetConfiguration") > page.index("imageset-config.yaml</h2>")

    r = c.post("/import", files={"file": ("pod.yaml", "kind: Pod\n", "application/yaml")}, follow_redirects=False)
    assert r.status_code == 303 and "err=pod.yaml" in r.headers["location"]


COMPANIONS = {"odf-operator": ["odf-dependencies"]}


def test_fold_restores_original_selections_and_same_output(tmp_path):
    s = parse_configs(write_fbc(tmp_path), IMAGE)
    plan = imageset.MirrorPlan()
    plan.add(IMAGE, "lvms-operator", ["stable-4.21"])
    plan.add(IMAGE, "odf-operator")
    rendered = imageset.render(plan, {IMAGE: s}, COMPANIONS, BASE)

    back, _ = imageset.import_isc(yaml.safe_dump(rendered))
    assert len(back.catalogs[0].packages) == 5            # dependencies came back as selections
    folded = imageset.fold_dependencies(back, {IMAGE: s}, COMPANIONS)
    assert sorted(folded) == ["mcg-operator", "odf-dependencies", "rook-ceph-operator"]
    assert {p.name: p.channels for p in back.catalogs[0].packages} == {
        "lvms-operator": ["stable-4.21"], "odf-operator": ["stable-4.22"]}
    again = imageset.render(back, {IMAGE: s}, COMPANIONS, BASE)
    assert again["mirror"]["operators"] == rendered["mirror"]["operators"]   # same mirrored content


def test_fold_keeps_deliberate_channel_and_unscanned_catalogs(tmp_path):
    s = parse_configs(write_fbc(tmp_path), IMAGE)
    plan = imageset.MirrorPlan()
    plan.add(IMAGE, "odf-operator")
    plan.add(IMAGE, "mcg-operator", ["stable-4.21"])          # a dependency, but not on its default channel
    plan.add("quay.example.com/ops/other:v1", "anything")       # catalog not scanned
    assert imageset.fold_dependencies(plan, {IMAGE: s}, COMPANIONS) == []
    assert plan.catalog(IMAGE).get("mcg-operator") is not None
    assert plan.catalog("quay.example.com/ops/other:v1").get("anything") is not None


def test_fold_dependency_loop_keeps_one_member(tmp_path):
    s = parse_configs(write_fbc(tmp_path), IMAGE)
    loop = {"lvms-operator": ["mcg-operator"], "mcg-operator": ["lvms-operator"]}
    plan = imageset.MirrorPlan()
    plan.add(IMAGE, "lvms-operator")
    plan.add(IMAGE, "mcg-operator")
    assert imageset.fold_dependencies(plan, {IMAGE: s}, loop) == ["lvms-operator"]
    assert [p.name for p in plan.catalog(IMAGE).packages] == ["mcg-operator"]
    out = imageset.render(plan, {IMAGE: s}, loop)
    assert {p["name"] for p in out["mirror"]["operators"][0]["packages"]} == {"lvms-operator", "mcg-operator"}


def test_import_endpoint_reports_folded_dependencies(tmp_path):
    c, mirror = app_client(tmp_path)
    s = parse_configs(write_fbc(tmp_path / "f"), IMAGE)
    plan = imageset.MirrorPlan(platform=imageset.Platform(channel="stable-4.22", min_version="4.22.15", max_version="4.22.15"))
    plan.add(IMAGE, "odf-operator")
    text = yaml.safe_dump(imageset.render(plan, {IMAGE: s}, COMPANIONS, ["registry.redhat.io/ubi9/ubi:latest"]))
    note = c.post("/import", files={"file": ("isc.yaml", text, "application/yaml")}, headers={"X-FH-Fetch": "1"}).json()
    assert "1 operator from 1 catalog plus 3 as dependencies, 0 own images" in note["msg"]
    saved = yaml.safe_load((mirror / "plan.yaml").read_text())
    assert [p["name"] for p in saved["catalogs"][0]["packages"]] == ["odf-operator"]
    assert "Added as dependencies" in c.get("/").text
