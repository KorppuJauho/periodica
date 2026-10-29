"""FastAPI's built-in OpenTelemetry support is switched off."""

from __future__ import annotations

from fastapi.telemetry import _asgi


def test_trace_headers_are_never_parsed(client, monkeypatch):
    calls = []
    monkeypatch.setattr(_asgi.propagate, "extract", lambda *a, **kw: calls.append(a) or {})
    headers = {
        "traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
        "tracestate": ",".join(f"k{i}=v{i}" for i in range(2000)),
        "baggage": "key=value",
    }
    r = client.get("/login", headers=headers)
    assert r.status_code == 200
    assert calls == [], "trace headers must not reach the OpenTelemetry propagator"


def test_tracing_metrics_and_logs_are_off(client):
    config = client.app._telemetry
    assert (config["tracing"], config["metrics"], config["logs"]) == (False, False, False)
