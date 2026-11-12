# Publishing

hidbridge is developed on the maintainer's private Forgejo (the source of truth) and published
through a public GitHub mirror. These are the publishing paths:

| Path | Trigger | Target | Auth | Status |
| --- | --- | --- | --- | --- |
| Forgejo Actions (`.forgejo/workflows/release-wheel.yml`) | push tag `wheel/release/X.Y.Z` | private Forgejo PyPI registry | CI secret `PACKAGE_REGISTRY_TOKEN` | Live |
| Forgejo Actions (`.forgejo/workflows/release-firmware.yml`) | push tag `fw/release/X.Y.Z` | private Forgejo generic registry | CI secret `PACKAGE_REGISTRY_TOKEN` | Live |
| Forgejo Actions (`.forgejo/workflows/ci.yml`) | push to `main`, pull requests | ruff + offline pytest, firmware compile | none | Live |
| GitHub Actions (`.github/workflows/pypi.yml`) | the mirrored `wheel/release/X.Y.Z` tag push | public PyPI (`hidbridge`) | Trusted Publishing (OIDC, no token) | Not set up |
| GitHub Actions (`.github/workflows/firmware-release.yml`) | the mirrored `fw/release/X.Y.Z` tag push | a GitHub Release with the `.bin`/`.uf2` files | the workflow's `GITHUB_TOKEN` (`contents: write`) | Not set up |
| GitHub Actions (`.github/workflows/test.yml`) | push to `main`, pull requests | ruff + offline pytest on Python 3.11-3.13 | none | Not set up |
| Forgejo push mirror | every push to `main`, plus tags | <https://github.com/martinmunk/hidbridge> | GitHub fine-grained PAT (stored in Forgejo only) | Not set up |

**End-state flow:** Forgejo is the source of truth. Pushing a `wheel/release/X.Y.Z` tag publishes
the wheel + sdist to the **private** Forgejo PyPI registry; the push mirror carries the same tag to
GitHub, where the tag push triggers `pypi.yml`, which publishes the same version to **public** PyPI
via Trusted Publishing. Likewise a `fw/release/X.Y.Z` tag publishes the firmware to the private
generic registry on Forgejo and, on GitHub, `firmware-release.yml` builds the same envs and attaches
the same versioned files to a GitHub Release named after the tag. One tag, both sides, no manual step
on GitHub. (Pushes made by a mirror with a personal access token do trigger GitHub workflows.)

Why PyPI is published from GitHub and not from Forgejo: PyPI's Trusted Publishing accepts
short-lived identity tokens only from GitHub Actions, GitLab, Google Cloud and ActiveState.
Publishing from Forgejo would need a long-lived PyPI API token stored as a CI secret.

**Version comes from the tag.** Both wheel workflows stamp `python/src/hidbridge/_version.py` from
the tag (`wheel/release/1.2.3` -> `__version__ = "1.2.3"`) before building; the committed value is
only the untagged baseline. Both firmware workflows inject the tag's version with
`PLATFORMIO_BUILD_FLAGS=-DHB_FW_VERSION='"1.2.3"'`, so `hello.fw` reports it. Details:
[`docs/releasing.md`](docs/releasing.md).

> ### `.forgejo/workflows/` and `.github/workflows/` coexist
> Both directories live in this repo. Forgejo gives `.forgejo/workflows/` **precedence** and
> **ignores** `.github/workflows/` when `.forgejo` exists, so only the Forgejo jobs run on Forgejo.
> GitHub, in turn, only runs `.github/workflows/`. There is no conflict, and no special handling is
> needed when mirroring the repo to GitHub.

> ### Nothing personal or infra-specific in tracked files
> The repo is public via the mirror: no LAN IPs, internal hostnames, device MACs or personal names in
> anything committed. Docs use `192.0.2.x` (RFC 5737) and `AA:BB:CC:DD:EE:xx` placeholders; the
> Forgejo workflows read real addresses from repo variables (`HB_REGISTRY_BASE`, `HB_BENCH_*`; see
> `docs/releasing.md`).

---

## 1. Forgejo private registries (automated, already working)

`release-wheel.yml` and `release-firmware.yml` run on any `wheel/release/*` / `fw/release/*` tag and
upload to the Forgejo PyPI / generic registries as user `martinmunk` with the CI secret
`PACKAGE_REGISTRY_TOKEN`. The registry base URL comes from the repo variable `HB_REGISTRY_BASE`
(the plain-HTTP `:3000` address the runner can reach). Releasing is just a tag push:

```bash
git tag wheel/release/1.2.3 && git push origin wheel/release/1.2.3
git tag fw/release/2.0.1    && git push origin fw/release/2.0.1
```

---

## 2. Public PyPI via Trusted Publishing

