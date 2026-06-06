# Plan: Media Generation Provider Layer (ComfyUI image generation)

> Source brief: [`docs/build-brief-media-generation.md`](../docs/build-brief-media-generation.md)
> Produced by: Architect (planning only — no application code in this slice)
> Companion artifacts: [`task-list.md`](task-list.md), [`open-questions.md`](open-questions.md)

---

## Executive summary

Odysseus already has working image generation (an agent `generate_image` tool, a gallery/asset store, and `image_model` / `image_gen_enabled` settings), but it is wired exclusively to **OpenAI-compatible** `/v1/images/generations` endpoints (cloud `gpt-image`/DALL·E and the local `scripts/diffusion_server.py` Diffusers runtime). There is **no ComfyUI provider, no media-model registry, and no `list_media_models` tool**. Because model selection currently flows through generic LLM/image `ModelEndpoint` rows and a loosely-resolved `image_model` string, the agent can attempt models that no configured endpoint actually serves — exactly the failure mode the brief describes.

This plan adds a small, modular **media provider layer** in front of image generation:

1. A **media model registry** that lists enabled image models, resolves a default, and returns structured errors when none exist.
2. A **ComfyUI provider module** (probe first, then queue/poll/retrieve) that lives isolated from UI code, modeled on the existing `services/tts` and `services/stt` multi-provider service pattern.
3. A **registry-aware `generate_image` path** plus a new **`list_media_models`** tool so the agent uses configured model IDs instead of guessing names, with clear degraded-state responses.
4. **Asset persistence** of ComfyUI output into the existing `data/generated_images/` + `gallery_images` store with media metadata.
5. **Minimal settings/UI** (only if needed) for endpoint URL, enable/disable, and default selection, reusing existing settings conventions.

The work is decomposed to match the brief's six suggested slices. Slice 1 (this repo map and plan) is delivered by this artifact set. Slices 2–6 are sequenced for Forge/Infra. Video and additional providers are explicitly deferred but the layer is shaped to admit them.

---

## Current implementation points (repo map)

The brief's Slice 1 asks for the current implementation points. Findings below are the basis for the plan; exact line references are in the task list and open questions.

| # | Concern | Where it lives today | Notes for media layer |
|---|---------|----------------------|------------------------|
| 1 | **Agent mode** | `src/agent_loop.py` (`stream_agent_loop`, the multi-round tool loop); HTTP entry `routes/chat_routes.py` (`/api/chat_stream`, `mode` field, chat→agent auto-escalation via `src/action_intents.py`); session mode in `core/database.py` (`get/set_session_mode`) | New tools plug into the existing loop; no new loop needed. |
| 2 | **Tool registration** | `src/tool_schemas.py` (`FUNCTION_TOOL_SCHEMAS`), `src/agent_tools.py` (`TOOL_TAGS`), `src/agent_loop.py` (`TOOL_SECTIONS` fenced prompt), `src/tool_index.py` (RAG/keyword selection), `src/tool_parsing.py` (`_TOOL_NAME_MAP`), `src/tool_execution.py` (`execute_tool_block` dispatch + `_MCP_TOOL_MAP`), `src/tool_implementations.py` / `src/ai_interaction.py` (impls) | `generate_image` already registered (routes via MCP `image_gen` server / `do_generate_image`); `list_media_models` is **new** and follows the same multi-file registration. |
| 3 | **Model/provider configuration** | SQLite `ModelEndpoint` table (`core/database.py`, has `model_type` `"llm"`/`"image"`, encrypted `api_key`); role bindings in `data/settings.json` (`src/settings.py` `DEFAULT_SETTINGS`, incl. `image_model`, `image_gen_enabled`, `image_quality`); resolution in `src/endpoint_resolver.py` and `src/ai_interaction.py` `_resolve_model()`; provider type is an informal string from `src/llm_core.py` `_detect_provider()` | Registry sits on top of these. ComfyUI is **not** OpenAI-compatible, so it does not fit the `ModelEndpoint`/`_resolve_model` `/v1/models` assumption cleanly — see open questions OQ-1/OQ-2. |
| 4 | **Existing image generation tools** | `generate_image` tool → `mcp_servers/image_gen_server.py` + canonical `src/ai_interaction.py` `do_generate_image()`; local runtime `scripts/diffusion_server.py`; gallery transform proxies in `routes/gallery_routes.py`; `edit_image` tool. **All OpenAI-`/v1/images/generations`-shaped. No ComfyUI.** | Reuse the save/return contract of `do_generate_image`; branch generation by provider. |
| 5 | **Endpoint probing** | `routes/model_routes.py` (`_probe_endpoint` GET models, `_ping_endpoint` reachability, `_probe_single_model` POST chat); `src/model_discovery.py` port scan; `httpx` with tiered timeouts + `verify=llm_verify()`; degraded states surfaced as `offline`/`empty` (no unified `degraded` abstraction) | ComfyUI probe is a **new** provider-specific probe (e.g. `GET /system_stats` or `/object_info`), structurally similar but not OpenAI-shaped. |
| 6 | **File/workspace/asset storage** | Generated images: `data/generated_images/{12-hex}.{ext}` + SQLite `gallery_images` row (owner-scoped, prompt/model/size metadata) via `_save_to_gallery()`; chat uploads: `data/uploads/YYYY/MM/DD/...` + `uploads.json`; served at `GET /api/generated-image/{filename}` | Save ComfyUI output through the **gallery** path to inherit owner scoping + serve route + metadata columns. |
| 7 | **Settings/config persistence** | `data/settings.json` (JSON, `src/settings.py`, admin-gated via `routes/auth_routes.py`, secret keys scrubbed by `src/settings_scrub.py`); SQLite for endpoints; per-user `data/user_prefs.json`; Fernet `EncryptedText` for secrets | Media-provider config can extend `DEFAULT_SETTINGS` and/or reuse `ModelEndpoint` rows — decision deferred to OQ-2. |

