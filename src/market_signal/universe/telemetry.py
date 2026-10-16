"""Public DU request metrics: aggregate counts/header latency, never response bodies."""

import json
import time
from collections import Counter


class Requests:
    def __init__(self):
        self.started = 0
        self.types = Counter()
        self.statuses = Counter()
        self.latencies = []
        self.response_bytes = 0

    def request(self, request):
        self.started += 1
        self.types[json.loads(request.content).get("type", "unknown")] += 1
        request.extensions["prism_started"] = time.monotonic()

    def response(self, response):
        self.statuses[str(response.status_code)] += 1
        self.latencies.append(time.monotonic() - response.request.extensions["prism_started"])
        self.response_bytes += int(response.headers.get("content-length", 0))

    def summary(self):
        values = sorted(self.latencies)
        return {
            "requests": self.started,
            "request_types": dict(self.types),
            "statuses": dict(self.statuses),
            "transport_failures": self.started - len(values),
            "header_latency_seconds_p50": values[len(values) // 2] if values else None,
            "header_latency_seconds_p95": values[min(len(values) - 1, int(len(values) * 0.95))]
            if values
            else None,
            "header_latency_seconds_max": max(values) if values else None,
            "declared_response_bytes": self.response_bytes,
        }
