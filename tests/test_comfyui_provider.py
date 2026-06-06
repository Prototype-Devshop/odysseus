"""Tests for the ComfyUI provider: connection probe (S3) and text-to-image
generation queue/poll/retrieve (S4B).

HTTP is mocked by monkeypatching ``httpx.get`` / ``httpx.post`` at the provider
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


# S4B guardrail: generation exists, but no video surface --------------------

def test_provider_exposes_generation_but_not_video():
    assert hasattr(ComfyUIProvider, "generate")
    assert hasattr(ComfyUIProvider, "probe")
    for forbidden in ("generate_video", "video"):
        assert not hasattr(ComfyUIProvider, forbidden), (
            f"S4B is image-only; {forbidden!r} must not exist yet"
        )


# S4B generation tests ------------------------------------------------------

class _FakeBytesResp:
    def __init__(self, status_code=200, content=b"", headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {"content-type": "image/png"}


def _history_with_image(prompt_id, filename="out.png", subfolder="", type_="output"):
    return {
        prompt_id: {
            "outputs": {
                "9": {"images": [{"filename": filename, "subfolder": subfolder, "type": type_}]}
            },
            "status": {"completed": True},
        }
    }


def _install_generation_router(monkeypatch, *, post, get):
    """Install fake httpx.post/get + a no-op sleep; record calls."""
    posts = []
    gets = []

    def fake_post(url, json=None, timeout=None):
        posts.append({"url": url, "json": json})
        return post(url, json)

    def fake_get(url, params=None, timeout=None):
        gets.append({"url": url, "params": params})
        return get(url, params)

    monkeypatch.setattr(comfyui.httpx, "post", fake_post)
    monkeypatch.setattr(comfyui.httpx, "get", fake_get)
    monkeypatch.setattr(comfyui.time, "sleep", lambda *_a, **_k: None)
    return posts, gets


def test_generate_happy_path_queue_poll_view(monkeypatch):
    prompt_id = "pid-123"
    png = b"\x89PNG\r\n\x1a\nFAKE"

    def post(url, body):
        assert url.endswith("/prompt")
        return _FakeResp(200, {"prompt_id": prompt_id})

    def get(url, params):
        if "/history/" in url:
            assert url.endswith(f"/history/{prompt_id}")
            return _FakeResp(200, _history_with_image(prompt_id))
        assert url.endswith("/view")
        assert params == {"filename": "out.png", "subfolder": "", "type": "output"}
        return _FakeBytesResp(200, png)

    posts, gets = _install_generation_router(monkeypatch, post=post, get=get)
    result = ComfyUIProvider(ENDPOINT).generate(prompt="a cat", width=512, height=512, seed=7)

    assert result["ok"] is True
    assert result["status"] == "generated"
    assert result["provider"] == "comfyui"
    assert result["image_bytes"] == png
    assert result["content_type"] == "image/png"
    assert result["prompt_id"] == prompt_id
    # POST /prompt, then GET /history, then GET /view were each exercised.
    assert len(posts) == 1
    assert any("/history/" in g["url"] for g in gets)
    assert any(g["url"].endswith("/view") for g in gets)


def test_generate_polls_until_output_ready(monkeypatch):
    prompt_id = "pid-poll"
    png = b"IMG"
    state = {"polls": 0}

    def post(url, body):
        return _FakeResp(200, {"prompt_id": prompt_id})

    def get(url, params):
        if "/history/" in url:
            state["polls"] += 1
            if state["polls"] < 3:
                return _FakeResp(200, {})  # not ready yet
            return _FakeResp(200, _history_with_image(prompt_id))
        return _FakeBytesResp(200, png)

    _install_generation_router(monkeypatch, post=post, get=get)
    result = ComfyUIProvider(ENDPOINT).generate(prompt="x", seed=1, poll_interval=0)

    assert result["ok"] is True
    assert state["polls"] >= 3


def test_generate_substitutes_only_known_fields(monkeypatch):
    """Workflow substitution touches placeholder inputs only — nothing else."""
    workflow = {
        "5": {"class_type": "EmptyLatentImage",
              "inputs": {"width": comfyui.PH_WIDTH, "height": comfyui.PH_HEIGHT, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": comfyui.PH_PROMPT}},
        "3": {"class_type": "KSampler",
              "inputs": {"seed": comfyui.PH_SEED, "steps": 20, "sampler_name": "euler"}},
        "meta": {"class_type": "Note", "inputs": {"text": "ignore me; do not change"}},
    }
    captured = {}

    def post(url, body):
        captured["wf"] = body["prompt"]
        return _FakeResp(200, {"prompt_id": "p"})

    def get(url, params):
        if "/history/" in url:
            return _FakeResp(200, _history_with_image("p"))
        return _FakeBytesResp(200, b"IMG")

    _install_generation_router(monkeypatch, post=post, get=get)
    ComfyUIProvider(ENDPOINT).generate(
        prompt="a fox", width=768, height=1024, seed=42, workflow=workflow,
    )

    wf = captured["wf"]
    assert wf["6"]["inputs"]["text"] == "a fox"
    assert wf["5"]["inputs"]["width"] == 768
    assert wf["5"]["inputs"]["height"] == 1024
    assert wf["3"]["inputs"]["seed"] == 42
    # Untouched fields stay exactly as authored.
    assert wf["3"]["inputs"]["steps"] == 20
    assert wf["3"]["inputs"]["sampler_name"] == "euler"
    assert wf["5"]["inputs"]["batch_size"] == 1
    assert wf["meta"]["inputs"]["text"] == "ignore me; do not change"
    # Original template object is not mutated in place.
    assert workflow["6"]["inputs"]["text"] == comfyui.PH_PROMPT


def test_generate_unreachable_endpoint(monkeypatch):
    def post(url, body):
        raise httpx.ConnectError("connection refused")

    def get(url, params):
        raise AssertionError("should not reach GET")

    _install_generation_router(monkeypatch, post=post, get=get)
    result = ComfyUIProvider(ENDPOINT).generate(prompt="x", seed=1)

    assert result["ok"] is False
    assert result["status"] == "unreachable"


def test_generate_queue_http_error_is_preserved_without_leaks(monkeypatch):
    def post(url, body):
        return _FakeResp(500, {"error": "boom"})

    def get(url, params):
        raise AssertionError("should not poll on queue failure")

    _install_generation_router(monkeypatch, post=post, get=get)
    result = ComfyUIProvider(ENDPOINT).generate(prompt="x", seed=1)

    assert result["ok"] is False
    assert result["status"] == "generation_failed"
    assert "HTTP 500" in (result.get("detail") or "")
    # No local filesystem paths leak in the rendered message.
    text = media_registry.format_degraded_message(result)
    assert "/Users/" not in text
    assert ".json" not in text


def test_generate_times_out_when_history_never_ready(monkeypatch):
    def post(url, body):
        return _FakeResp(200, {"prompt_id": "pid"})

    def get(url, params):
        if "/history/" in url:
            return _FakeResp(200, {})  # never ready
        raise AssertionError("should not fetch /view on timeout")

    _install_generation_router(monkeypatch, post=post, get=get)
    # timeout=0 makes the polling budget elapse immediately.
    result = ComfyUIProvider(ENDPOINT).generate(prompt="x", seed=1, timeout=0, poll_interval=0)

    assert result["ok"] is False
    assert result["status"] == "timeout"


def test_generate_no_image_in_output(monkeypatch):
    def post(url, body):
        return _FakeResp(200, {"prompt_id": "pid"})

    def get(url, params):
        if "/history/" in url:
            return _FakeResp(200, {"pid": {"outputs": {"9": {"images": []}}}})
        raise AssertionError("should not fetch /view without an image")

    _install_generation_router(monkeypatch, post=post, get=get)
    result = ComfyUIProvider(ENDPOINT).generate(prompt="x", seed=1, poll_interval=0)

    assert result["ok"] is False
    assert result["status"] == "generation_failed"


def test_generate_not_configured_without_endpoint(monkeypatch):
    result = ComfyUIProvider("").generate(prompt="x", seed=1)
    assert result["ok"] is False
    assert result["status"] == "not_configured"


def test_generate_workflow_missing(monkeypatch):
    # An empty/invalid explicit workflow yields the workflow_missing degraded state.
    result = ComfyUIProvider(ENDPOINT).generate(prompt="x", seed=1, workflow={})
    assert result["ok"] is False
    assert result["status"] == "workflow_missing"


def test_bundled_workflow_loads_and_has_placeholders():
    wf = comfyui._load_default_workflow()
    assert isinstance(wf, dict) and wf
    flat = str(wf)
    for token in (comfyui.PH_PROMPT, comfyui.PH_SEED, comfyui.PH_WIDTH, comfyui.PH_HEIGHT):
        assert token in flat
