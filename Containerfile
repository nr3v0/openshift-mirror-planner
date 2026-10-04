# OpenShift mirror planner: build oc-mirror ImageSetConfigurations in a browser.
#
#   podman build --pull=always -t openshift-mirror-planner .
#
# Every stage applies all available package updates. The runtime is ubi-micro with only the
# Python runtime and CA certificates installed into it: no package manager, compilers, headers,
# build tooling or OpenShift binaries ship in the image. Catalog scans read registries directly.
#
# Volumes (see README):
#   /data/mirror                    plan.yaml, imageset-config.yaml, catalog scan summaries
#   /run/secrets/pull-secret.json   pull secret for catalog scans, read-only

ARG OCP_CHANNEL=stable-4.22
ARG UBI_PYTHON=registry.access.redhat.com/ubi9/python-312:latest
ARG UBI=registry.access.redhat.com/ubi9/ubi:latest
ARG UBI_MICRO=registry.access.redhat.com/ubi9/ubi-micro:latest

# ---------------------------------------------------------------- application, built into a venv
FROM ${UBI_PYTHON} AS build
USER 0
RUN dnf -y update && dnf clean all
COPY pyproject.toml README.md LICENSE /src/
COPY mirror_planner /src/mirror_planner
# The venv uses /usr/bin/python3.12, which the runtime installs from the same RPM.
RUN /usr/bin/python3.12 -m venv /opt/mirror-planner \
 && /opt/mirror-planner/bin/pip install --no-cache-dir --upgrade pip setuptools wheel \
 && /opt/mirror-planner/bin/pip install --no-cache-dir --upgrade --upgrade-strategy eager /src \
 && /opt/mirror-planner/bin/pip uninstall -y pip setuptools wheel \
 && find /opt/mirror-planner -name '__pycache__' -prune -exec rm -rf {} +

# ---------------------------------------------------------------- runtime root filesystem
# ubi-micro plus python3.12 and CA certificates, installed and updated with the builder's dnf.
FROM ${UBI_MICRO} AS micro
FROM ${UBI} AS rootfs
COPY --from=micro / /rootfs/
RUN dnf -y update \
 && dnf -y --installroot=/rootfs --releasever=9 --setopt=install_weak_deps=0 --nodocs update \
 && dnf -y --installroot=/rootfs --releasever=9 --setopt=install_weak_deps=0 --nodocs \
        install python3.12 ca-certificates \
 && dnf -y --installroot=/rootfs clean all \
 && rm -rf /rootfs/var/cache/dnf /rootfs/var/log/dnf* /rootfs/var/lib/dnf/history* \
 && mkdir -p /rootfs/data/mirror && chmod 0777 /rootfs/data/mirror

# ---------------------------------------------------------------- runtime
FROM ${UBI_MICRO}
ARG OCP_CHANNEL

LABEL org.opencontainers.image.title="OpenShift mirror planner" \
      org.opencontainers.image.description="Build oc-mirror ImageSetConfigurations for disconnected OpenShift" \
      org.opencontainers.image.source="https://github.com/nr3v0/openshift-mirror-planner" \
      org.opencontainers.image.licenses="Apache-2.0"

COPY --from=rootfs /rootfs/ /
COPY --from=build /opt/mirror-planner /opt/mirror-planner

ENV PATH=/opt/mirror-planner/bin:${PATH} \
    MIRROR_DIR=/data/mirror \
    HOME=/tmp \
    PULL_SECRET_FILE=/run/secrets/pull-secret.json \
    OCP_CHANNEL=${OCP_CHANNEL} \
    LISTEN=0.0.0.0 \
    PORT=8088 \
    PYTHONDONTWRITEBYTECODE=1

USER 1001
VOLUME ["/data/mirror"]
EXPOSE 8088
ENTRYPOINT ["mirror-planner"]
CMD ["serve"]
