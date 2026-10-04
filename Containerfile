# OpenShift mirror planner: web UI + oc + oc-mirror in one image.
#
#   podman build --pull=always -t openshift-mirror-planner .
#   podman build --pull=always --build-arg OCP_CHANNEL=stable-4.21 -t openshift-mirror-planner:4.21 .
#
# Every stage applies all available package updates. The runtime is ubi-micro with only the
# Python runtime and CA certificates installed into it: no package manager, compilers, headers
# or other build tooling ship in the image.
#
# Volumes (see README):
#   /data/mirror                    plan.yaml, imageset-config.yaml, catalog scans, the oc-mirror bundle
#   /data/cache                     oc-mirror image cache (large)
#   /run/secrets/pull-secret.json   pull secret, read-only

ARG OCP_CHANNEL=stable-4.22
ARG OCP_VERSION=
ARG UBI_MINIMAL=registry.access.redhat.com/ubi9/ubi-minimal:latest
ARG UBI_PYTHON=registry.access.redhat.com/ubi9/python-312:latest
ARG UBI=registry.access.redhat.com/ubi9/ubi:latest
ARG UBI_MICRO=registry.access.redhat.com/ubi9/ubi-micro:latest

# ---------------------------------------------------------------- clients, checksum-verified
FROM ${UBI_MINIMAL} AS clients
ARG OCP_CHANNEL
ARG OCP_VERSION
RUN microdnf -y update && microdnf -y install tar gzip findutils && microdnf clean all
COPY scripts/fetch-clients.sh /src/scripts/fetch-clients.sh
RUN DL=/dl OCP_CHANNEL=${OCP_CHANNEL} OCP_VERSION=${OCP_VERSION} /src/scripts/fetch-clients.sh \
 && . /dl/versions.env \
 && mkdir /out \
 && tar --no-same-owner -xzf /dl/${OPENSHIFT_CLIENT_TGZ} -C /out oc \
 && tar --no-same-owner -xzf /dl/${OC_MIRROR_TGZ} -C /out oc-mirror \
 && chmod 0755 /out/* \
 && cp /dl/versions.env /out/

# ---------------------------------------------------------------- application, built into a venv
FROM ${UBI_PYTHON} AS build
USER 0
RUN dnf -y update && dnf clean all
COPY pyproject.toml README.md LICENSE /src/
COPY mirror_planner /src/mirror_planner
# The venv uses /usr/bin/python3.12, which the runtime stage installs from the same RPM.
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
 && mkdir -p /rootfs/data/mirror /rootfs/data/cache && chmod 0777 /rootfs/data/mirror /rootfs/data/cache

# ---------------------------------------------------------------- runtime
FROM ${UBI_MICRO}
ARG OCP_CHANNEL

LABEL org.opencontainers.image.title="OpenShift mirror planner" \
      org.opencontainers.image.description="Plan disconnected OpenShift content and run oc-mirror v2" \
      org.opencontainers.image.source="https://github.com/nr3v0/openshift-mirror-planner" \
      org.opencontainers.image.licenses="Apache-2.0"

COPY --from=rootfs /rootfs/ /
COPY --from=clients /out/oc /out/oc-mirror /usr/local/bin/
COPY --from=clients /out/versions.env /usr/local/share/mirror-planner/clients.env
COPY --from=build /opt/mirror-planner /opt/mirror-planner

# oc-mirror keeps state under $HOME; point it at the cache volume. Any UID works (--userns=keep-id).
ENV PATH=/opt/mirror-planner/bin:${PATH} \
    MIRROR_DIR=/data/mirror \
    OC_MIRROR_CACHE=/data/cache \
    HOME=/data/cache \
    PULL_SECRET_FILE=/run/secrets/pull-secret.json \
    OCP_CHANNEL=${OCP_CHANNEL} \
    LISTEN=0.0.0.0 \
    PORT=8088 \
    PYTHONDONTWRITEBYTECODE=1

USER 1001
VOLUME ["/data/mirror", "/data/cache"]
EXPOSE 8088
ENTRYPOINT ["mirror-planner"]
CMD ["serve"]
