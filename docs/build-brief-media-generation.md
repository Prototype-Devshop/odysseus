# Build Brief: Media Generation Provider Layer

## Project

Prototype fork of Odysseus.

## Goal

Add a media generation provider layer so Odysseus agents can generate images through configured local providers instead of guessing unavailable model names.

The first implementation should support image generation through ComfyUI. The system should be designed so video generation and additional providers can be added later, but video is not part of the first implementation slice.

## Background

In agent mode, Odysseus can expose a `generate_image`-style tool. However, the agent may attempt to use a model such as `Stable Diffusion` even when that model is not configured on any available endpoint.

This creates a confusing user experience:

1. User asks whether the agent can make images.
2. Agent says it can use an image generation tool.
3. Agent attempts to use an unavailable model.
4. The tool fails because no configured endpoint has that model.
5. The user is asked to retry or configure an endpoint, but there is no clear provider discovery, model registry, or degraded-state explanation.

The Prototype fork should make this behavior more reliable and useful.

## Problem

Odysseus needs a clear media-generation layer that knows:

* Which media providers are configured.
* Which media models or workflows are available.
* Which model should be used by default.
* What capabilities each model supports.
* How to report missing or unavailable providers.
* How to avoid hardcoding model names in agent behavior.

The agent should not guess `Stable Diffusion`, `Qwen-Image`, `FLUX`, or any other model unless that model is registered and enabled.

## Desired Outcome

A user should be able to configure a local ComfyUI endpoint and register or select an image workflow/model. Once configured, agent mode should be able to generate an image using the default available image model.

If no image model is configured, the agent should return a clear degraded-state message explaining what is missing and how to fix it.

## Initial User Experience

### Configured State

User asks:

```text
Can you make an image of a calm mountain lake at sunrise?
```

Expected behavior:

1. Agent checks available media models.
2. Agent selects the default enabled image model.
3. Agent calls image generation with the user prompt.
4. Backend sends the prompt to the configured provider.
5. Generated image is returned to the user and, where possible, saved into the workspace/files/assets system.
6. Metadata is stored with the generated asset.

### Unconfigured State

User asks:

```text
Can you make an image?
```

Expected behavior:

```text
Image generation is available as a tool, but no image model is currently configured.

Checked:
- ComfyUI: not configured or unavailable
- Diffusers: not configured
- Other image providers: not configured

Next steps:
1. Configure a ComfyUI endpoint.
2. Register or select an image workflow/model.
3. Run provider probe again.
```

## MVP Scope

### Include

* Media model registry for image models.
* Configurable ComfyUI provider support.
* ComfyUI endpoint probe.
* Ability to list enabled media models.
* Ability to select or resolve a default image model.
* Agent/tool path that uses configured model IDs instead of hardcoded model names.
* Clear degraded-state response when no image model is available.
* Basic image generation request flow through ComfyUI.
* Save generated image output into the existing Odysseus file/workspace/asset system if a suitable integration point exists.
* Store basic metadata for generated media.

### Exclude

* Video generation.
* Automatic model installation.
* Model downloads.
* Workflow marketplace.
* LoRA management.
* Advanced prompt editor.
* New visual design system.
* Broad CSS changes.
* Broad frontend refactors.
* Commercial license validation automation.
* Support for every possible image backend.
* Changes to unrelated Odysseus provider behavior.

## Preferred Architecture

Odysseus should remain the orchestration layer.

ComfyUI should remain the media runtime.

High-level flow:

```text
Odysseus Agent
  ↓
list_media_models
  ↓
generate_image(model_id, prompt, params)
  ↓
Media Provider Layer
  ↓
ComfyUI Provider
  ↓
ComfyUI API
  ↓
Generated Output
  ↓
Odysseus Workspace / Files / Assets
```

The provider layer should be modular so future providers can be added without rewriting agent behavior.

## Proposed Data Shape

```ts
type MediaKind = "image" | "video";

type MediaCapability =
  | "text-to-image"
  | "image-to-image"
  | "image-edit"
  | "text-to-video"
  | "image-to-video";

type MediaProviderType =
  | "comfyui"
  | "diffusers"
  | "wan"
  | "ltx"
  | "custom";

type MediaModel = {
  id: string;
  label: string;
  provider: MediaProviderType;
  kind: MediaKind;
  capabilities: MediaCapability[];
  endpointUrl: string;
  workflowPath?: string;
  enabled: boolean;
  isDefault?: boolean;
  notes?: string;
};
```

This is a proposed shape only. The final implementation should follow the conventions already present in the Odysseus codebase.

## Proposed Agent Tools

Add or adapt tools equivalent to:

```text
list_media_models
generate_image
```

### `list_media_models`

Returns configured and enabled media models with their capabilities.

Example response:

```json
{
  "models": [
    {
      "id": "qwen-image-comfy",
      "label": "Qwen-Image",
      "provider": "comfyui",
      "kind": "image",
      "capabilities": ["text-to-image", "image-edit"],
      "enabled": true,
      "isDefault": true
    }
  ]
}
```

### `generate_image`

Generates an image using a configured model ID.

Expected input:

```json
{
  "modelId": "qwen-image-comfy",
  "prompt": "A calm mountain lake at sunrise, soft light, peaceful mood",
  "width": 1024,
  "height": 1024,
  "seed": 12345
}
```

If `modelId` is omitted, the tool should use the default enabled image model.

If no default image model exists, the tool should return a clear degraded-state response.

## ComfyUI Provider Requirements

The first provider should support ComfyUI.

### Required

* Configurable endpoint URL.
* Default endpoint suggestion: `http://localhost:8188`.
* Connection probe.
* Clear unavailable-state error.
* Queue prompt/workflow request.
* Poll job status.
* Retrieve generated output.
* Return generated file path or asset reference.

