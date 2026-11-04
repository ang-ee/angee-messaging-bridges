"""Actual compiler composition proof beside native source-proxy behavior tests."""

import os
import subprocess
import sys
from importlib.util import find_spec
from pathlib import Path


def test_discord_native_composed_host(tmp_path):
    root = Path(__file__).resolve().parents[1]
    spec = find_spec("angee")
    assert spec is not None and spec.submodule_search_locations
    framework = next(
        candidate
        for location in spec.submodule_search_locations
        if (candidate := Path(location).resolve().parents[1]).joinpath("tests/composed_host.py").is_file()
    )
    env = dict(os.environ)
    env.pop("DJANGO_SETTINGS_MODULE", None)
    result = subprocess.run(
        [sys.executable, str(framework / "tests/composed_host.py"),
         "--runtime-dir", str(tmp_path / "runtime"), "--app", "angee.messaging_integrate_discord",
         "--addon-dir", str(root / "src"), "--no-examples", "--action", "tests",
         "--test-label", "angee.messaging_integrate_discord.tests", "--output", str(tmp_path / "result.json")],
        cwd=framework, env=env, capture_output=True, text=True, timeout=1200,
    )
    assert result.returncode == 0, result.stdout + result.stderr
