"""Single source of the wheel version.

BASELINE = the committed default (an untagged `uv build` produces this). The release builds
(.forgejo/workflows/release-wheel.yml for the private registry, .github/workflows/pypi.yml for
public PyPI) REWRITE this line from the `wheel/release/<X.Y.Z>` git tag before building, so the
published wheel's version derives from the tag -- the tag is the source of truth.
The firmware version is SEPARATE (baked via a PlatformIO flag; see firmware/*/src/main.cpp), and the
wire-compat integer `proto` is separate again (see bridge.MIN_FIRMWARE_PROTO). SemVer; keep it a plain
release string here so `hatchling` reads it as the package version. See docs/releasing.md.
"""

__version__ = "1.0.0"
