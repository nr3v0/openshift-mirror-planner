from pathlib import Path

import pytest
import yaml

from mirror_planner import cli, imageset


def test_imageset_renders_from_plan(tmp_path: Path):
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    plan = imageset.MirrorPlan(platform=imageset.Platform(channel="stable-4.22", min_version="4.22.15",
                                                          max_version="4.22.15"))
    plan.save(mirror / "plan.yaml")
    assert cli.main(["imageset", "--mirror-dir", str(mirror)]) == 0
    isc = yaml.safe_load((mirror / "imageset-config.yaml").read_text())
    assert isc["kind"] == "ImageSetConfiguration"
    assert isc["mirror"]["platform"]["channels"][0]["maxVersion"] == "4.22.15"
    assert (mirror / "oc-mirror.flags").read_text().strip() == ""


def test_imageset_without_plan_fails(tmp_path: Path):
    assert cli.main(["imageset", "--mirror-dir", str(tmp_path)]) == 1


def test_no_mirror_subcommand():
    with pytest.raises(SystemExit):
        cli.main(["mirror"])
