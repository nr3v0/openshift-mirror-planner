# CLAUDE.md

Guidance for Claude Code in this repository.

## What this is

OpenShift mirror planner: a small web app (and container image) that makes building an
**oc-mirror v2 `ImageSetConfiguration`** easy for disconnected OpenShift installs. Its only job is
the configuration. It must not run, wrap or bundle `oc-mirror` (or `oc`); mirroring happens
elsewhere (the sibling project `/opt/ocp4/ai-slop/factory-helper` runs `oc-mirror` with the
generated file via its `make mirror`, and runs this planner's container for `make mirror-ui` /
`make imageset` with `PLANNER_IMAGE`).

- GitHub: https://github.com/nr3v0/openshift-mirror-planner (public, Apache-2.0)
- Image: `quay.io/nr3v0/openshift-mirror-planner` (Quay builds from git tags)

## Release rule (always)

Every change that gets pushed is a release, because the Quay build trigger versions images
from git tags:

1. bump `__version__` in `mirror_planner/__init__.py` (semver) and the image tag in `README.md`
   (and in factory-helper's `Makefile` `PLANNER_IMAGE` if that project should follow);
2. commit (end the message with the Co-Authored-By trailer);
3. `git tag -a vX.Y.Z -m "..."` matching the version;
4. `git push origin main vX.Y.Z`.

Do **not** `podman push` to quay.io unless the user explicitly asks at that moment. Local
`make image` builds are for testing. Note: `gh` is aliased to `git log` in the user's shell; use
`/usr/bin/gh`. The quay trigger may expect `/Dockerfile` while this repo has `Containerfile`
(unconfirmed; if Quay builds fail, that's the first suspect).

## Layout

| Path | What |
| --- | --- |
| `mirror_planner/app.py` | FastAPI app: page, `/search` fragment, POST actions, scan jobs, `/import`, `/api/releases/versions` |
| `mirror_planner/registry.py` | OCI registry client: pull-secret creds, bearer/basic auth, image index → linux/amd64, streamed digest-checked layers, whiteouts; extracts the catalog's configs dir (from the `operators.operatorframework.io.index.configs.v1` label) |
| `mirror_planner/catalog.py` | FBC parsing (JSON streams + YAML, split layouts), channel heads, must-gather images, disconnected annotation, dependency resolution, `CatalogCache` summaries in `mirror/.catalogs/` |
| `mirror_planner/releases.py` | OpenShift update service (Cincinnati) channels/versions, cached |
| `mirror_planner/imageset.py` | `MirrorPlan`, render ISC, `automatic_images` (base + must-gathers), `import_isc`, `fold_dependencies` |
| `mirror_planner/presets.yaml` | suggested sets, `companions` (undeclared deps: odf-operator → odf-dependencies, ACM → MCE), `base_images` |
| `mirror_planner/cli.py` | `mirror-planner serve` and `imageset` only |
| `mirror_planner/templates/` | `base.html`, `macros.html` (PatternFly macros), `index.html`, `_operator_results.html` |
| `mirror_planner/static/` | `app.css` + vendored PatternFly 6 (`scripts/vendor-patternfly.sh`) |
| `tests/` | pytest; `fakeregistry.py` (in-memory OCI registry via httpx.MockTransport), offline update-service stub in `test_planner.py` |

## Commands

```bash
make venv test        # 34 tests, no network
make serve            # UI from the checkout (MIRROR_DIR=..., PULL_SECRET=...)
make image            # podman build --pull=always (all stages fully updated)
make scan             # Trivy (as a container) against the built image
make start|stop|logs  # container UI on 127.0.0.1:8088
make imageset         # regenerate imageset-config.yaml from plan.yaml in the container
```

A pull secret for testing: `/opt/ocp4/ai-slop/factory-helper/dl/*pull-secret*.json` (never
commit it; never print it).

## Decisions and constraints

- **No Node.js.** Server-rendered Jinja + PatternFly 6 CSS; small inline, dependency-free JS.
  Look follows the OpenShift console: dark masthead with Red Hat red (`--pf-t--color--red--50`)
  accent stripe and "MP" mark (not the Red Hat logo), blue primary buttons, toasts for feedback.
- **No page reloads.** All POST forms are sent with fetch (header `X-FH-Fetch: 1`); the server
  answers JSON (`msg`/`err`/`warn`/`job`/`location`, see `FETCH` contextvar and `back()`); the page
  re-renders only `[data-region]` elements listed in the form's `data-regions`. Without JS the
  forms still post/redirect normally. Live search uses `GET /search`.
- **Card order**: left column OpenShift release, Operator catalogs, Suggested sets, Find
  operators; right column Selected operators, Additional images, imageset-config.yaml, Import.
- **Catalogs**: three built-in indexes (redhat, certified, community; no marketplace), plus
  added catalogs shown by image name (`catalog_name`), optional TLS-verify skip which writes
  `--src-tls-verify=false` to `mirror/oc-mirror.flags` (a hint for whoever mirrors).
- **Scans** run as background jobs (one at a time) with a locking progress dialog.
- **additionalImages** always include `base_images` and every mirrored operator's must-gather.
- **Import** recognises dependencies and automatic images for scanned catalogs and keeps them
  automatic; unsupported ISC content is reported as warnings.
- **Container**: runtime is `ubi9/ubi-micro` + python3.12 + CA certs installed via `--installroot`;
  app in a venv built in a separate stage; pip/setuptools removed; no package manager, no
  OpenShift binaries (Quay's fixable CVEs came from oc/oc-mirror Go deps). Volumes:
  `/data/mirror`, `/run/secrets/pull-secret.json`. Run with
  `--userns=keep-id --user "$(id -u):$(id -g)"` so it can write to host folders.
- `REGISTRY_*` env vars break oc-mirror's embedded registry, which is one reason the pull
  secret env here is `PULL_SECRET_FILE` (REGISTRY_AUTH_FILE is only a fallback).

## Verifying UI changes

There is no browser on the host; use headless Chromium in a container:
`podman run --rm --network host docker.io/zenika/alpine-chrome --no-sandbox --screenshot ...`
for screenshots, or `--remote-debugging-port` plus a small Python `websocket-client` script
(`suppress_origin=True`) to drive clicks. Restore any plan you change while testing.

## User preferences

- Keep things simple; podman and Quadlets on the infra side; avoid extra frameworks.
- The user's shell is zsh: word splitting and globbing differ from bash, so wrap multi-word
  variable loops in `bash -c`.
