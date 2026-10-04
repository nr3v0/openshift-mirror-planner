import pytest

from fakeregistry import FakeRegistry, catalog_files, layer
from mirror_planner.catalog import CatalogCache
from mirror_planner.registry import ImageRef, RegistryError, extract_path, load_credentials

IMG = "registry.lab.example/ops/index:v1"


def test_image_ref_parsing():
    assert ImageRef.parse("registry.redhat.io/redhat/redhat-operator-index:v4.22") == \
        ImageRef("registry.redhat.io", "redhat/redhat-operator-index", "v4.22")
    assert ImageRef.parse("localhost:5000/a/b@sha256:abc") == ImageRef("localhost:5000", "a/b", "sha256:abc")
    assert ImageRef.parse("quay.io/x/y").reference == "latest"
    with pytest.raises(RegistryError):
        ImageRef.parse("library/ubuntu:22.04")


def test_bearer_login_index_layers_and_whiteouts(tmp_path):
    reg = FakeRegistry()
    base = catalog_files("alpha", "beta", "gamma") | {"configs/beta/extra.json": b"{}", "usr/bin/opm": b"binary"}
    upper = {"configs/alpha/.wh.catalog.json": None,                 # file whiteout
             "configs/beta/.wh..wh..opq": None,                       # opaque: drop lower beta/*
             "configs/beta/catalog.json": catalog_files("beta")["configs/beta/catalog.json"],
             "configs/.wh.gamma": None,                               # directory whiteout
             "./configs/delta/catalog.json": catalog_files("delta")["configs/delta/catalog.json"]}
    reg.add_image("ops/index", "v1", [layer(base), layer(upper, compress=False)])
    seen = []
    used = extract_path(IMG, None, tmp_path / "out", authfile=reg.authfile(tmp_path / "auth.json"),
                        progress=lambda d, t: seen.append((d, t)), transport=reg.transport())
    files = sorted(str(p.relative_to(tmp_path / "out")) for p in (tmp_path / "out").rglob("*") if p.is_file())
    assert used == "/configs"
    assert files == ["beta/catalog.json", "delta/catalog.json"]          # nothing outside /configs, whiteouts applied
    assert seen[-1][0] == seen[-1][1] > 0                                # progress reaches the total
    assert any("auth.lab.example" in r for r in reg.requests)            # bearer token fetched


def test_label_path_and_plain_manifest(tmp_path):
    reg = FakeRegistry(require_auth=False)
    reg.add_image("ops/index", "v1", [layer({"var/lib/catalog/one/catalog.json": b"{}"})],
                  label_path="/var/lib/catalog", index=False)
    assert extract_path(IMG, None, tmp_path / "out", transport=reg.transport()) == "/var/lib/catalog"
    assert (tmp_path / "out" / "one" / "catalog.json").exists()


def test_corrupt_layer_and_bad_login(tmp_path):
    import hashlib
    reg = FakeRegistry()
    gz = layer(catalog_files("alpha"))
    plain = layer(catalog_files("beta"), compress=False)
    reg.add_image("ops/index", "v1", [gz, plain])
    auth = reg.authfile(tmp_path / "auth.json")
    for bad in (gz, plain):  # a damaged gzip stream, and a valid stream with the wrong digest
        reg.corrupt = {"sha256:" + hashlib.sha256(bad[1]).hexdigest()}
        with pytest.raises(RegistryError, match="digest check|unreadable"):
            extract_path(IMG, None, tmp_path / "a", authfile=auth, transport=reg.transport())
    reg.corrupt = set()
    reg.password = "changed"
    with pytest.raises(RegistryError, match="login refused"):
        extract_path(IMG, None, tmp_path / "b", authfile=auth, transport=reg.transport())
    with pytest.raises(RegistryError, match="not found"):
        extract_path("registry.lab.example/ops/missing:v1", None, tmp_path / "c",
                     transport=FakeRegistry(require_auth=False).transport())


def test_credentials_lookup(tmp_path):
    reg = FakeRegistry()
    path = reg.authfile(tmp_path / "auth.json")
    assert load_credentials(path, "registry.lab.example") == ("me", "secret")
    assert load_credentials(path, "quay.io") is None and load_credentials(None, "x") is None


def test_catalog_cache_scan_reports_progress(tmp_path):
    reg = FakeRegistry()
    reg.add_image("ops/index", "v1", [layer(catalog_files("alpha", "beta"))])
    cache = CatalogCache(tmp_path / "cache", authfile=reg.authfile(tmp_path / "auth.json"), transport=reg.transport())
    phases = []
    s = cache.refresh(IMG, progress=lambda phase, frac, detail: phases.append((phase, frac)))
    assert [p.name for p in s.packages] == ["alpha", "beta"]
    assert ("extracting", 1.0) in phases and phases[-1] == ("parsing", 1.0)
    assert cache.load(IMG).packages[0].name == "alpha"
    assert not (tmp_path / "cache" / "registry.lab.example-ops-index-v1" / "configs").exists()
