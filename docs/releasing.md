# Releasing hidbridge (SemVer + Forgejo Actions)

hidbridge is a **monorepo** with two independently-shipped artifacts:

- the **Python wheel** (`python/`), and
- the **firmware** images (`firmware/esp32/`, `firmware/pico/`).

They share one strict contract (the HID report descriptor + the host<->MCU JSON-RPC protocol, guarded
by parity tests), which is *why* they live together -- but they are versioned and released
**independently**. Both start at **1.0.0** and move on their own SemVer clocks after that.

## Three version numbers, deliberately separate

| Number | Where | Source of truth | Bumps when |
| --- | --- | --- | --- |
| **wheel version** | `hidbridge.__version__` / the wheel filename | the `wheel/release/<X.Y.Z>` git tag | you release the Python package |
| **firmware version** | `hello.fw` (baked via `-DHB_FW_VERSION`) | the `fw/release/<X.Y.Z>` git tag | you release the firmware |
| **`proto`** (wire-compat integer) | `hello.proto` / `hbmode1::PROTO_VERSION` | the firmware source | **only** on a *breaking* protocol change |

`proto` is **not** the release version. The host<->MCU protocol is additive (a new capability is a new
`caps` entry; the host feature-detects on `caps`, not on the version), and `proto` is bumped only when
old firmware can no longer be spoken to. The wheel's `bridge.MIN_FIRMWARE_PROTO` is the oldest `proto`
it supports. Full policy: [`bt-hid-protocol.md`](bt-hid-protocol.md). So you can ship wheel 1.4.0 and
firmware 1.1.0 against `proto` 1 simultaneously -- three numbers, three clocks.

## Tag conventions (the tag IS the version)

| Tag | Triggers | Publishes |
| --- | --- | --- |
| `wheel/release/1.0.0` | `.forgejo/workflows/release-wheel.yml` | wheel + sdist -> Forgejo PyPI registry |
| `fw/release/1.0.0` | `.forgejo/workflows/release-firmware.yml` | esp32 + pico `.bin`/`.uf2` -> Forgejo generic registry |

Prefixed tags are what make one repo serve two release lines: a `wheel/release/*` tag never triggers a
firmware build and vice versa.

### Wheel version wiring

The wheel version is single-sourced from `python/src/hidbridge/_version.py` (committed baseline
`1.0.0`). `pyproject.toml` declares `dynamic = ["version"]` and reads that file via
`[tool.hatch.version]`. A plain `uv build` on an untagged tree therefore produces the baseline
`1.0.0`. The release workflow rewrites the `__version__` line from the tag (`wheel/release/1.2.3` -> `1.2.3`)
with `sed` before `uv build`, so the published wheel's version derives from the tag.

### Firmware version wiring

Each firmware `main.cpp` defines `HB_FW_VERSION` (default `"1.0.0-dev"`) and reports it in the `hello`
handshake as `fw`. The release workflow injects the tag's version through PlatformIO:
`PLATFORMIO_BUILD_FLAGS=-DHB_FW_VERSION='"1.2.3"'`, which bakes `1.2.3` into `hello.fw` for both the
esp32 and pico builds. (The OTA live test reuses the same mechanism to prove an over-the-air swap.)

## How to cut a release

Everything is driven by pushing a tag; the pipeline builds, gates, and publishes.

### Wheel

```bash
git tag wheel/release/1.0.0
git push origin wheel/release/1.0.0
```

`release-wheel.yml` then: stamps `_version.py` from the tag -> `ruff check .` + offline `pytest -q` ->
`uv build` -> `twine check` -> `twine upload` to the Forgejo PyPI registry. Re-pushing an
already-published version is idempotent (a "file already exists" upload is treated as success).

### Firmware

```bash
git tag fw/release/1.0.0
git push origin fw/release/1.0.0
```

`release-firmware.yml` then builds `esp32dev`, `esp32-ethernet-kit`, `esp32-wifi`, and `rpipico2w`
with `-DHB_FW_VERSION` injected and PUTs the versioned artifacts
(`hidbridge-esp32-ethernet-kit-1.0.0.bin`, `hidbridge-pico-1.0.0.uf2`, ...) to the generic registry.
Upload is idempotent (`200/201` ok, `409` "already exists" non-fatal).

> Tags are the source of truth, so a botched release is fixed by deleting the tag + registry package
> and re-tagging, not by hand-editing versions.

## Consuming a release

### The wheel (Forgejo PyPI registry)

