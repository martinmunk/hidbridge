"""The wheel carries a PEP 440 / SemVer version, single-sourced from src/hidbridge/_version.py.

The release build (release-wheel.yml) rewrites _version.py from the `wheel/release/<X.Y.Z>` git tag
before `uv build`, THEN re-runs this gate -- so nothing here may hardcode a specific version, or every
release but the baseline would fail (it did: wheel/release/1.0.1 run #10/#11). This test pins the
contract the packaging relies on: `hidbridge.__version__` exists, is a valid release version string,
and matches the single source (_version.py) at whatever version the tree currently carries.
"""

import pathlib
import re

import hidbridge

# PEP 440 release core (X.Y.Z) with optional pre/dev suffix -- what the tag-driven build produces.
_SEMVER = re.compile(r"^\d+\.\d+\.\d+([.-]?(a|b|rc|dev|post)\d+)?$")


def test_version_attribute_is_semver():
    assert isinstance(hidbridge.__version__, str)
    assert _SEMVER.match(hidbridge.__version__), hidbridge.__version__


def test_version_matches_single_source():
    # __version__ is single-sourced from _version.py; they must AGREE at any version (baseline 1.0.0
    # untagged, or a higher version the release build stamps in) -- never hardcode a literal here.
    vf = pathlib.Path(__file__).resolve().parents[1] / "src" / "hidbridge" / "_version.py"
    m = re.search(r'__version__\s*=\s*"([^"]+)"', vf.read_text())
    assert m, "no __version__ in _version.py"
    assert hidbridge.__version__ == m.group(1)
