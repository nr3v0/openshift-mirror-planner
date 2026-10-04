# OpenShift mirror planner: web UI + oc + oc-mirror in one image.
#
#   podman build -t openshift-mirror-planner .
#   podman build --build-arg OCP_CHANNEL=stable-4.21 -t openshift-mirror-planner:4.21 .
#
# Volumes (see README):
#   /data/mirror                    plan.yaml, imageset-config.yaml, catalog scans, the oc-mirror bundle
#   /data/cache                     oc-mirror image cache (large)
#   /run/secrets/pull-secret.json   pull secret, read-only

ARG OCP_CHANNEL=stable-4.22
ARG OCP_VERSION=

# ---------------------------------------------------------------- clients, checksum-verified
FROM registry.access.redhat.com/ubi9/ubi-minimal:latest AS clients
ARG OCP_CHANNEL
ARG OCP_VERSION
RUN microdnf install -y tar gzip findutils && microdnf clean all
COPY scripts/fetch-clients.sh /src/scripts/fetch-clients.sh
RUN DL=/dl OCP_CHANNEL=${OCP_CHANNEL} OCP_VERSION=${OCP_VERSION} /src/scripts/fetch-clients.sh \
 && . /dl/versions.env \
 && mkdir /out \
 && tar --no-same-owner -xzf /dl/${OPENSHIFT_CLIENT_TGZ} -C /out oc \
 && tar --no-same-owner -xzf /dl/${OC_MIRROR_TGZ} -C /out oc-mirror \
 && chmod 0755 /out/* \
 && cp /dl/versions.env /out/

# ---------------------------------------------------------------- application
FROM registry.access.redhat.com/ubi9/python-312:latest
ARG OCP_CHANNEL

LABEL org.opencontainers.image.title="OpenShift mirror planner" \
      org.opencontainers.image.description="Plan disconnected OpenShift content and run oc-mirror v2" \
      org.opencontainers.image.source="https://github.com/nr3v0/openshift-mirror-planner" \
      org.opencontainers.image.licenses="Apache-2.0"

USER 0
COPY --from=clients /out/oc /out/oc-mirror /usr/local/bin/
COPY --from=clients /out/versions.env /usr/local/share/mirror-planner/clients.env
COPY pyproject.toml README.md LICENSE /src/
COPY mirror_planner /src/mirror_planner
RUN pip install --no-cache-dir /src && rm -rf /src \
 && mkdir -p /data/mirror /data/cache && chmod 0777 /data/mirror /data/cache

# oc-mirror keeps state under $HOME; point it at the cache volume. Any UID works (--userns=keep-id).
ENV MIRROR_DIR=/data/mirror \
    OC_MIRROR_CACHE=/data/cache \
    HOME=/data/cache \
    PULL_SECRET_FILE=/run/secrets/pull-secret.json \
    OCP_CHANNEL=${OCP_CHANNEL} \
    LISTEN=0.0.0.0 \
    PORT=8088

USER 1001
VOLUME ["/data/mirror", "/data/cache"]
EXPOSE 8088
ENTRYPOINT ["mirror-planner"]
CMD ["serve"]
