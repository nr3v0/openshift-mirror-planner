# OpenShift mirror planner

Plan the content for a disconnected OpenShift install in a browser, then mirror it with
oc-mirror v2. Pick a release from the real update channels, search the Red Hat, certified and
community operator catalogs (or your own), add suggested operator sets, and get an
`imageset-config.yaml` with every dependency and must-gather image already resolved.

It runs as one container: the web UI plus `oc` and `oc-mirror`, with your plan, the catalog
scans, the bundle and oc-mirror's cache kept in folders you mount. Built with Python and
server-rendered HTML styled with [PatternFly](https://www.patternfly.org/), the OpenShift
console's design system. No Node.js.

## Quick start

```bash
podman run -d --name mirror-planner --userns=keep-id --user "$(id -u):$(id -g)" -p 127.0.0.1:8088:8088 \
  -v ./mirror:/data/mirror:z \
  -v ./oc-mirror-cache:/data/cache:z \
  -v ./pull-secret.json:/run/secrets/pull-secret.json:ro,z \
  quay.io/nr3v0/openshift-mirror-planner:v1.0.1
```

Open http://127.0.0.1:8088/, build the plan, then mirror it to disk with the same folders:

```bash
podman run --rm --userns=keep-id --user "$(id -u):$(id -g)" \
  -v ./mirror:/data/mirror:z -v ./oc-mirror-cache:/data/cache:z \
  -v ./pull-secret.json:/run/secrets/pull-secret.json:ro,z \
  quay.io/nr3v0/openshift-mirror-planner:v1.0.1 mirror          # add --dry-run to only resolve
```

`mirror/` then holds `mirror_*.tar`, `imageset-config.yaml` and `working-dir/`. Carry them
into the disconnected site and push them to your registry with
`oc-mirror --v2 -c imageset-config.yaml --from file://<dir> docker://<registry>`.

Get the pull secret from https://console.redhat.com/openshift/install/pull-secret. It needs
credentials for `registry.redhat.io` and for any catalog you add.

### Volumes and settings

| Path in the container | Holds | Size |
| --- | --- | --- |
| `/data/mirror` | `plan.yaml` (your choices), `imageset-config.yaml`, `.catalogs/` scan summaries, the oc-mirror bundle | the bundle: tens to hundreds of GB |
| `/data/cache` | oc-mirror's image cache (also the container's `$HOME`) | about the bundle's size |
| `/run/secrets/pull-secret.json` | pull secret, read-only | |

| Variable | Default | Purpose |
| --- | --- | --- |
| `OCP_CHANNEL`, `OCP_VERSION` | `stable-4.22`, newest | release for a new plan |
| `PORT`, `LISTEN` | `8088`, `0.0.0.0` | UI listener inside the container |

`--userns=keep-id --user "$(id -u):$(id -g)"` runs the container as you, so it can write to
your folders and the files it creates belong to you. The UI has no login, so
publish it on `127.0.0.1` and reach it from elsewhere through an SSH tunnel:
`ssh -L 8088:127.0.0.1:8088 <host>`.

### Image contents and updates

The runtime is `ubi9/ubi-micro` with only `python3.12` and CA certificates added, plus the
planner's Python environment and the `oc` and `oc-mirror` binaries. It has no package manager,
compilers or other build tooling. Every build pulls the newest base images (`--pull=always`)
and applies all available RHEL updates in each stage, so rebuilding picks up new fixes. `make
scan` lists the remaining vulnerabilities with Trivy. Those in `oc` and `oc-mirror` come from
the Go libraries Red Hat builds them with, and are fixed by their next release; the build
always fetches the newest.

### With make

```bash
make image                    # build locally; OCP_CHANNEL=stable-4.21 picks another oc version
make scan                     # vulnerability report for the image
make start                    # UI in the background; make stop / make logs
make mirror                   # or: make dry-run
make help                     # MIRROR_DIR, CACHE_DIR, PULL_SECRET and PORT pick the folders
```

## Using the planner

The page runs top to bottom on the left, with the outcome on the right:

1. **OpenShift release.** The channel and min/max version lists come from the OpenShift update
   service (the source `oc-mirror list releases` uses), so they show only channels and
   releases that exist. Equal min and max mirror exactly one release. The update graph image
   is needed for an in-cluster OpenShift Update Service.
2. **Operator catalogs.** Scan the Red Hat, certified and community indexes for the plan's
   OpenShift minor. A scan extracts the catalog's file-based configs with `oc image extract`
   (about 15 s) and keeps a small summary. **Add catalog** takes any other index image, with
   an option to skip TLS verification for lab registries (`make mirror` then passes
   `--src-tls-verify=false`).
3. **Suggested sets** add groups of operators in one click. They live in
   `mirror_planner/presets.yaml`; edit or add your own. Operators a catalog doesn't contain
   are reported.
4. **Find operators** searches one catalog or all of them as you type. The disconnected-capable
   filter (on by default) uses each operator's `features.operators.openshift.io/disconnected`
   annotation. Add an operator with a channel; the newest version on that channel is
   mirrored.
5. **Selected operators** lists your choices. **Dependencies** are added automatically: those
   a catalog declares (`olm.package.required`, `olm.gvk.required`) and those an operator
   creates itself at install time, listed under `companions` in `presets.yaml`
   (`odf-operator` -> `odf-dependencies` and its components; ACM -> MCE). oc-mirror doesn't
   resolve dependencies itself.
6. **Additional images** always include `ubi9/ubi` and `rhel9/support-tools` (`base_images` in
   `presets.yaml`) and the must-gather image of every mirrored operator, plus your own.
7. **imageset-config.yaml** shows the generated oc-mirror v2 file.
8. **Import ImageSetConfiguration** replaces the plan with an existing oc-mirror v2 or v1
   file. Operators that are only dependencies, and the images the planner adds itself, are
   recognised for scanned catalogs. Anything the planner can't represent (a second platform
   channel, operator version ranges, `helm`, `blockedImages`) is listed in a warning.

Every action applies without reloading the page and reports in a toast. Every change saves
`plan.yaml` and regenerates `imageset-config.yaml`, so `mirror-planner imageset` and
`mirror-planner mirror` work from the command line too.

## Development

```bash
make venv test                # Python 3.11+; tests use a synthetic catalog and need no network
make clients                  # oc and oc-mirror into dl/, checksum-verified
make serve                    # UI from the checkout (needs oc on PATH for scans)
make vendor-patternfly        # refresh the vendored PatternFly CSS and fonts
```

Layout: `mirror_planner/catalog.py` reads file-based catalogs, `releases.py` queries the update
service, `imageset.py` holds the plan and renders and imports ImageSetConfigurations,
`app.py` is the web app, `cli.py` the `mirror-planner` command.

## License

Apache License 2.0. PatternFly, vendored in `mirror_planner/static/patternfly/`, is MIT licensed.