Additional finding (brief Slice 1 item "existing degraded-state reporting patterns"): there is **no shared degraded-state contract**. Today it is ad hoc (`offline`/`empty` flags, `_format_upstream_error`, HTTP 503 strings). The registry should introduce a small, consistent degraded-state response shape used by both `list_media_models` and `generate_image`.

---

## Scope

### In scope (this initiative)

- Media model **registry** for image models: register entries, list enabled, resolve default, structured no-default error.
- Configurable **ComfyUI provider** module (isolated from UI): endpoint URL, probe, queue prompt/workflow, poll status, retrieve output, return file/asset reference.
- ComfyUI **endpoint probe** with structured status and clear connection failures.
- Registry-aware `generate_image` (uses model IDs, not hardcoded names) + new `list_media_models` tool.
- Clear **degraded-state** responses (no models; provider unavailable; model disabled; workflow missing; generation failed).
- **Persist** generated image into existing gallery/asset store with basic media metadata.
- **Minimal** settings/UI only if required for configuration (endpoint URL, enable/disable, default selection), reusing existing patterns.
- Smallest relevant **verification** per slice (`py_compile`, targeted `pytest`, `node --check` for any touched JS).

### Out of scope (explicit)

- Video generation (Wan/LTX) — layer is shaped to admit it later only.
- Diffusers provider rework, Qwen-Image/FLUX presets, workflow marketplace.
- Automatic model installation, model downloads, LoRA management, GPU capability detection.
- Advanced prompt editor, queue management, cancel/retry, progress UI for long jobs.
- New visual design system, broad CSS changes, broad frontend refactors, file moves, formatting-only changes.
- Commercial license validation/automation.
- Changes to unrelated Odysseus provider behavior.

---

## Assumptions

- **A1** — A user can run ComfyUI locally and reach it over HTTP; default suggestion `http://localhost:8188`. (Brief.)
- **A2** — ComfyUI exposes its standard HTTP API (`POST /prompt`, `GET /history/{id}`, `GET /view`, plus a probe-able endpoint such as `/system_stats` or `/object_info`). To be confirmed against the target ComfyUI build — OQ-3.
- **A3** — Generated output should be saved through the existing gallery store (`data/generated_images/` + `gallery_images`) so it inherits owner scoping, the `/api/generated-image/{filename}` serve route, and metadata columns. (Matches brief item 5/6.)
- **A4** — The registry can be a lightweight in-process module backed by existing persistence (settings JSON and/or `ModelEndpoint`); a new heavy data store is not required for MVP. Final backing store is OQ-2.
- **A5** — `generate_image` keeps its existing return contract (`image_url`, `image_id`, prompt/model/size) so the agent loop's SSE forwarding (`src/agent_loop.py`) keeps working unchanged.
- **A6** — Existing OpenAI-compatible image generation must keep working; ComfyUI is added as an additional provider, not a replacement (provider-branched dispatch).
- **A7** — Privilege gating reuses the existing `can_generate_images` privilege; no new privilege is introduced for MVP.
- **A8** — Workflow JSON files (if used) are treated as untrusted config: loaded as data, with prompt-text substitution into known node fields only; no execution of embedded instructions (brief Safety section).