`.github/workflows/pypi.yml` gates (ruff + offline pytest), builds (`python -m build`), runs
`twine check`, and uploads `python/dist/` with `pypa/gh-action-pypi-publish` using
[Trusted Publishing](https://docs.pypi.org/trusted-publishers/) (OIDC). No API token is stored. It
uses `skip-existing`, so re-running a tag that is already on PyPI is a no-op.

### One-time manual setup on PyPI

Because Trusted Publishing has to be configured before the project exists on PyPI, set it up as a
**pending publisher**:

1. Sign in at <https://pypi.org> with the account that will own the project.
2. Go to **Account settings -> Publishing -> Add a pending publisher** (GitHub tab).
3. Fill in:
   - **PyPI Project Name:** `hidbridge`
   - **Owner:** `martinmunk`
   - **Repository name:** `hidbridge`
   - **Workflow name:** `pypi.yml`
   - **Environment name:** `pypi`
4. Save. Also create the `pypi` environment under the GitHub repo's **Settings -> Environments**
   (the workflow references `environment: pypi`; optionally add a required reviewer there as a
   manual gate before each public upload).

Do **not** add a `PYPI_API_TOKEN` secret; Trusted Publishing replaces it. A version that exists on
PyPI can never be re-uploaded or changed, so a botched public release is fixed with a new version.
The first public release should be a new tag (e.g. `wheel/release/1.2.3`, after the last private
`1.2.2`) cut once the mirror and the pending publisher are in place.

> The maintainer must perform the PyPI account steps; they cannot be automated here, and publishing
> to public PyPI is irreversible.

---

## 3. GitHub repo and push mirror

The public copy lives at <https://github.com/martinmunk/hidbridge>. It is fed only by the Forgejo
push mirror; nobody pushes to GitHub directly. Because the workflows already live under
`.github/workflows/`, GitHub picks them up as mirrored.

### Setup

1. **GitHub repo** `martinmunk/hidbridge`, created **empty** and public: no README, no
   `.gitignore`, and licence set to *None* (the repo already carries `LICENSE`; letting
   GitHub add a file would create a commit the mirror cannot fast-forward onto).
2. **GitHub fine-grained PAT**, repository access limited to `hidbridge`, with
   **Contents: read and write** *and* **Workflows: read and write**. The second permission is
   required because the mirror pushes commits that touch `.github/workflows/`; without it GitHub
   rejects the push with "refusing to allow a Personal Access Token to create or update workflow".
3. **Forgejo push mirror** on this repo (web form: **Settings -> Repository -> Mirror settings ->
   Push mirror**, or the API below): remote `https://github.com/martinmunk/hidbridge.git`, username
   `martinmunk`, the PAT as the password, sync on commit, interval 8h, **branch filter `main`**.
   The branch filter is opt-in: only the listed branches are ever mirrored. The private `feat/*`
   branches (e.g. `feat/directed-advertising`) are **not** mirrored -- never use a catch-all filter.
   Replace `<forgejo>` with the Forgejo base URL:
   ```bash
   curl -X POST \
     <forgejo>/api/v1/repos/martinmunk/hidbridge/push_mirrors \
     -H "Authorization: token <FORGEJO_TOKEN>" \
     -H "Content-Type: application/json" \
     -d '{
           "remote_address": "https://github.com/martinmunk/hidbridge.git",
           "remote_username": "martinmunk",
           "remote_password": "<github-pat>",
           "sync_on_commit": true,
           "interval": "8h0m0s",
           "branch_filter": "main"
         }'
   # force a sync now:
   curl -X POST -H "Authorization: token <FORGEJO_TOKEN>" \
     <forgejo>/api/v1/repos/martinmunk/hidbridge/push_mirrors-sync
   # last_error of the mirror shows why a push was rejected:
   curl -H "Authorization: token <FORGEJO_TOKEN>" \
     <forgejo>/api/v1/repos/martinmunk/hidbridge/push_mirrors
   ```
4. **`pypi` environment** created on the GitHub repo (Settings -> Environments); `pypi.yml`
   references it and PyPI's trusted publisher is bound to it.

**Tags ARE mirrored** along with the filtered branch, so a `wheel/release/*` or `fw/release/*` tag
pushed to Forgejo arrives on GitHub and triggers `pypi.yml` / `firmware-release.yml` there. The
**pre-existing** tags (`wheel/release/1.0.0` .. `1.2.2`, `fw/release/1.0.0` .. `2.0.0`) will be
mirrored too, but they point at commits that have no `.github/workflows/`, and GitHub runs the
workflow files *of the tagged commit* -- so the first sync does **not** publish anything. Only tags
cut after this file landed trigger public releases.

---

## Status (summary)

- [ ] GitHub repo `martinmunk/hidbridge` created empty (no README/licence) and a fine-grained PAT (Contents + Workflows read/write).
- [ ] Forgejo push mirror, branch filter `main`; first sync done, GitHub `test` workflow green.
- [ ] Forgejo repo variables `HB_REGISTRY_BASE` (and, for `hil.yml`, `HB_BENCH_NET_HOST`, `HB_BENCH_OTA_HOST`, `HB_BENCH_ESP32_PORT`) set.
- [ ] PyPI pending publisher for `hidbridge` (workflow `pypi.yml`, environment `pypi`) and the `pypi` environment on GitHub.
- [ ] First public wheel release (a new `wheel/release/X.Y.Z` tag) on PyPI from one tag push.
- [ ] First public firmware release (a new `fw/release/X.Y.Z` tag) as a GitHub Release.
