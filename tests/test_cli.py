from pathlib import Path

import yaml

from mirror_planner import cli, imageset


def _plan(tmp_path: Path) -> Path:
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    imageset.MirrorPlan().save(mirror / "plan.yaml")
    fake = tmp_path / "oc-mirror"
    fake.write_text(f"#!/usr/bin/bash\necho \"$@\" > {tmp_path}/args\n")
    fake.chmod(0o755)
    return mirror


def test_mirror_uses_env_cache_without_flag(tmp_path, monkeypatch):
    mirror = _plan(tmp_path)
    monkeypatch.setenv("OC_MIRROR_CACHE", str(tmp_path / "cache"))
    assert cli.main(["mirror", "--mirror-dir", str(mirror), "--oc-mirror", str(tmp_path / "oc-mirror"), "--dry-run"]) == 0
    args = (tmp_path / "args").read_text()
    assert "--cache-dir" not in args and "--dry-run" in args and f"file://{mirror.resolve()}" in args
    assert yaml.safe_load((mirror / "imageset-config.yaml").read_text())["kind"] == "ImageSetConfiguration"


def test_mirror_default_cache_dir_without_env(tmp_path, monkeypatch):
    mirror = _plan(tmp_path)
    monkeypatch.delenv("OC_MIRROR_CACHE", raising=False)
    cli.main(["mirror", "--mirror-dir", str(mirror), "--oc-mirror", str(tmp_path / "oc-mirror")])
    assert f"--cache-dir {tmp_path.resolve()}/oc-mirror-cache" in (tmp_path / "args").read_text()


def test_mirror_hides_registry_variables_from_oc_mirror(tmp_path, monkeypatch):
    mirror = _plan(tmp_path)
    (tmp_path / "oc-mirror").write_text(f"#!/usr/bin/bash\nenv > {tmp_path}/env\necho \"$@\" > {tmp_path}/args\n")
    monkeypatch.setenv("REGISTRY_AUTH_FILE", "/run/secrets/pull-secret.json")
    monkeypatch.delenv("PULL_SECRET_FILE", raising=False)
    cli.main(["mirror", "--mirror-dir", str(mirror), "--oc-mirror", str(tmp_path / "oc-mirror")])
    assert "REGISTRY_" not in (tmp_path / "env").read_text()
    assert "--authfile /run/secrets/pull-secret.json" in (tmp_path / "args").read_text()