```bash
pip install --extra-index-url http://forgejo.example.com:3000/api/packages/martinmunk/pypi/simple/ --trusted-host forgejo.example.com hidbridge
pip install --extra-index-url http://forgejo.example.com:3000/api/packages/martinmunk/pypi/simple/ --trusted-host forgejo.example.com "hidbridge[mcu]"   # + pyserial for the Pico/ESP32 serial backend
```

Persist it in a project instead of typing the index each time:

`requirements.txt`
```
--extra-index-url http://forgejo.example.com:3000/api/packages/martinmunk/pypi/simple/
--trusted-host forgejo.example.com
hidbridge[mcu]
```

`pip.conf` (`~/.config/pip/pip.conf`)
```ini
[global]
extra-index-url = http://forgejo.example.com:3000/api/packages/martinmunk/pypi/simple/
trusted-host = forgejo.example.com
```

`uv` (`pyproject.toml`)
```toml
[[tool.uv.index]]
name = "forgejo"
url = "http://forgejo.example.com:3000/api/packages/martinmunk/pypi/simple/"
```
then `uv add --index forgejo hidbridge`.

> **Auth assumption:** these examples assume the registry is **public-read** (like this instance's
> Debian registry). If read is gated behind a token, use the credentialed index URL
> `http://<user>:<token>@forgejo.example.com:3000/api/packages/martinmunk/pypi/simple/`
> or add a `~/.netrc` entry for the host. The plain `http://` (not `https://`) is required: HTTPS/443
> is unreachable on this LAN; add the host to pip's `trusted-host` if pip warns.

### The firmware (Forgejo generic registry)

```bash
V=1.0.0
BASE=http://forgejo.example.com:3000/api/packages/martinmunk/generic/hidbridge-firmware/$V
curl -O "$BASE/hidbridge-esp32-ethernet-kit-$V.bin"
curl -O "$BASE/hidbridge-pico-$V.uf2"
```

Flash the esp32 `.bin` with `esptool`/`pio run -t upload`; drop the pico `.uf2` onto the BOOTSEL
mass-storage device.

## Setup the user must do once

### Register a Forgejo runner (for `ci.yml` + the two release workflows)

These workflows use `runs-on: docker` and run inside a container -- the same convention as the repo's
other `.forgejo/workflows/deb.yml` pipelines. Register a runner that advertises the `docker` label:

```bash
# on the runner host (needs Docker)
forgejo-runner register \
  --instance  http://forgejo.example.com:3000 \
  --token     <RUNNER_REGISTRATION_TOKEN_from_the_repo/org_Actions_settings> \
  --name      hidbridge-ci \
  --labels    docker:docker://python:3.13-slim
forgejo-runner daemon
```

The label's left side (`docker`) is what `runs-on:` matches; the right side is the default image. The
workflows install everything they need at run time (they apt-install `nodejs`+`git` before
`actions/checkout`, then `pip install` uv/twine/platformio), so any Debian-based image works.

### Set the registry token secret

Both release workflows publish with `${{ secrets.PACKAGE_REGISTRY_TOKEN }}` (a Forgejo token with
`write:package` scope) as user `martinmunk` -- the **same secret name** the deb pipelines already use.
Add it under the repo's *Settings -> Actions -> Secrets* as `PACKAGE_REGISTRY_TOKEN`. The workflows
fail with a clear message if it is unset.

> The workflows use only the standard `${{ github.* }}` context (`github.ref_name`, `github.sha`,
> `github.event_name`) plus that one secret -- no Forgejo-specific context is required.

### Future: the hardware-in-the-loop runner (`hil.yml`)

`hil.yml` is a **stub** for the on-bench hardware gate (the `*_live.py` tests, the central self-test,
the OTA loop) -- the tests that cannot run on the docker CI runner. It is `workflow_dispatch`-only and
is not expected to pass until a bench runner exists. To wire it up, register a **self-hosted** runner
directly on the bench host (not in a container, so it reaches the USB serial ports + LAN boards +
bonded iPad) with the labels `self-hosted,hidbridge-bench`:

```bash
forgejo-runner register --instance http://forgejo.example.com:3000 \
  --token <TOKEN> --name hidbridge-bench --labels self-hosted,hidbridge-bench
```

Then edit the `env:` block in `hil.yml` to the live ports/IPs (they drift between sessions) and ensure
the net token is present at `~/.config/bt_hid/net.token` (it is the TLS-PSK + OTA password, **not** a
CI secret). Gating env vars the tests read: `BT_HID_CENTRAL=1`, `BT_HID_OTA=1`, `BT_HID_ESP32_PORT`,
`BT_HID_ESP32_OTA_HOST`/`BT_HID_NET_HOST`, and the per-test `BT_HID_LIVE_*` vars.
