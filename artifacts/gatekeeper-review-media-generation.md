# Gatekeeper Review — Media Generation (S4B)

> Scope: security / privacy / tenancy / deploy-risk review of the **S4B** media
> generation changes only (ComfyUI generation + registry routing + MCP
> delegation). Reviewer role: Gatekeeper (per `agents/prototyper/agents/gatekeeper.md`).
> No product-scope decisions; remediations are stated as requirements, not code.

## Files reviewed

- `services/media/comfyui.py` (provider: probe + `generate` queue/poll/retrieve)
- `services/media/workflows/text_to_image.json` (bundled workflow template)
- `src/ai_interaction.py` (`do_generate_image`, `_generate_image_via_comfyui`, `_persist_generated_image`)
- `mcp_servers/image_gen_server.py` (delegation to `do_generate_image`)
- `src/media_registry.py` (resolution + degraded-state + presentation helpers)
- `tests/test_comfyui_provider.py`, `tests/test_generate_image_media.py`
- `artifacts/task-list.md`

---

## 1. Pass/Fail recommendation

**CONDITIONAL PASS — go for local (loopback) live testing.**

- **Go** for live testing against a **local/loopback ComfyUI** (e.g. `http://127.0.0.1:8188`). The implementation has no telemetry, makes no third-party calls, sends no secrets/API keys to ComfyUI, does not leak local filesystem paths, and treats workflow JSON and `/view` filenames as untrusted data.
- **No critical (stop-the-line) bug found**, so no code was changed in this review.
- **Two findings (F1, F2) must be addressed before** the ComfyUI endpoint is pointed at any **non-loopback / remote host** or before **multi-user** deployment. Until then they are low-impact (localhost-only).

