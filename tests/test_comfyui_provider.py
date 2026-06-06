"""Tests for the ComfyUI provider connection probe (Slice 3).

Probe-only: no generation, polling, or output retrieval is exercised (none
exists yet). HTTP is mocked by monkeypatching ``httpx.get`` at the provider
module path, matching the repo convention (see tests/test_lmstudio_discovery.py).
"""

import httpx

from services.media import comfyui
from services.media.comfyui import ComfyUIProvider
from src import media_registry


ENDPOINT = "http://localhost:8188"


class _FakeResp:
    def __init__(self, status_code=200, json_data=None, raise_json=False):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self._raise = raise_json

    def json(self):
        if self._raise:
            raise ValueError("response was not valid JSON")
        return self._json


def _install_router(monkeypatch, router):
    """Install a fake httpx.get that dispatches on URL via ``router(url)``."""
    calls = []

    def fake_get(url, timeout=None):
        calls.append(url)
        return router(url)

    monkeypatch.setattr(comfyui.httpx, "get", fake_get)
    return calls


# 1. Reachable ComfyUI via /system_stats ------------------------------------

def test_probe_online_via_system_stats(monkeypatch):
    def router(url):
        assert url.endswith("/system_stats")
        return _FakeResp(200, {"system": {"comfyui_version": "0.3.0"}, "devices": []})

    calls = _install_router(monkeypatch, router)
    result = ComfyUIProvider(ENDPOINT).probe()

    assert result["ok"] is True
    assert result["available"] is True
    assert result["status"] == "online"
    assert result["provider"] == "comfyui"
    assert result["endpoint"] == ENDPOINT
    assert result["via"] == "/system_stats"
    assert "0.3.0" in (result["detail"] or "")
    assert len(calls) == 1  # no fallback needed


# 2. Fallback success via /object_info --------------------------------------

def test_probe_falls_back_to_object_info_on_http_error(monkeypatch):
    def router(url):
        if url.endswith("/system_stats"):
            return _FakeResp(500, {})  # http error → triggers fallback
        return _FakeResp(200, {"KSampler": {"input": {}}})

    calls = _install_router(monkeypatch, router)
    result = ComfyUIProvider(ENDPOINT).probe()

    assert result["ok"] is True
    assert result["status"] == "online"
    assert result["via"] == "/object_info"
    assert [c.rsplit("/", 1)[-1] for c in calls] == ["system_stats", "object_info"]


def test_probe_falls_back_when_system_stats_malformed(monkeypatch):
    def router(url):
        if url.endswith("/system_stats"):
            return _FakeResp(200, raise_json=True)  # malformed → fallback
        return _FakeResp(200, {"KSampler": {}})

    calls = _install_router(monkeypatch, router)
    result = ComfyUIProvider(ENDPOINT).probe()

    assert result["ok"] is True
    assert result["via"] == "/object_info"
    assert len(calls) == 2


# 3. Unreachable / offline endpoint -----------------------------------------

def test_probe_unreachable_no_fallback(monkeypatch):
    def router(url):
        raise httpx.ConnectError("connection refused")

    calls = _install_router(monkeypatch, router)
    result = ComfyUIProvider(ENDPOINT).probe()

    assert result["ok"] is False
    assert result["available"] is False
    assert result["status"] == "unreachable"
    assert ENDPOINT in result["message"]
    assert "connection refused" in (result["detail"] or "")
    # Network errors are terminal — must NOT retry the fallback path.
    assert len(calls) == 1


def test_probe_timeout_is_unreachable(monkeypatch):
    def router(url):
        raise httpx.ConnectTimeout("timed out")

    _install_router(monkeypatch, router)
    result = ComfyUIProvider(ENDPOINT).probe()
    assert result["status"] == "unreachable"


# 4. Malformed response (both paths) ----------------------------------------

def test_probe_malformed_both_paths_is_unavailable(monkeypatch):
    def router(url):
        return _FakeResp(200, raise_json=True)

    calls = _install_router(monkeypatch, router)
    result = ComfyUIProvider(ENDPOINT).probe()

    assert result["ok"] is False
    assert result["status"] == "unavailable"
    assert len(calls) == 2  # tried primary then fallback
    assert "/system_stats" in (result["detail"] or "")


def test_probe_non_dict_json_is_malformed(monkeypatch):
    def router(url):
        return _FakeResp(200, json_data=["not", "a", "dict"])

    _install_router(monkeypatch, router)
    result = ComfyUIProvider(ENDPOINT).probe()
    assert result["status"] == "unavailable"


# Additional coverage --------------------------------------------------------

def test_probe_not_configured_when_endpoint_empty(monkeypatch):
    # httpx.get must never be called when there is no endpoint.
    def boom(url, timeout=None):
        raise AssertionError("network call made without an endpoint")

    monkeypatch.setattr(comfyui.httpx, "get", boom)
    result = ComfyUIProvider("").probe()

    assert result["ok"] is False
    assert result["status"] == "not_configured"
    assert result["checked"][0]["provider"] == "comfyui"


def test_probe_auth_error_no_fallback(monkeypatch):
    def router(url):
        return _FakeResp(401, {})

    calls = _install_router(monkeypatch, router)
    result = ComfyUIProvider(ENDPOINT).probe()

    assert result["status"] == "auth_error"
    assert result["ok"] is False
    assert len(calls) == 1  # auth is terminal, no fallback


def test_from_settings_reads_endpoint(monkeypatch):
    provider = ComfyUIProvider.from_settings(
        settings={"comfyui_endpoint_url": "http://host:8188"}
    )
    assert provider.endpoint_url == "http://host:8188"


def test_module_probe_helper_prefers_explicit_url(monkeypatch):
    def router(url):
        return _FakeResp(200, {"system": {}})

    _install_router(monkeypatch, router)
    result = comfyui.probe("http://explicit:8188")
    assert result["endpoint"] == "http://explicit:8188"
    assert result["status"] == "online"


# Degraded-state shape compatibility ----------------------------------------

def test_probe_result_renders_with_media_registry_formatter(monkeypatch):
    def router(url):
        raise httpx.ConnectError("refused")

    _install_router(monkeypatch, router)
    result = ComfyUIProvider(ENDPOINT).probe()

    # Same keys as the shared degraded-state shape → reusable rendering.
    for key in ("ok", "available", "status", "message", "checked", "next_steps", "detail"):
        assert key in result
    text = media_registry.format_degraded_message(result)
    assert "Checked:" in text
    assert "- comfyui:" in text


# S3 guardrail: probe only, no generation surface ---------------------------

def test_provider_exposes_no_generation_methods():
    for forbidden in ("generate", "generate_image", "queue_prompt", "poll", "get_output"):
        assert not hasattr(ComfyUIProvider, forbidden), (
            f"S3 is probe-only; {forbidden!r} must not exist yet"
        )
