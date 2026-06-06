# Task List: Media Generation Provider Layer

> Companion to [`plan.md`](plan.md) and [`open-questions.md`](open-questions.md).
> Status values: `todo` / `doing` / `blocked` / `done`. Owner: human or agent (Architect/Forge/Infra/Gatekeeper/Verifier).

## Tasks

| ID | Slice | Description | Owner | Status | Links |
|----|-------|-------------|-------|--------|-------|
| T1.1 | S1 | Map agent mode entry + loop (`src/agent_loop.py` `stream_agent_loop`; `routes/chat_routes.py` `/api/chat_stream`, `mode`; `src/action_intents.py`; `core/database.py` session mode) | Architect | done | plan.md "Current implementation points" #1 |
| T1.2 | S1 | Map tool registration surfaces (`src/tool_schemas.py`, `src/agent_tools.py` `TOOL_TAGS`, `src/agent_loop.py` `TOOL_SECTIONS`, `src/tool_index.py`, `src/tool_parsing.py`, `src/tool_execution.py` `execute_tool_block`/`_MCP_TOOL_MAP`, `src/tool_implementations.py`, `src/ai_interaction.py`) | Architect | done | plan.md #2 |
| T1.3 | S1 | Map model/provider config + probing + existing image gen (`core/database.py` `ModelEndpoint`, `src/settings.py`, `src/endpoint_resolver.py`, `src/ai_interaction.py` `_resolve_model`/`do_generate_image`, `mcp_servers/image_gen_server.py`, `scripts/diffusion_server.py`, `routes/model_routes.py` probes) | Architect | done | plan.md #3,#4,#5 |
| T1.4 | S1 | Map asset storage + settings persistence + degraded-state patterns (`data/generated_images/` + `gallery_images`, `_save_to_gallery`, `GET /api/generated-image/{filename}`; `data/settings.json`/`src/settings.py`; `src/settings_scrub.py`; `core/database.py` `EncryptedText`) | Architect | done | plan.md #6,#7 |
| T1.5 | S1 | Write `plan.md`, `task-list.md`, `open-questions.md` | Architect | done | this file |
| T2.1 | S2 | Decide registry source of truth + backing store | Architect/Infra | done | OQ-1, OQ-2 resolved (hybrid registry; settings.json backing) |
| T2.2 | S2 | Implement media model registry: register entry, list enabled image models | Forge | done | `src/media_registry.py` (`normalize_model`, `load_media_models`, `list_enabled_models`); keys added to `src/settings.py` `DEFAULT_SETTINGS` |
| T2.3 | S2 | Implement default image model resolution (and explicit deferral when default omitted) | Forge | done | `src/media_registry.py` `resolve_default_model` (setting → isDefault → single-enabled) |
| T2.4 | S2 | Define shared degraded-state response shape; return structured no-default error | Forge | done | `src/media_registry.py` `degraded_state`, `default_image_model_or_degraded`, `format_degraded_message` (AC-3, R5) |
| T2.5 | S2 | Add tests/validation script for registry list + default resolution + no-default error | Forge/Verifier | done | `tests/test_media_registry.py` (23 tests passing) |
| T3.1 | S3 | Create isolated ComfyUI provider module (config-driven, UI-isolated; `services/`-style per TTS/STT precedent) | Forge/Infra | done | `services/media/comfyui.py` (`ComfyUIProvider`) + `services/media/__init__.py`; not wired into `services/__init__.py` to keep it isolated |
| T3.2 | S3 | Implement ComfyUI probe (accept endpoint URL; `httpx` + bounded 5s timeout; `GET /system_stats` → fallback `GET /object_info`) | Forge | done | `ComfyUIProvider.probe()` / `from_settings()` / module `probe()`. Note: `llm_verify()` deliberately NOT used — ComfyUI is a local runtime, not an LLM provider; the extra-CA override is test-pinned to LLM call sites (`test_tls_overrides_scope.py`) |
| T3.3 | S3 | Return structured provider status; surface clear connection failures (reuse degraded-state shape) | Forge | done | Status dicts reuse `media_registry.degraded_state` shape (`online`/`not_configured`/`unreachable`/`auth_error`/`unavailable`) + `provider`/`endpoint`/`via` (AC-4, R5) |
| T3.4 | S3 | Tests for probe: reachable, fallback success, unreachable/offline, malformed response | Forge/Verifier | done | `tests/test_comfyui_provider.py` (13 tests passing; httpx mocked per repo convention) |
| T4.1 | S4A | Register `list_media_models` tool across schema/tags/index/parsing/dispatch | Forge | done | AC-2. Added to `TOOL_TAGS` (`agent_tools.py`), `FUNCTION_TOOL_SCHEMAS` + native arg map (`tool_schemas.py`), `_TOOL_NAME_MAP` (`tool_parsing.py`), `dispatch` group (`tool_execution.py`), `do_list_media_models` + dispatcher (`ai_interaction.py`), RAG description + image keyword hint (`tool_index.py`), `TOOL_SECTIONS` + non-admin keep-list (`agent_loop.py`). Reads via `media_registry`; public output omits `endpointUrl`/`workflowPath`; degraded-state reused when none configured. Tests: `tests/test_list_media_models_tool.py` (12). |
| T4.2 | S4 | Adapt `generate_image` (`do_generate_image`, canonical owner-aware path) to resolve via registry (model ID or default), not hardcoded names; MCP server delegates | Forge | todo | AC-1, AC-5; OQ-5 resolved |
| T4.3 | S4 | Provider-branched generation: submit ComfyUI job (`POST /prompt`), poll `GET /history/{id}` (≤120s, 1–2s interval, `progress_cb`), retrieve via `GET /view` | Forge | blocked | AC-6; needs reachable ComfyUI (D1) |
| T4.4 | S4 | Workflow handling: load the single bundled workflow JSON as data; substitute known fields only (no arbitrary user workflows) | Forge | todo | OQ-4 resolved, R4 |
| T4.5 | S4 | Degraded states: model disabled, workflow missing, generation failed (provider error preserved, no secrets/paths) | Forge | todo | AC-3, R4 |
| T4.6 | S4 | Gatekeeper review (workflow/prompt injection, path/secret exposure) | Gatekeeper | todo | R4 |
| T5.1 | S5 | Save ComfyUI output to gallery store (`data/generated_images/` + `gallery_images`); ensure owner/session set | Forge | todo | AC-7, R2 |
| T5.2 | S5 | Store media metadata in existing gallery fields first; JSON sidecar beside the image if it does not fit (no new DB columns) | Forge | todo | AC-8; OQ-6 resolved |
| T5.3 | S5 | Return asset/file reference to agent (keep `do_generate_image` return contract: `image_url`/`image_id`) | Forge | todo | AC-7, A5 |
| T6.1 | S6 | (If needed) Configure ComfyUI endpoint via existing settings patterns; no new visual styles | Forge | todo | AC-4; OQ-2 |
| T6.2 | S6 | (If needed) Enable/disable model + select default image model in settings | Forge | todo | AC-5 |
| T6.3 | S6 | (If needed) Minimal settings UI wiring, reusing existing components; `node --check` touched JS | Forge/Prism | todo | brief Slice 6 |
| TV.1 | S2–S6 | Run per-slice checks: `py_compile`, targeted `pytest`, `node --check` | Verifier | todo | brief Verification |
| TV.2 | S4–S6 | Execute manual 8-case test matrix; verification report vs AC-1…AC-10 | Verifier | todo | brief Verification |

## Blocked items

| ID | Reason | Unblocker |
|----|--------|-----------|
| T4.3 | ComfyUI generation cannot be end-to-end verified without a reachable instance | A running ComfyUI endpoint available for testing (D1) |

> All open questions (OQ-1…OQ-9) were resolved on S2 kickoff; see [`open-questions.md`](open-questions.md). The remaining true blocker is environmental (a reachable ComfyUI instance for S4 generation verification).

## Dependencies between tasks

- T2.2–T2.5 depend on T2.1.
- T3.2–T3.4 depend on T3.1.
- T4.2–T4.5 depend on S2 (registry) and S3 (provider); T4.3/T4.4 also on a reachable ComfyUI (D1).
- T5.1–T5.3 depend on S4.
- T6.x depends on S4/S5 and only proceeds if configuration is not already covered by existing endpoint settings.
- TV.1/TV.2 run continuously per slice and at release.