Risk lens only — this is not product acceptance (that is Verifier's call).

---

## 2. Privacy-first / local-first findings

**F2 (Medium, architectural) — No local-by-default enforcement or remote labeling for the ComfyUI endpoint.**
- `comfyui_endpoint_url` (and per-model `endpointUrl`) is accepted as-is and used directly (`services/media/comfyui.py` `from_settings`, `ComfyUIProvider.__init__`; `src/media_registry.py` `normalize_model` L135–142). The suggested default is local, but nothing distinguishes a loopback endpoint from a remote one, and nothing warns/labels before **user prompts and (implicitly) generation parameters are POSTed** to it (`POST /prompt`, L323).
- Impact: a misconfiguration, an imported settings blob, or a settings-injection could silently send private prompts to a remote host with no user signal. This directly conflicts with the stated trust model ("no surprise external network calls / hidden data egress").
- Recommendation (aligns with the proposed core principle): treat media providers as **local-by-default**; only allow non-loopback endpoints when an admin explicitly opts in, and **label remote endpoints as remote** in `list_media_models` / settings before any prompt or file is sent. See §8 for the concrete control. Owner: Infra/Forge.

**Positive (local-first):** the provider sends **no auth headers / API keys** to ComfyUI, and the LLM extra-CA-bundle override (`src/tls_overrides.py` / `llm_verify`) is deliberately **not** used here (confirmed; `test_tls_overrides_scope.py` still passes). TLS trust is not broadened for the media path.

---

## 3. Data egress findings

- **No hidden outbound calls / no telemetry.** Every network call targets the configured ComfyUI base URL plus a fixed path (`/prompt`, `/history/{id}`, `/view`). No analytics, no phone-home, no third-party hosts. (`services/media/comfyui.py` L323, L345, L370.)
- **Generated artifacts stay local.** Image bytes are written to `data/generated_images/<uuid>.png` and exposed only via the in-app route `/api/generated-image/<filename>` (`_persist_generated_image`, L1640–1644). Nothing is uploaded externally.
- **Indirect egress channel to the LLM (see F1).** Provider **error text** is returned as tool output and therefore becomes part of the conversation context, which—if the active LLM is a remote OpenAI-compatible model—is transmitted to that provider. The error text currently embeds the ComfyUI endpoint URL. This is the one real egress concern and is covered as F1 below.
- **Prompt is logged locally** (`logger.info(... prompt[:80] ...)`, pre-existing). Local logs only; acceptable for local-first, but see §8 hardening.

---

## 4. Prompt / workflow injection findings

**Strong — no critical issues.**
- `apply_workflow_params` (L102–140) **deep-copies** the workflow and substitutes **only** node `inputs` values that are strings **exactly equal** to a known placeholder token (`%prompt%`, `%seed%`, `%width%`, `%height%`, `%negative_prompt%`, `%checkpoint%`). It never reads node metadata, titles, or notes, and never executes anything from the workflow. A hostile/edited workflow cannot smuggle behavior through this path. Verified by `test_generate_substitutes_only_known_fields` (untouched fields and a decoy `Note` node remain unchanged; original template object is not mutated).
- Workflow JSON is loaded purely as data (`json.load`, `_load_default_workflow` L170–178) and validated to be a dict.
- **Instruction-following risk:** the workflow output / history / `/view` response is treated as opaque data (status codes + bytes), never parsed back into the agent's instruction stream. The agent does **not** follow text from workflow metadata, image metadata, or provider responses.
- **Minor (Low) — prompt newlines can shift the line-based tool protocol.** The MCP server builds `content = "\n".join([prompt, model, size, quality])` (`image_gen_server.py` L64) and `do_generate_image` parses by line. A prompt containing a newline could populate the "model" line. Impact is bounded: it can only select an **already-configured** model id (no URL/host control, no arbitrary endpoint), so it is not an SSRF/egress vector — but it is sloppy. Recommend passing structured fields instead of newline-joining (see §8). This pattern is pre-existing (also in the chat-mode shortcut).

---

## 5. Path / secret / error leakage findings

**F1 (Medium) — ComfyUI endpoint URL is leaked into agent-visible error output.**
- The generate failure path returns `{"error": media_registry.format_degraded_message(result)}` (`ai_interaction.py` L1721). `format_degraded_message` renders `message` + `next_steps` + `detail` (`media_registry.py` L376–401), and the provider's degraded builders embed `self.endpoint_url` in both the `message` and `next_steps` for `_unreachable` (L472, L475–476), `_generation_failed` (L523), `_timeout` (L549), and `_auth_error` (L486). For network errors, `detail=str(e)` (L325/L347/L372) may additionally include host/port text.
- Impact: the endpoint URL reaches the model context and, with a remote LLM, is transmitted off-box. For the localhost default this is low-sensitivity; for an admin-configured private/internal URL (e.g. `http://10.x.x.x:8188` or an internal hostname) it is meaningful internal-topology leakage. The existing tests intentionally only assert absence of `"/Users/"` and `".json"`, so the endpoint URL passing through is currently un-guarded.
- Recommendation: keep endpoint URLs in **server logs only**; scrub them from any text returned to the agent/user (e.g. user-facing copy "ComfyUI is unavailable" without the URL; or run returned messages through the existing settings-scrub utility). Add a test asserting the configured endpoint URL is **absent** from `do_generate_image` error output. Owner: Forge. **Required before any non-loopback endpoint.**

**F5 (Low) — generic exception text echoed.** `_generate_image_via_comfyui` returns `{"error": f"ComfyUI generation error: {str(e)}"}` (L1718). `str(e)` of an unexpected exception could contain a path/URL. Bound or genericize this message; log full detail server-side. Owner: Forge.

**Positives (leakage):**
- **Response bodies are never surfaced** — only HTTP **status codes** are placed in `detail` (e.g. `"queue request returned HTTP {code}"`, L330/L375). Verified by `test_generate_queue_http_error_is_preserved_without_leaks`.
- **No local filesystem paths leak.** Saved files use random UUID names; the local path is never echoed to the agent (only the `/api/...` URL). `to_public_dict` omits `endpointUrl` and `workflowPath` for `list_media_models` (`media_registry.py` L361–367).
- **No secrets.** ComfyUI requests carry no Authorization headers / API keys, so there is nothing secret to leak from the media path.

---

## 6. Owner / session scoping findings

**F3 (Low for single-user local; Medium for multi-user) — agent/MCP-generated images are persisted unattributed.**
- The canonical chat-mode shortcut passes the real user: `do_generate_image(..., owner=_user)` (`routes/chat_routes.py` L844) → `_persist_generated_image` records `owner`/`session_id` correctly (`ai_interaction.py` L1657–1658).
- However, the **MCP delegation** (the normal agent-mode `generate_image` tool path) calls `do_generate_image(content, owner=None)` (`image_gen_server.py` L65), so images generated by the agent are saved with `owner=None` and `session_id=None`. This is consistent with the **pre-existing** MCP limitation (the old server also didn't set owner), but S4B makes this the canonical generation path, so it is now the de-facto behavior for agent-generated images.
- Impact: acceptable for single-user local deployments (the product's default). For any multi-user deployment, agent-generated images would be unattributed/global and could surface across users via gallery queries. Recommend threading owner/session into the MCP path (or documenting MCP generation as single-user-only). Owner: Infra. Note: registry resolution is intentionally global admin config (owner accepted but unused) — that is fine for media config.

**Positive:** owner/session **are** plumbed end-to-end on the direct path, and the gallery owner-filter behavior remains covered (`test_gallery_owner_filter_single_user.py` passes).

---

## 7. SSRF / endpoint-handling findings

- **No prompt-controlled URLs.** The request host/base comes only from admin settings; user prompts never influence the destination host. No SSRF from user input.
- **F4 (Low) — provider-supplied `prompt_id` is interpolated unencoded** into `f"{base}{HISTORY_PATH}/{prompt_id}"` (L345). `prompt_id` originates from ComfyUI's own `/prompt` response. Because it is appended to the **path** of an already-fixed `scheme://host:port`, it cannot redirect to a different host (no SSRF), but a hostile/compromised ComfyUI returning an odd `prompt_id` could malform the request. Recommend validating `prompt_id` (e.g. expected charset) and URL-encoding it. The `/view` params are passed via httpx `params=` and are query-encoded correctly (L370) — good; the `/view` filename is treated as a **remote provider identifier**, never as a local path. Owner: Forge.
- **Bounded resource use:** per-request timeout (30s) and bounded polling (120s budget, 1.5s interval) limit hang/amplification (L69–74, L340–358). Good.

---

## 8. Required fixes & recommended hardening

### Required before live testing
- **(Local loopback testing: none blocking.)** The implementation is safe to exercise against a localhost ComfyUI.

### Required before remote / multi-user use
- **F1 — Strip endpoint URLs from agent-facing error/degraded text** (log them server-side instead); add a regression test asserting the configured URL is absent from `do_generate_image` error output.
- **F2 — Local-by-default enforcement + remote labeling:** reject or require explicit admin opt-in for non-loopback `comfyui_endpoint_url`/`endpointUrl`; surface a clear "remote" label in `list_media_models`/settings before prompts are sent. (Implements the proposed core principle below.)
- **F3 — Thread owner/session through the MCP generation path**, or document/guard MCP generation as single-user-only.

### Recommended hardening follow-ups
- **F4** — validate + URL-encode the provider `prompt_id` before path interpolation.
- **F5** — genericize the catch-all `str(e)` error string; keep full detail in logs only.
- **F8 (functional, deploy-risk, non-security):** the bundled workflow uses `%checkpoint%` but `do_generate_image` never passes a checkpoint, so `apply_workflow_params` leaves the literal `"%checkpoint%"` in `ckpt_name`. **Live generation against real ComfyUI will fail** until a checkpoint is configured/substituted. Flagging because live testing is imminent. Owner: Forge.
- **Doc hygiene:** `services/media/comfyui.py` module docstring still says "connection probe only (Slice 3) … Generation … intentionally NOT implemented here; they arrive in S4." This is now inaccurate (generation is implemented). Update to avoid misleading future reviewers.
- **Prompt logging:** consider gating `logger.info(... prompt ...)` behind a debug flag, since prompts may contain sensitive content (local logs only, but defense-in-depth).
- **Workflow substitution robustness:** consider asserting at load time that the bundled template contains the expected placeholders (a happy test exists; a startup guard would catch packaging drift).

---

## Proposed core principle (for project docs, per request)

> **Media providers must be local-by-default.** Any remote media provider must be
> explicitly configured by an admin and clearly labeled as remote before any
> prompt, file, or metadata is sent to it.

Recommend recording this in the project's architecture/principles doc and wiring
F2 to enforce it. (Captured here for handoff; not added to docs by Gatekeeper.)

---

## Verdict

**Conditional PASS.** No critical security bug; safe for **local/loopback** live
testing now. Address **F1** and **F2** (and **F3** for multi-user) before pointing
the provider at a remote endpoint or deploying to more than one user. F4/F5/F8 and
the doc fixes are recommended hardening. Handoff to **Forge/Infra** for remediation;
**Verifier** should add the F1 "endpoint URL absent from error output" assertion to
the verification set.