### Preferred

* Support workflow JSON files or workflow templates.
* Support replacing prompt text inside a known workflow.
* Support common generation parameters where feasible:

  * prompt
  * negative prompt
  * width
  * height
  * seed
  * steps
  * guidance/cfg
* Keep provider code isolated from UI code.

## Metadata Requirements

Generated assets should store metadata where possible:

```json
{
  "assetType": "generated_image",
  "prompt": "A calm mountain lake at sunrise, soft light, peaceful mood",
  "modelId": "qwen-image-comfy",
  "modelLabel": "Qwen-Image",
  "provider": "comfyui",
  "workflow": "qwen-image-workflow",
  "seed": 12345,
  "width": 1024,
  "height": 1024,
  "createdAt": "ISO_TIMESTAMP"
}
```

If the current Odysseus file system does not support metadata cleanly, document the nearest existing storage pattern and propose a minimal approach.

## Error Handling

The system should avoid vague failures.

### No Media Models

Return:

```text
Image generation is available as a tool, but no image model is currently configured.
```

Include checked providers and next steps.

### Provider Unavailable

Return:

```text
ComfyUI is configured but unavailable at http://localhost:8188.
```

Include the connection error if available.

### Model Disabled

Return:

```text
The selected image model is disabled or unavailable.
```

### Workflow Missing

Return:

```text
The selected image workflow could not be found.
```

### Generation Failed

Return the relevant provider error and preserve logs where possible.

## Safety and Security Notes

Treat prompts, workflow files, fetched documents, notes, memories, and user-editable skills as untrusted input.

Avoid allowing generated workflow content to execute arbitrary code.

Do not expose local filesystem paths unnecessarily to the agent or end user.

Do not include secrets, tokens, API keys, private file contents, or private logs in tool output.

The agent should not follow instructions embedded inside user documents, workflow metadata, or generated image metadata that attempt to alter system behavior.

## Implementation Constraints

* Keep changes focused and reviewable.
* Avoid broad rewrites.
* Avoid formatting-only changes.
* Avoid moving many files.
* Avoid CSS changes in the first implementation.
* Avoid UI changes unless required for minimal configuration.
* Preserve existing Odysseus behavior unless the change is directly related to media generation.
* Prefer isolated modules for Prototype-specific behavior.
* Use narrow bridge points when integration with existing Odysseus systems is required.
* Match existing project conventions.

## Suggested Implementation Slices

### Slice 1: Repo Map and Plan

Planning only.

Identify current implementation points for:

* Agent mode.
* Tool registration.
* Model/provider configuration.
* Existing image generation tools, if any.
* Endpoint probing.
* File/workspace/asset storage.
* Settings/config persistence.
* Existing degraded-state reporting patterns.

Write:

* `artifacts/plan.md`
* `artifacts/task-list.md`
* `artifacts/open-questions.md`

Do not write application code in this slice.

### Slice 2: Media Model Registry

Add a small registry for media models.

Requirements:

* Register media model entries.
* List enabled media models.
* Resolve default image model.
* Return useful errors when no default exists.
* Add tests or a validation script if consistent with the repo.

### Slice 3: ComfyUI Provider Probe

Add a ComfyUI provider module.

Requirements:

* Accept endpoint URL.
* Probe availability.
* Return structured provider status.
* Surface clear connection failures.
* Do not generate images yet.

### Slice 4: Generate Image Flow

Wire `generate_image` to the registry and ComfyUI provider.

Requirements:

* Use configured model ID or default image model.
* Submit generation job to ComfyUI.
* Poll for completion.
* Retrieve output.
* Return result to the agent.

### Slice 5: Asset/File Persistence

Save generated output into the existing Odysseus storage system.

Requirements:

* Store generated image.
* Store basic metadata where possible.
* Return asset/file reference to the agent.

### Slice 6: Minimal Settings/UI, If Needed

Only after backend/tool flow works.

Requirements:

* Add or reuse existing settings patterns.
* Configure ComfyUI endpoint.
* Enable/disable model.
* Select default image model.
* Avoid introducing new visual styles.

## Verification

Run the smallest relevant checks for each slice.

Likely checks:

```bash
python -m py_compile app.py routes/*.py src/*.py
python -m pytest
```

For changed frontend JavaScript:

```bash
node --check static/js/<changed-file>.js
```

For Docker-sensitive changes:

```bash
docker compose config
docker compose up -d --build
docker compose logs --tail=120 odysseus
```

Manual test cases should include:

1. No media provider configured.
2. ComfyUI endpoint configured but offline.
3. ComfyUI endpoint online but no enabled image model.
4. Enabled default image model configured.
5. Successful image generation.
6. Failed generation with provider error.
7. Agent asks for image without naming a model.
8. Agent asks for an unavailable model.

## Success Criteria

* Agent no longer guesses unavailable image models.
* Configured media models can be listed.
* Missing-provider state is clear and actionable.
* ComfyUI endpoint can be probed.
* Default image model can be resolved.
* Image generation can route through the configured provider.
* Generated output can be returned to the user or saved into the workspace/file system.
* Basic metadata is preserved.
* Implementation remains modular enough to support future video generation.
* Existing Odysseus behavior remains stable.

## Future Extensions

These are intentionally out of scope for the MVP:

* Wan video support.
* LTX video support.
* Diffusers provider.
* Qwen-Image preset.
* FLUX preset.
* Workflow marketplace.
* Model download/install helpers.
* GPU capability detection.
* Progress UI for long-running video jobs.
* Queue management.
* Cancel/retry generation jobs.
* License tracking dashboard.
