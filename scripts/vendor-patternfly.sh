#!/usr/bin/bash
# Vendor PatternFly (the OpenShift console's design system) into the package:
# patternfly.min.css plus the font files it references. Static files only, no Node.js; the
# pages then work without internet. Re-run to move to another version.
#   scripts/vendor-patternfly.sh [version]     default: newest 6.x
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
dest="${root}/mirror_planner/static/patternfly"
base=https://cdn.jsdelivr.net/npm/@patternfly/patternfly
version=${1:-$(curl -fsSL "https://data.jsdelivr.com/v1/packages/npm/@patternfly/patternfly/resolved?specifier=6" \
    | python3 -c 'import sys, json; print(json.load(sys.stdin)["version"])')}

rm -rf "${dest}" && mkdir -p "${dest}"
curl -fsSL "${base}@${version}/patternfly.min.css" -o "${dest}/patternfly.min.css"
# PatternFly is MIT licensed; keep the license next to the files if the package ships one
for f in LICENSE LICENSE.txt LICENSE.md; do
    curl -fsSL "${base}@${version}/${f}" -o "${dest}/LICENSE.txt" 2>/dev/null && break
done
[[ -s "${dest}/LICENSE.txt" ]] || { rm -f "${dest}/LICENSE.txt"; echo "MIT, https://github.com/patternfly/patternfly" > "${dest}/LICENSE.txt"; }

# every relative url(...) in the stylesheet: fonts and images
grep -oE 'url\([^)]+\)' "${dest}/patternfly.min.css" | sed -E 's/url\(["'\'']?//; s/["'\'']?\)$//' \
    | grep -vE '^(data:|https?:|#)' | sed -E 's/[?#].*//' | sort -u | while read -r rel; do
    mkdir -p "${dest}/$(dirname "${rel}")"
    curl -fsSL "${base}@${version}/${rel#./}" -o "${dest}/${rel#./}"
done
echo "${version}" > "${dest}/VERSION"
echo "PatternFly ${version}: $(find "${dest}" -type f | wc -l) files, $(du -sh "${dest}" | cut -f1)"
