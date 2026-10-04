#!/usr/bin/bash
# Download and verify the clients the planner uses: oc (catalog scans) and oc-mirror (mirroring).
#
#   OCP_CHANNEL=stable-4.22   channel folder whose release.txt picks the oc version (default)
#   OCP_VERSION=4.22.15       pin an exact oc version instead
#   DL=dl                     download directory
#
# Existing files are verified against the published checksums, not re-downloaded. Writes
# $DL/versions.env. Offline, the newest openshift-client tarball already in $DL decides the version.
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
DL=${DL:-${root}/dl}
OCP_CHANNEL=${OCP_CHANNEL:-stable-4.22}
OCP_VERSION=${OCP_VERSION:-}
OCP_BASE=https://mirror.openshift.com/pub/openshift-v4/clients/ocp
CGW_BASE=https://mirror.openshift.com/pub/cgw

log() { echo "fetch-clients: $*"; }
mkdir -p "${DL}"

if [[ -z "${OCP_VERSION}" ]]; then
    if release=$(curl -fsSL --max-time 20 "${OCP_BASE}/${OCP_CHANNEL}/release.txt" 2>/dev/null); then
        OCP_VERSION=$(awk '/^Name:/ {print $2; exit}' <<< "${release}")
        log "${OCP_CHANNEL} is ${OCP_VERSION}"
    else
        OCP_VERSION=$(ls "${DL}"/openshift-client-linux-*.tar.gz 2>/dev/null \
            | sed -E 's/.*openshift-client-linux-(.*)\.tar\.gz/\1/' | sort -V | tail -1)
        [[ -n "${OCP_VERSION}" ]] || { echo "mirror unreachable and no openshift-client tarball in ${DL}" >&2; exit 1; }
        log "mirror unreachable; using ${OCP_VERSION} found in ${DL}"
    fi
fi

# verify <directory URL> <file name> <path>: check against sha256sum.txt in that directory.
verify() {
    local sums line
    sums=$(curl -fsSL --max-time 30 "$1/sha256sum.txt") || return 2   # mirror unreachable
    line=$(awk -v n="$2" -v f="$3" '$2 == n || $2 == "*" n {print $1 "  " f}' <<< "${sums}")
    [[ -n "${line}" ]] || { echo "$2 is not listed in $1/sha256sum.txt" >&2; return 1; }
    sha256sum -c --quiet <<< "${line}"
}

# fetch <directory URL> <file name> [moving]: verify an existing copy, or download and verify.
# "moving" marks a latest/ folder: an older existing copy no longer matches, which is expected.
fetch() {
    local dir=$1 name=$2 moving=${3:-} rc=0
    if [[ -s "${DL}/${name}" ]]; then
        verify "${dir}" "${name}" "${DL}/${name}" 2>/dev/null || rc=$?
        case ${rc} in
            0) log "have ${name} (checksum ok)" ;;
            2) log "have ${name} (mirror unreachable, not verified)" ;;
            *) if [[ -n "${moving}" ]]; then
                   log "have ${name} (older than ${dir}; delete it to fetch the newest)"
               else
                   echo "${DL}/${name} does not match the published checksum; delete it to re-download" >&2
                   exit 1
               fi ;;
        esac
        return
    fi
    log "downloading ${name}"
    curl -fL --retry 3 -sS -o "${DL}/${name}.part" "${dir}/${name}"
    verify "${dir}" "${name}" "${DL}/${name}.part" || { rm -f "${DL}/${name}.part"; exit 1; }
    mv "${DL}/${name}.part" "${DL}/${name}"
    log "downloaded ${name} (checksum ok)"
}

fetch "${OCP_BASE}/${OCP_VERSION}" "openshift-client-linux-${OCP_VERSION}.tar.gz"
fetch "${CGW_BASE}/oc-mirror/latest" oc-mirror-rhel9-linux-amd64.tar.gz moving

cat > "${DL}/versions.env" <<VERSIONS
# written by scripts/fetch-clients.sh $(date -u +%FT%TZ)
OCP_CHANNEL=${OCP_CHANNEL}
OCP_VERSION=${OCP_VERSION}
OPENSHIFT_CLIENT_TGZ=openshift-client-linux-${OCP_VERSION}.tar.gz
OC_MIRROR_TGZ=oc-mirror-rhel9-linux-amd64.tar.gz
VERSIONS
log "wrote ${DL}/versions.env (oc ${OCP_VERSION})"
