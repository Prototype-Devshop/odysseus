"""Agent-loop tests for deterministic image-discovery pre-routing."""

import asyncio
import json

import src.agent_loop as al
from src import media_registry as mr


def _collect(gen):
    async def _run():
        return [c async for c in gen]

    return asyncio.run(_run())


def _events(chunks):
    out = []
    for chunk in chunks:
        if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
            try:
                out.append(json.loads(chunk[6:]))
            except Exception:
                pass
    return out


def _patch_loop_basics(monkeypatch):
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    monkeypatch.setattr(al, "_load_mcp_disabled_map", lambda: {}, raising=False)
    monkeypatch.setattr(
        al,
        "_build_system_prompt",
        lambda messages, *a, **k: (messages, []),
        raising=False,
    )

    async def _no_teacher(*_a, **_k):
        return
        yield  # pragma: no cover

    monkeypatch.setattr(
        "src.teacher_escalation.run_teacher_inline",
        _no_teacher,
        raising=False,
    )

    def _trim(messages, *a, **k):
        return messages

    monkeypatch.setattr("src.context_compactor.trim_for_context", _trim, raising=False)


def _no_model_result():
    _, degraded = mr.default_image_model_or_degraded(
        settings={"media_models": [], "default_image_media_model": "", "image_model": ""},
    )
    text = mr.format_degraded_message(degraded)
    return {
        "results": text,
        "models": [],
        "status": degraded.get("status"),
        "available": False,
    }


_CREATION_PROMPT = "Generate an image of a red bicycle on a white background."


def test_deterministic_image_creation_answer_uses_degraded_text():
    payload = _no_model_result()
    answer = al._deterministic_image_creation_answer(payload)
    assert answer == payload["results"]
    assert "no image model" in answer.lower()
    assert "available as a tool" in answer.lower()


def test_deterministic_image_creation_answer_none_when_models_available():
    assert al._deterministic_image_creation_answer({
        "results": "Configured image models (1):",
        "available": True,
        "status": None,
    }) is None


def test_creation_preroute_streams_degraded_answer_without_model_call(monkeypatch):
    _patch_loop_basics(monkeypatch)
    model_called = []

    async def _fake_stream(*_a, **_k):
        model_called.append(True)
        yield "data: " + json.dumps({"delta": "I cannot generate an image"}) + "\n\n"
        yield "data: [DONE]\n\n"

    async def _fake_dispatch(tool, content, session_id=None, owner=None):
        assert tool == "list_media_models"
        return ("list_media_models", _no_model_result())

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    monkeypatch.setattr("src.ai_interaction.dispatch_ai_tool", _fake_dispatch, raising=False)
    monkeypatch.setattr(
        "src.tool_index.should_preroute_image_discovery",
        lambda query, owner="", settings=None: "creation"
        if query == _CREATION_PROMPT
        else None,
        raising=False,
    )

    chunks = _collect(
        al.stream_agent_loop(
            "http://local.test/v1",
            "local-model",
            [{"role": "user", "content": _CREATION_PROMPT}],
            max_rounds=1,
            relevant_tools={"list_media_models", "bash"},
        )
    )
    events = _events(chunks)
    assert model_called == []
    tool_starts = [e for e in events if e.get("type") == "tool_start"]
    assert tool_starts and tool_starts[0]["tool"] == "list_media_models"
    agent_steps = [e for e in events if e.get("type") == "agent_step"]
    assert agent_steps and agent_steps[0]["round"] == 1
    deltas = "".join(e.get("delta", "") for e in events if "delta" in e)
    delta_idx = next(i for i, e in enumerate(events) if "delta" in e)
    agent_step_idx = next(i for i, e in enumerate(events) if e.get("type") == "agent_step")
    assert agent_step_idx < delta_idx, "agent_step must precede final delta for live render"
    assert "no image model" in deltas.lower()
    assert "available as a tool" in deltas.lower()
    assert "cannot generate" not in deltas.lower()
    metrics = next(e for e in events if e.get("type") == "metrics")
    assert metrics["data"]["round_texts"] == [deltas]


def test_capability_preroute_still_calls_model(monkeypatch):
    _patch_loop_basics(monkeypatch)
    model_called = []

    async def _fake_stream(*_a, **_k):
        model_called.append(True)
        yield "data: " + json.dumps({"delta": "Image generation is available as a tool."}) + "\n\n"
        yield "data: [DONE]\n\n"

    async def _fake_dispatch(tool, content, session_id=None, owner=None):
        return ("list_media_models", _no_model_result())

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    monkeypatch.setattr("src.ai_interaction.dispatch_ai_tool", _fake_dispatch, raising=False)
    monkeypatch.setattr(
        "src.tool_index.should_preroute_image_discovery",
        lambda query, owner="", settings=None: "capability"
        if query == "Can you make images?"
        else None,
        raising=False,
    )

    _collect(
        al.stream_agent_loop(
            "http://local.test/v1",
            "local-model",
            [{"role": "user", "content": "Can you make images?"}],
            max_rounds=1,
            relevant_tools={"list_media_models"},
        )
    )
    assert model_called == [True]


def test_configured_creation_does_not_preroute(monkeypatch):
    _patch_loop_basics(monkeypatch)
    dispatch_called = []

    async def _fake_stream(*_a, **_k):
        yield "data: " + json.dumps({"delta": "ok"}) + "\n\n"
        yield "data: [DONE]\n\n"

    async def _fake_dispatch(*_a, **_k):
        dispatch_called.append(True)
        return ("list_media_models", _no_model_result())

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    monkeypatch.setattr("src.ai_interaction.dispatch_ai_tool", _fake_dispatch, raising=False)
    monkeypatch.setattr(
        "src.tool_index.should_preroute_image_discovery",
        lambda query, owner="", settings=None: None,
        raising=False,
    )

    _collect(
        al.stream_agent_loop(
            "http://local.test/v1",
            "local-model",
            [{"role": "user", "content": _CREATION_PROMPT}],
            max_rounds=1,
            relevant_tools={"list_media_models", "generate_image"},
        )
    )
    assert dispatch_called == []
