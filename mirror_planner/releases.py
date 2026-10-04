"""OpenShift release channels and versions from the update service (Cincinnati) graph, the source
`oc-mirror list releases` uses. Lets the planner offer real channels and min/max versions."""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

GRAPH_URL = "https://api.openshift.com/api/upgrades_info/v1/graph"
CHANNEL_KINDS = ("stable", "fast", "candidate", "eus")
_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)(.*)$")


def version_key(v: str) -> tuple:
    m = _VERSION.match(v)
    if not m:
        return (0, 0, 0, 0, v)
    major, minor, patch, rest = m.groups()
    return (int(major), int(minor), int(patch), 0 if rest else 1, rest)  # 4.22.0-rc.1 < 4.22.0


def minor_of(version_or_channel: str) -> tuple[int, int] | None:
    m = re.search(r"(\d+)\.(\d+)", version_or_channel)
    return (int(m.group(1)), int(m.group(2))) if m else None


class ReleaseGraph:
    """Cached graph lookups. Failures (no internet) return empty results; the page then falls
    back to free-text fields."""

    def __init__(self, arch: str = "amd64", ttl: float = 600, client: httpx.Client | None = None):
        self.arch = arch
        self.ttl = ttl
        self.client = client or httpx.Client(timeout=15, headers={"Accept": "application/json"})
        self._cache: dict[str, tuple[float, list[str]]] = {}
        self._lock = threading.Lock()
        self.last_error: str | None = None

    def versions(self, channel: str) -> list[str]:
        """Every release in the channel, newest first (includes older minors it upgrades from)."""
        with self._lock:
            hit = self._cache.get(channel)
            if hit and time.time() - hit[0] < self.ttl:
                return hit[1]
        try:
            r = self.client.get(GRAPH_URL, params={"channel": channel, "arch": self.arch})
            r.raise_for_status()
            found = sorted({n["version"] for n in r.json().get("nodes", [])}, key=version_key, reverse=True)
            self.last_error = None
        except (httpx.HTTPError, ValueError, KeyError) as e:
            self.last_error = f"update service unreachable: {e}"
            return []
        with self._lock:
            self._cache[channel] = (time.time(), found)
        return found

    def channels(self, around: str, below: int = 2, above: int = 1) -> list[str]:
        """Channels that exist for minors near `around` (e.g. 4.20-4.23 for 4.22), newest minor first."""
        mm = minor_of(around)
        if mm is None:
            return []
        major, minor = mm
        names = [f"{kind}-{major}.{m}" for m in range(minor + above, max(minor - below, 0) - 1, -1)
                 for kind in CHANNEL_KINDS]
        with ThreadPoolExecutor(max_workers=8) as pool:
            counts = dict(zip(names, pool.map(lambda c: len(self.versions(c)), names)))
        return [c for c in names if counts[c]]
