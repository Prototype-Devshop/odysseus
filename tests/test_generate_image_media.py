"""S4B tests: do_generate_image routes through the media registry and ComfyUI.

These cover the resolution order (explicit registry modelId → default registry
image model → legacy OpenAI-compatible path → shared degraded-state) without
hitting the network. ComfyUI generation is mocked at the provider boundary; the
legacy path is exercised with a faked httpx.AsyncClient.
"""

import base64
import json

import httpx

import src.agent_tools  # noqa: F401  (import-order guard for facade modules)
from src import ai_interaction
from services.media import comfyui


def _comfy_settings(model_id="qwen-image", *, default=True, enabled=True):
    return {
        "comfyui_endpoint_url": "http://localhost:8188",
        "default_image_media_model": model_id if default else "",
        "media_models": [
            {
                "id": model_id,
                "label": "Qwen Image",
                "provider": "comfyui",
                "kind": "image",
                "enabled": enabled,
                "isDefault": default,
            }
        ],
    }


def _patch_settings(monkeypatch, settings):
    import src.settings as settings_mod
    monkeypatch.setattr(settings_mod, "load_settings", lambda: settings)


def _patch_comfy_generate(monkeypatch, capture):
    def fake_generate(self, *, prompt, width=1024, height=1024, progress_cb=None,
                      checkpoint=None, **kwargs):
        capture["prompt"] = prompt
        capture["width"] = width
        capture["height"] = height
        capture["endpoint"] = self.endpoint_url
        capture["checkpoint"] = checkpoint
        return {
            "ok": True,
            "status": "generated",
            "provider": "comfyui",
            "image_bytes": b"PNGDATA",
            "content_type": "image/png",
            "width": width,
            "height": height,
            "prompt_id": "pid-1",
        }

    monkeypatch.setattr(comfyui.ComfyUIProvider, "generate", fake_generate)


def _patch_persist(monkeypatch):
    monkeypatch.setattr(
        ai_interaction,
        "_persist_generated_image",
        lambda *a, **k: ("/api/generated-image/test.png", "gid-1"),
    )


# 1. Default configured comfyui image model when modelId omitted -------------

async def test_uses_default_comfyui_model_when_model_omitted(monkeypatch):
    _patch_settings(monkeypatch, _comfy_settings("qwen-image"))
    capture = {}
    _patch_comfy_generate(monkeypatch, capture)
    _patch_persist(monkeypatch)

    result = await ai_interaction.do_generate_image("a serene lake\n\n512x512", owner=None)

    assert result.get("image_url") == "/api/generated-image/test.png"
    assert result.get("image_id") == "gid-1"
    assert result.get("image_model") == "qwen-image"
    assert result.get("image_size") == "512x512"
    assert capture["prompt"] == "a serene lake"
    assert capture["endpoint"] == "http://localhost:8188"


# 1a. Configured checkpoint is forwarded to the ComfyUI provider -------------

async def test_configured_checkpoint_is_forwarded(monkeypatch):
    settings = _comfy_settings("qwen-image")
    settings["media_models"][0]["checkpoint"] = "sdxl.safetensors"
    _patch_settings(monkeypatch, settings)
    capture = {}
    _patch_comfy_generate(monkeypatch, capture)
    _patch_persist(monkeypatch)

    await ai_interaction.do_generate_image("a serene lake", owner=None)

    assert capture["checkpoint"] == "sdxl.safetensors"


