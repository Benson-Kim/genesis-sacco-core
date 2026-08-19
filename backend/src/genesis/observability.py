"""In-process metrics registry with Prometheus text rendering (issue #4).

Deliberately dependency-free (stdlib only, the genesis.logging posture):
a counter map plus fixed-bucket latency histograms guarded by one lock.
This is the CODE half of the observability issue — the scrape target
(`/ops/metrics`, genesis.api.ops) renders this registry together with
the DB-derived gauges (outbox depth, worker heartbeats, pool
saturation). The provider half (error tracker, external uptime,
paging) is #39 and cannot live here.

Process-local by design: counters reset on restart and are NOT shared
across workers/processes — exactly the Prometheus client-library
contract (rate() and increase() absorb resets). Values are aggregate
counts and durations only; no request payloads, no identifiers, no
PII ever enters this module (the no-PII-in-logs gate applies to
metrics labels too — the only label is the ROUTER name, a code-owned
path segment).
"""

from __future__ import annotations

import threading

#: Cumulative latency buckets (seconds) — the Prometheus default-ish
#: ladder, wide enough for a shared-host p95 without unbounded memory.
LATENCY_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)

#: Counter names are pinned here so raise-site increments and the
#: renderer can never drift apart on spelling.
RATE_LIMITED_TOTAL = "genesis_rate_limited_responses_total"
AUTH_FAILURES_TOTAL = "genesis_auth_failures_total"

_COUNTER_HELP: dict[str, str] = {
    RATE_LIMITED_TOTAL: "HTTP 429 responses counted at the error-handler seam.",
    AUTH_FAILURES_TOTAL: "HTTP 401 responses counted at the error-handler seam.",
}


def _escape_label(value: str) -> str:
    """Prometheus label-value escaping (text exposition format 0.0.4)."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_float(value: float) -> str:
    """Render a float the Prometheus way (no trailing junk, inf spelled +Inf)."""
    if value == float("inf"):
        return "+Inf"
    return repr(value)


class _Histogram:
    """Fixed-bucket latency histogram: cumulative counts + sum + count."""

    __slots__ = ("bucket_counts", "count", "total")

    def __init__(self) -> None:
        self.bucket_counts: list[int] = [0] * len(LATENCY_BUCKETS)
        self.count = 0
        self.total = 0.0

    def observe(self, seconds: float) -> None:
        for i, bound in enumerate(LATENCY_BUCKETS):
            if seconds <= bound:
                self.bucket_counts[i] += 1
        self.count += 1
        self.total += seconds

    def quantile(self, q: float) -> float:
        """Approximate quantile by linear interpolation inside the bucket.

        Good enough for an ops dashboard (the histogram buckets travel
        too, so Prometheus can compute its own): observations above the
        largest finite bucket clamp to that bound.
        """
        if self.count == 0:
            return 0.0
        rank = q * self.count
        cumulative = 0
        lower = 0.0
        for i, bound in enumerate(LATENCY_BUCKETS):
            in_bucket = self.bucket_counts[i] - cumulative
            if self.bucket_counts[i] >= rank and in_bucket > 0:
                fraction = (rank - cumulative) / in_bucket
                return lower + (bound - lower) * min(max(fraction, 0.0), 1.0)
            cumulative = self.bucket_counts[i]
            lower = bound
        return LATENCY_BUCKETS[-1]


class MetricsRegistry:
    """Thread-safe registry: named counters + per-router latency histograms."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {}
        self._histograms: dict[str, _Histogram] = {}

    def inc_counter(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + amount

    def counter_value(self, name: str) -> int:
        with self._lock:
            return self._counters.get(name, 0)

    def observe_request(self, router: str, seconds: float) -> None:
        with self._lock:
            histogram = self._histograms.get(router)
            if histogram is None:
                histogram = _Histogram()
                self._histograms[router] = histogram
            histogram.observe(seconds)

    def request_p95(self, router: str) -> float:
        with self._lock:
            histogram = self._histograms.get(router)
            return histogram.quantile(0.95) if histogram is not None else 0.0

    def reset(self) -> None:
        """Test seam only — production code never resets counters."""
        with self._lock:
            self._counters.clear()
            self._histograms.clear()

    def render_prometheus(self) -> str:
        """Render every counter and histogram in Prometheus text format."""
        with self._lock:
            lines: list[str] = []
            for name in sorted(set(self._counters) | set(_COUNTER_HELP)):
                lines.append(f"# HELP {name} {_COUNTER_HELP.get(name, name)}")
                lines.append(f"# TYPE {name} counter")
                lines.append(f"{name} {self._counters.get(name, 0)}")
            if self._histograms:
                base = "genesis_http_request_duration_seconds"
                lines.append(f"# HELP {base} HTTP request latency per router.")
                lines.append(f"# TYPE {base} histogram")
                p95_lines: list[str] = []
                for router in sorted(self._histograms):
                    histogram = self._histograms[router]
                    label = _escape_label(router)
                    for i, bound in enumerate(LATENCY_BUCKETS):
                        lines.append(
                            f'{base}_bucket{{router="{label}",le="{_format_float(bound)}"}} '
                            f"{histogram.bucket_counts[i]}"
                        )
                    lines.append(f'{base}_bucket{{router="{label}",le="+Inf"}} {histogram.count}')
                    lines.append(f'{base}_sum{{router="{label}"}} {_format_float(histogram.total)}')
                    lines.append(f'{base}_count{{router="{label}"}} {histogram.count}')
                    p95_lines.append(
                        f'genesis_http_request_p95_seconds{{router="{label}"}} '
                        f"{_format_float(histogram.quantile(0.95))}"
                    )
                lines.append(
                    "# HELP genesis_http_request_p95_seconds "
                    "Approximate p95 request latency per router (from the histogram)."
                )
                lines.append("# TYPE genesis_http_request_p95_seconds gauge")
                lines.extend(p95_lines)
            return "\n".join(lines) + "\n" if lines else ""


#: The process-wide registry: the middleware/error-handler seams write
#: to it; the /ops/metrics endpoint renders it.
metrics = MetricsRegistry()


def router_label(route_path: str | None) -> str:
    """Bounded-cardinality router label from a matched route TEMPLATE.

    The label is the first segment of the route's path template
    (``/members/{member_id}`` -> ``members``) — never the raw request
    URL, so path parameters (member numbers, UUIDs) cannot leak into
    label values and cardinality stays bounded by the route table.
    Unmatched requests (404 before routing) collapse into one label.
    """
    if not route_path:
        return "unrouted"
    first = route_path.strip("/").split("/", 1)[0]
    return first if first else "root"
