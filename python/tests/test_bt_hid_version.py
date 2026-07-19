"""The wheel carries a PEP 440 / SemVer version, single-sourced from src/hidbridge/_version.py.

The release build (release-wheel.yml) rewrites _version.py from the `wheel-v<X.Y.Z>` git tag before
`uv build`; an untagged tree builds the baseline. This test just pins the contract the packaging
relies on: `hidbridge.__version__` exists and is a valid release version string.
"""

import re

import hidbridge

# PEP 440 release core (X.Y.Z) with optional pre/dev suffix -- what the tag-driven build produces.
_SEMVER = re.compile(r"^\d+\.\d+\.\d+([.-]?(a|b|rc|dev|post)\d+)?$")


def test_version_attribute_is_semver():
    assert isinstance(hidbridge.__version__, str)
    assert _SEMVER.match(hidbridge.__version__), hidbridge.__version__


def test_version_baseline_is_one_zero_zero():
    # The committed baseline (untagged tree) is 1.0.0; releases inject a higher version at build time.
    assert hidbridge.__version__ == "1.0.0"