---

## Risks

| ID | Risk | Impact | Likelihood | Mitigation |
|----|------|--------|------------|------------|
| R1 | ComfyUI API does not fit the OpenAI `ModelEndpoint`/`_resolve_model` model (async queue + poll + `/view`, no `/v1/models`) | Forcing it into existing endpoint plumbing causes leaky abstractions or breaks LLM endpoint code | High | Keep ComfyUI provider as an **isolated module** (services-style), bridged via the registry; do not overload `_resolve_model`. Resolve OQ-1/OQ-2 before Slice 4. |
| R2 | Two image-gen entry paths exist (MCP `image_gen_server.py` and `ai_interaction.do_generate_image`) and the MCP path omits `owner`/`session_id` on gallery insert | Owner-scope leak or inconsistent behavior when ComfyUI added | Medium | Route ComfyUI through the canonical `do_generate_image` path (owner-aware) or fix MCP insert to set owner; record in OQ-5. |
| R3 | Long ComfyUI jobs exceed current tool/HTTP timeouts; agent loop may stall | Failed/timed-out generations, poor UX | Medium | Use polling with bounded timeout + progress callback already supported by `execute_tool_block`; document max wait; cancel/retry deferred. |
| R4 | Workflow JSON / prompt injection via untrusted workflow files or metadata | Security (arbitrary behavior, path disclosure) | Medium | Treat workflows as data; substitute prompt only into known fields; never echo local filesystem paths to agent/user; no secrets in tool output. Gatekeeper review before Slice 4/5. |
| R5 | Degraded-state messaging implemented ad hoc per call site (as today) | Inconsistent UX, regressions | Medium | Define one degraded-state response shape in the registry (Slice 2) and reuse it in `list_media_models` and `generate_image`. |
| R6 | Settings/config split (JSON vs `ModelEndpoint` rows) chosen wrongly | Rework across slices, migration pain | Medium | Decide OQ-2 in Slice 2 before provider/probe work; prefer the lowest-footprint option consistent with conventions. |
| R7 | Touching shared model-resolution code risks unrelated LLM endpoint behavior | Regression in core chat | Low/High | Prefer additive, provider-branched code paths; run targeted endpoint/model tests; Infra review for any change to `endpoint_resolver`/`model_routes`. |

---

## Dependencies

- **D1** — A reachable ComfyUI instance (local) for manual verification of Slices 3–5. Probe/generation cannot be end-to-end verified without it.
- **D2** — Confirmation of the target ComfyUI HTTP API surface and a baseline image workflow JSON (OQ-3, OQ-4).
- **D3** — Decision on registry backing store and ComfyUI config location (OQ-1, OQ-2) before Slice 3/4 implementation.
- **D4** — Existing gallery store (`data/generated_images/`, `gallery_images`, `GET /api/generated-image/{filename}`) as the persistence target (Slice 5).
- **D5** — Existing tool-registration surfaces (`tool_schemas`, `TOOL_TAGS`, `tool_index`, `tool_execution`, `agent_loop` `TOOL_SECTIONS`) for adding `list_media_models` and adapting `generate_image` (Slices 2/4).
- **D6** — `can_generate_images` privilege and `image_gen_enabled` setting as existing gates.
- **Sequencing**: S1 → S2 → S3 → S4 → S5 → S6. S3 depends on S2 (registry/config shape); S4 depends on S2+S3; S5 depends on S4; S6 depends on S4/S5.

---

## Acceptance criteria (from brief)

Mapped from the brief's **Success Criteria** (and degraded-state/UX requirements):

- **AC-1** Agent no longer guesses unavailable image models (uses configured model IDs).
- **AC-2** Configured/enabled media models can be listed (`list_media_models`).
- **AC-3** Missing-provider/no-model state is clear and actionable (degraded-state message with checked providers + next steps).
- **AC-4** ComfyUI endpoint can be probed; clear unavailable-state error.
- **AC-5** Default image model can be resolved (and `generate_image` works with `modelId` omitted).
- **AC-6** Image generation can route through the configured ComfyUI provider (queue → poll → retrieve).
- **AC-7** Generated output is returned to the user and/or saved into the workspace/gallery store.
- **AC-8** Basic media metadata is preserved with the saved asset.
- **AC-9** Implementation remains modular enough to support future video/providers.
- **AC-10** Existing Odysseus behavior remains stable (existing image gen + LLM endpoints unaffected).

---

## Execution slices