async def test_checkpoint_required_is_surfaced_safely(monkeypatch):
    # No checkpoint configured + bundled workflow (has %checkpoint%) → the
    # real provider must fail before any network call, with a safe message.
    settings = _comfy_settings("qwen-image")  # no checkpoint
    _patch_settings(monkeypatch, settings)
    monkeypatch.setattr(comfyui.httpx, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not POST")))

    result = await ai_interaction.do_generate_image("a cat", owner=None)

    assert "error" in result
    assert "checkpoint" in result["error"].lower()
    for leak in ("/Users/", "://", "8188"):
        assert leak not in result["error"]


# 1b. Owner/session are persisted on the ComfyUI path (F3) ------------------

async def test_owner_session_persisted_on_comfyui_path(monkeypatch):
    _patch_settings(monkeypatch, _comfy_settings("qwen-image"))
    _patch_comfy_generate(monkeypatch, {})

    captured = {}

    def fake_persist(image_bytes, *, prompt, model, size, quality, session_id=None, owner=None):
        captured["session_id"] = session_id
        captured["owner"] = owner
        return ("/api/generated-image/x.png", "gid-1")

    monkeypatch.setattr(ai_interaction, "_persist_generated_image", fake_persist)

    await ai_interaction.do_generate_image(
        "a serene lake", session_id="sess-9", owner="alice",
    )

    assert captured["owner"] == "alice"
    assert captured["session_id"] == "sess-9"


# 2. Explicit modelId is honored --------------------------------------------

async def test_uses_explicit_registry_model_id(monkeypatch):
    # Default points elsewhere; explicit id must win.
    settings = _comfy_settings("qwen-image", default=False)
    settings["media_models"].append({
        "id": "flux-comfy", "label": "Flux", "provider": "comfyui",
        "kind": "image", "enabled": True, "isDefault": False,
    })
    _patch_settings(monkeypatch, settings)
    capture = {}
    _patch_comfy_generate(monkeypatch, capture)
    _patch_persist(monkeypatch)

    result = await ai_interaction.do_generate_image("a fox\nflux-comfy\n768x768", owner=None)

    assert result.get("image_model") == "flux-comfy"
    assert capture["width"] == 768 and capture["height"] == 768


# 3. Degraded-state when nothing is configured ------------------------------

async def test_degraded_state_when_no_model_configured(monkeypatch):
    _patch_settings(monkeypatch, {"media_models": [], "image_model": ""})

    # Make sure the legacy auto-detect finds nothing either.
    def _raise(*a, **k):
        raise ValueError("no endpoint")
    monkeypatch.setattr(ai_interaction, "_resolve_model", _raise)

    result = await ai_interaction.do_generate_image("a dragon", owner=None)

    assert result.get("image_url") is None
    assert result.get("available") is False
    assert result.get("status") in ("no_models", "no_default")
    # The shared degraded message guides the user without leaking paths.
    assert "image model" in result.get("results", "").lower()


# 4. Provider error is preserved without leaking paths ----------------------

async def test_comfyui_provider_error_is_surfaced(monkeypatch):
    # Use a non-loopback-but-private endpoint so the request is attempted and
    # then fails at the network layer (real _unreachable path, not a fake).
    settings = _comfy_settings("qwen-image")
    settings["comfyui_endpoint_url"] = "http://10.1.2.3:8188"
    _patch_settings(monkeypatch, settings)

    def boom(*a, **k):
        raise httpx.ConnectError("connect to 10.1.2.3:8188 failed")

    monkeypatch.setattr(comfyui.httpx, "post", boom)

    result = await ai_interaction.do_generate_image("a dragon", owner=None)

    assert "error" in result
    # F1: no endpoint URL/host, local paths, or raw error message in agent output.
    # (A bare exception class name like "ConnectError" is allowed — it carries
    # no URL/path/secret.)
    for leak in ("/Users/", ".json", "10.1.2.3", "8188", "connect to"):
        assert leak not in result["error"], leak


# 5. Disabled registry model falls through (not used) -----------------------

async def test_disabled_model_not_used_for_default(monkeypatch):
    _patch_settings(monkeypatch, _comfy_settings("qwen-image", enabled=False))

    def _raise(*a, **k):
        raise ValueError("no endpoint")
    monkeypatch.setattr(ai_interaction, "_resolve_model", _raise)

    result = await ai_interaction.do_generate_image("a dragon", owner=None)
    # Disabled → no comfyui route → legacy finds nothing → degraded-state.
    assert result.get("available") is False


# Endpoint-field containment (follow-up #1) ---------------------------------
# The structured `endpoint` field must never reach the agent/LLM/tool output
# path. It may exist only in admin/debug contexts (the probe dict / logs).

async def test_generate_degraded_output_has_no_endpoint_field(monkeypatch):
    # No-model degraded output returned to the agent.
    _patch_settings(monkeypatch, {"media_models": [], "image_model": ""})
    monkeypatch.setattr(ai_interaction, "_resolve_model", lambda *a, **k: (_ for _ in ()).throw(ValueError("no endpoint")))

    result = await ai_interaction.do_generate_image("a dragon", owner=None)

    assert "endpoint" not in result and "endpointUrl" not in result
    # (The shared message may suggest the generic localhost default — that is a
    # hardcoded suggestion, not the user's configured endpoint.)


async def test_comfyui_failure_output_hides_configured_endpoint(monkeypatch):
    # A configured (private-LAN) endpoint that fails must not leak via any
    # agent-visible field, including the structured `endpoint` field.
    settings = _comfy_settings("qwen-image")
    settings["comfyui_endpoint_url"] = "http://10.9.9.9:8188"
    settings["media_models"][0]["endpointUrl"] = "http://10.9.9.9:8188"
    _patch_settings(monkeypatch, settings)

    monkeypatch.setattr(comfyui.httpx, "post", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("connect to 10.9.9.9 failed")))

    result = await ai_interaction.do_generate_image("a dragon", owner=None)

    assert "endpoint" not in result and "endpointUrl" not in result
    blob = json.dumps(result)
    assert "10.9.9.9" not in blob and "8188" not in blob


async def test_list_media_models_output_has_no_endpoint(monkeypatch):
    settings = _comfy_settings("qwen-image")
    settings["media_models"][0]["endpointUrl"] = "http://10.9.9.9:8188"
    _patch_settings(monkeypatch, settings)

    result = await ai_interaction.do_list_media_models("image", owner=None)

    assert result.get("available") is True
    assert "endpoint" not in result and "endpointUrl" not in result
    blob = json.dumps(result)
    assert "endpointUrl" not in blob
    assert "10.9.9.9" not in blob and "8188" not in blob
    for m in result.get("models", []):
        assert "endpointUrl" not in m and "endpoint" not in m


# 6. Existing OpenAI-compatible path still works ----------------------------

class _FakeImgResp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeAsyncClient:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        assert url.endswith("/images/generations")
        b64 = base64.b64encode(b"IMG").decode()
        return _FakeImgResp({"data": [{"b64_json": b64}]})


async def test_legacy_openai_path_still_works(monkeypatch, tmp_path):
    # No media registry models configured → legacy path used.
    _patch_settings(monkeypatch, {"media_models": [], "image_model": "gpt-image-1"})
    monkeypatch.setattr(
        ai_interaction,
        "_resolve_model",
        lambda spec, owner=None: ("https://api.example.com/v1/chat/completions", "gpt-image-1", {"Authorization": "Bearer x"}),
    )
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.chdir(tmp_path)

    result = await ai_interaction.do_generate_image("a city skyline\ngpt-image-1\n1024x1024", owner=None)

    assert result.get("image_url", "").startswith("/api/generated-image/")
    assert result.get("image_model") == "gpt-image-1"
