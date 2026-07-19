"""Single source of the wheel version.

BASELINE = the committed default (an untagged `uv build` produces this). The release build
(.forgejo/workflows/release-wheel.yml) REWRITES this line from the `wheel-v<X.Y.Z>` git tag before
`uv build`, so the published wheel's version derives from the tag -- the tag is the source of truth.
The firmware version is SEPARATE (baked via a PlatformIO flag; see firmware/*/src/main.cpp), and the
wire-compat integer `proto` is separate again (see bridge.MIN_FIRMWARE_PROTO). SemVer; keep it a plain
release string here so `hatchling` reads it as the package version. See docs/releasing.md.
"""

__version__ = "1.0.0"