Slice IDs align with the brief's "Suggested Implementation Slices". Tasks are tracked in [`task-list.md`](task-list.md); open questions in [`open-questions.md`](open-questions.md).

### S1 — Repo map and plan (this slice)

- **Goal**: Identify current implementation points (agent mode, tool registration, model/provider config, existing image gen, endpoint probing, file/asset storage, settings persistence, degraded-state patterns) and produce planning artifacts.
- **Tasks**: T1.1–T1.4 (see task list).
- **AC references**: Enables AC-1…AC-10; no runtime AC delivered here.
- **Notes**: Planning only — no application code. Delivered by `plan.md`, `task-list.md`, `open-questions.md`. **Status: done.**

### S2 — Media model registry

- **Goal**: Small registry that registers media-model entries, lists enabled image models, resolves a default image model, and returns a structured degraded-state error when none exists.
- **Tasks**: T2.1–T2.5.
- **AC references**: AC-2, AC-3, AC-5, AC-9; defines the shared degraded-state shape used by AC-3.
- **Notes**: Resolve OQ-1 (registry source of truth) and OQ-2 (backing store) here. Follow `MediaModel` shape from the brief but adapt to repo conventions. Add tests/validation script if consistent with repo. No ComfyUI calls yet.

### S3 — ComfyUI provider probe

- **Goal**: Isolated ComfyUI provider module that accepts an endpoint URL, probes availability, returns structured provider status, and surfaces clear connection failures. **No image generation yet.**
- **Tasks**: T3.1–T3.4.
- **AC references**: AC-4, AC-9.
- **Notes**: Model the module on `services/tts`/`services/stt` precedent (config-driven, UI-isolated). Resolve OQ-3 (probe endpoint) and OQ-2 (where endpoint URL is stored). Use `httpx` + `verify=llm_verify()` + bounded timeout, matching existing probe conventions.

### S4 — Generate image flow

- **Goal**: Wire `generate_image` to the registry + ComfyUI provider: use configured model ID or default, submit the job, poll for completion, retrieve output, return result to the agent. Add `list_media_models` tool.
- **Tasks**: T4.1–T4.6.
- **AC references**: AC-1, AC-2, AC-5, AC-6, AC-10; degraded states for model-disabled/workflow-missing/generation-failed.
- **Notes**: Provider-branch generation (OpenAI-compat vs ComfyUI) behind the registry; keep `do_generate_image` return contract (AC-5/A5). Resolve OQ-4 (workflow handling) and OQ-5 (which entry path). Gatekeeper review for workflow/prompt-injection (R4).

### S5 — Asset/file persistence

- **Goal**: Save ComfyUI output into the existing gallery store with basic metadata; return an asset/file reference to the agent.
- **Tasks**: T5.1–T5.3.
- **AC references**: AC-7, AC-8.
- **Notes**: Reuse `data/generated_images/` + `gallery_images` + `_save_to_gallery()` and the `/api/generated-image/{filename}` serve route; ensure owner/session set (R2). Store media metadata fields (provider, workflow, seed, width, height, model label) where columns allow; if not, document nearest pattern and propose minimal approach (OQ-6).

### S6 — Minimal settings/UI (only if needed)

- **Goal**: Reuse/extend existing settings to configure ComfyUI endpoint, enable/disable a model, and select the default image model — without new visual styles.
- **Tasks**: T6.1–T6.3.
- **AC references**: Supports AC-3, AC-4, AC-5 (configuration path); AC-10.
- **Notes**: Only after backend/tool flow works. Extend `DEFAULT_SETTINGS` + existing admin settings routes/UI; avoid CSS/visual-system changes. May be deferred if config via existing endpoint settings suffices (OQ-2).

---

## Verification approach (per brief)

Run the smallest relevant checks per slice:

```bash
python -m py_compile app.py routes/*.py src/*.py
python -m pytest            # scope to touched areas where possible
node --check static/js/<changed-file>.js   # only if JS touched
```

Manual test matrix (brief Verification): (1) no provider configured; (2) ComfyUI configured but offline; (3) online but no enabled image model; (4) enabled default image model configured; (5) successful generation; (6) failed generation with provider error; (7) agent asks for image without naming a model; (8) agent asks for an unavailable model. Verifier owns the report against AC-1…AC-10.

---

## Open questions

See [`open-questions.md`](open-questions.md). The blocking ones for implementation start are OQ-1 (registry source of truth), OQ-2 (config backing store), OQ-3 (ComfyUI probe endpoint), and OQ-4 (workflow handling).
