# services/media/comfyui.py
"""ComfyUI media provider — connection probe only (Slice 3).

Isolated provider module for the media generation layer, modeled on the
existing single-responsibility service modules (``services/tts``,
``services/stt``). It is deliberately kept out of ``src/ai_interaction.py`` so
provider code stays separate from the agent/tool plumbing.

Scope of this slice (S3): answer three questions and nothing more —
  1. Can we reach ComfyUI?
  2. What did we check (which probe path)?
  3. What status should the agent / user see?

Generation (``POST /prompt``), polling (``GET /history/{id}``), and output
retrieval (``GET /view``) are intentionally NOT implemented here; they arrive
in S4.

Probe strategy (per resolved OQ-3):
  - ``GET /system_stats`` first (cheap, returns a small JSON dict).
  - If that fails with a non-auth / non-network response (an HTTP error or a
    malformed body), fall back to ``GET /object_info``.
  - Network errors (host down / connection refused / timeout) and auth errors
    (401/403) are terminal — no fallback, because a second path will not help.

TLS note: ComfyUI is a *local media runtime*, not an LLM provider. The
extra-CA-bundle override in ``src/tls_overrides.py`` is intentionally scoped
(and test-pinned) to LLM provider HTTP only, so it is NOT used here. Probes
use httpx's default verification.

The returned status dict is compatible with the shared degraded-state shape in
``src/media_registry.py`` (same ``ok`` / ``available`` / ``status`` /
``message`` / ``checked`` / ``next_steps`` / ``detail`` keys), with two
provider-specific extras: ``provider`` and ``endpoint`` (plus ``via`` naming
the probe path that answered).
"""

from __future__ import annotations

import copy
import json
import logging
import random
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

import httpx

from src import media_registry

logger = logging.getLogger(__name__)

PROVIDER_TYPE = "comfyui"

# Standard ComfyUI HTTP API paths used for probing (OQ-3).
PROBE_PRIMARY_PATH = "/system_stats"
PROBE_FALLBACK_PATH = "/object_info"

# Generation API paths (OQ-3): queue a workflow, poll its history, fetch output.
QUEUE_PATH = "/prompt"
HISTORY_PATH = "/history"
VIEW_PATH = "/view"

# Bounded per-request timeout (seconds). Worst case is two sequential probes
# (primary + fallback), i.e. ~2x this — still well under interactive limits.
DEFAULT_PROBE_TIMEOUT = 5.0

# Per-HTTP-call timeout for generation requests (queue / history / view).
REQUEST_TIMEOUT = 30.0

# Bounded polling for a generation job (OQ-8): overall wall-clock budget and
# the interval between /history polls.
DEFAULT_GENERATE_TIMEOUT = 120.0
POLL_INTERVAL = 1.5

# Workflow substitution placeholders. The bundled template carries these exact
# tokens; substitution only replaces input values that equal one of them, so a
# malicious/edited workflow cannot smuggle behavior through other fields.
PH_PROMPT = "%prompt%"
PH_NEGATIVE = "%negative_prompt%"
PH_SEED = "%seed%"
PH_WIDTH = "%width%"
PH_HEIGHT = "%height%"
PH_CHECKPOINT = "%checkpoint%"

_DEFAULT_WORKFLOW_PATH = Path(__file__).resolve().parent / "workflows" / "text_to_image.json"

# Suggested default endpoint surfaced in guidance (mirrors media_registry).
SUGGESTED_ENDPOINT = media_registry.SUGGESTED_COMFYUI_ENDPOINT


def _safe_progress(progress_cb: Optional[Callable[[str], Any]], message: str) -> None:
    """Invoke a (sync) progress callback, never letting it break generation."""
    if progress_cb is None:
        return
    try:
        progress_cb(message)
    except Exception:  # pragma: no cover - progress is best-effort
        pass


def apply_workflow_params(
    workflow: Dict[str, Any],
    *,
    prompt: str,
    seed: int,
    width: int,
    height: int,
    negative_prompt: str = "",
    checkpoint: Optional[str] = None,
) -> Dict[str, Any]:
    """Return a copy of ``workflow`` with known placeholder tokens substituted.

    SECURITY: the workflow is treated as untrusted data. We deep-copy it and
    replace ONLY node-``inputs`` values that exactly equal one of the known
    placeholder tokens. No node metadata is read or executed, and no other
    field is touched — so an edited/hostile workflow cannot inject behavior
    via this path.
    """
    subs: Dict[str, Any] = {
        PH_PROMPT: prompt,
        PH_NEGATIVE: negative_prompt,
        PH_SEED: int(seed),
        PH_WIDTH: int(width),
        PH_HEIGHT: int(height),
    }
    if checkpoint:
        subs[PH_CHECKPOINT] = checkpoint

    wf = copy.deepcopy(workflow)
    for node in wf.values():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for key, value in list(inputs.items()):
            if isinstance(value, str) and value in subs:
                inputs[key] = subs[value]
    return wf


def _first_image_output(history_entry: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Extract the first produced image reference from a ComfyUI history entry.

    Returns ``{filename, subfolder, type}`` suitable as /view query params, or
    None. The filename is only forwarded back to the same ComfyUI /view
    endpoint — it is never used to write a local path here.
    """
    if not isinstance(history_entry, dict):
        return None
    outputs = history_entry.get("outputs")
    if not isinstance(outputs, dict):
        return None
    for node in outputs.values():
        if not isinstance(node, dict):
            continue
        images = node.get("images")
        if isinstance(images, list) and images:
            first = images[0]
            if isinstance(first, dict) and first.get("filename"):
                return {
                    "filename": first.get("filename"),
                    "subfolder": first.get("subfolder", ""),
                    "type": first.get("type", "output"),
                }
    return None


def _load_default_workflow() -> Optional[Dict[str, Any]]:
    """Load the bundled text-to-image workflow template as plain data."""
    try:
        with open(_DEFAULT_WORKFLOW_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError) as e:  # pragma: no cover - packaging issue
        logger.warning("comfyui: could not load bundled workflow: %s", e)
        return None


class ComfyUIProvider:
    """Thin client around a single ComfyUI endpoint. S3 exposes ``probe()``."""

    provider_type = PROVIDER_TYPE

    def __init__(
        self,
        endpoint_url: Optional[str] = None,
        timeout: float = DEFAULT_PROBE_TIMEOUT,
    ):
        self.endpoint_url = (endpoint_url or "").strip()
        self.timeout = timeout

    @classmethod
    def from_settings(
        cls,
        settings: Optional[Dict[str, Any]] = None,
        timeout: float = DEFAULT_PROBE_TIMEOUT,
    ) -> "ComfyUIProvider":
        """Build a provider using ``comfyui_endpoint_url`` from settings.

        Tests (and callers) may pass an explicit ``settings`` dict to stay
        offline; otherwise the global settings store is read.
        """
        if settings is None:
            try:
                from src.settings import load_settings
                settings = load_settings()
            except Exception:  # pragma: no cover - settings unavailable at boot
                settings = {}
        url = settings.get("comfyui_endpoint_url") or ""
        return cls(endpoint_url=url, timeout=timeout)

    # ── HTTP ──

    def _probe_path(self, url: str) -> Tuple[str, Optional[int], Any]:
        """GET ``url`` once and classify the outcome.

        Returns ``(kind, status_code, extra)`` where ``kind`` is one of:
          - ``"online"``        extra = parsed JSON dict
          - ``"network_error"`` extra = error string (host unreachable/timeout)
          - ``"auth_error"``    status_code = 401/403
          - ``"http_error"``    status_code = the non-2xx code
          - ``"malformed"``     body was not valid JSON / not a dict
        """
        try:
            resp = httpx.get(url, timeout=self.timeout)
        except (httpx.RequestError, OSError) as e:
            return ("network_error", None, str(e))

        code = getattr(resp, "status_code", None)
        if code in (401, 403):
            return ("auth_error", code, None)
        if isinstance(code, int) and not (200 <= code < 300):
            return ("http_error", code, None)

        try:
            data = resp.json()
        except Exception:
            return ("malformed", code, None)
        if not isinstance(data, dict):
            return ("malformed", code, None)
        return ("online", code, data)

    # ── Probe ──

    def probe(self) -> Dict[str, Any]:
        """Probe the ComfyUI endpoint and return a structured status dict."""
        endpoint = self.endpoint_url
        if not endpoint:
            return self._not_configured()

        base = endpoint.rstrip("/")

        kind, code, extra = self._probe_path(base + PROBE_PRIMARY_PATH)
        if kind == "online":
            return self._online(via=PROBE_PRIMARY_PATH, data=extra)
        if kind == "network_error":
            return self._unreachable(detail=extra)
        if kind == "auth_error":
            return self._auth_error(code)

        # Non-auth / non-network failure on the primary path → try fallback.
        logger.info(
            "comfyui: %s probe failed (%s); trying %s",
            PROBE_PRIMARY_PATH, kind, PROBE_FALLBACK_PATH,
        )
        kind2, code2, extra2 = self._probe_path(base + PROBE_FALLBACK_PATH)
        if kind2 == "online":
            return self._online(via=PROBE_FALLBACK_PATH, data=extra2)
        if kind2 == "network_error":
            return self._unreachable(detail=extra2)
        if kind2 == "auth_error":
            return self._auth_error(code2)

        last_code = code2 if code2 is not None else code
        return self._unavailable(last_code=last_code)

    # ── Generation (queue → poll → retrieve) ──

    def generate(
        self,
        *,
        prompt: str,
        width: int = 1024,
        height: int = 1024,
        seed: Optional[int] = None,
        negative_prompt: str = "",
        checkpoint: Optional[str] = None,
        workflow: Optional[Dict[str, Any]] = None,
        progress_cb: Optional[Callable[[str], Any]] = None,
        timeout: float = DEFAULT_GENERATE_TIMEOUT,
        poll_interval: float = POLL_INTERVAL,
    ) -> Dict[str, Any]:
        """Run a text-to-image job and return the raw image bytes.

        Synchronous (uses httpx.post/get + time.sleep); callers on the event
        loop should invoke via ``asyncio.to_thread``. Returns either a success
        dict ``{"ok": True, "image_bytes": ..., "content_type": ..., ...}`` or a
        degraded/error dict in the shared shape (``ok=False``). Persistence and
        gallery/metadata are the caller's responsibility — this stays isolated
        from the DB / filesystem.
        """
        endpoint = self.endpoint_url
        if not endpoint:
            return self._not_configured()
        base = endpoint.rstrip("/")

        if seed is None:
            seed = random.randint(0, 2_147_483_647)

        wf = workflow if workflow is not None else _load_default_workflow()
        if not isinstance(wf, dict) or not wf:
            return self._workflow_missing()
        wf = apply_workflow_params(
            wf, prompt=prompt, seed=seed, width=width, height=height,
            negative_prompt=negative_prompt, checkpoint=checkpoint,
        )

        # 1) Queue the workflow.
        _safe_progress(progress_cb, "Submitting image job to ComfyUI…")
        try:
            resp = httpx.post(base + QUEUE_PATH, json={"prompt": wf}, timeout=REQUEST_TIMEOUT)
        except (httpx.RequestError, OSError) as e:
            return self._unreachable(detail=str(e))
        code = getattr(resp, "status_code", None)
        if code in (401, 403):
            return self._auth_error(code)
        if isinstance(code, int) and not (200 <= code < 300):
            return self._generation_failed(detail=f"queue request returned HTTP {code}")
        try:
            qdata = resp.json()
        except Exception:
            return self._generation_failed(detail="queue response was not valid JSON")
        prompt_id = qdata.get("prompt_id") if isinstance(qdata, dict) else None
        if not prompt_id:
            return self._generation_failed(detail="ComfyUI did not return a prompt_id")

        # 2) Poll history until the job appears (bounded wall-clock budget).
        deadline = time.monotonic() + timeout
        history_entry = None
        while time.monotonic() < deadline:
            _safe_progress(progress_cb, "Waiting for ComfyUI to finish…")
            try:
                hresp = httpx.get(f"{base}{HISTORY_PATH}/{prompt_id}", timeout=REQUEST_TIMEOUT)
            except (httpx.RequestError, OSError) as e:
                return self._unreachable(detail=str(e))
            hcode = getattr(hresp, "status_code", None)
            if isinstance(hcode, int) and 200 <= hcode < 300:
                try:
                    hdata = hresp.json()
                except Exception:
                    hdata = None
                entry = hdata.get(prompt_id) if isinstance(hdata, dict) else None
                if entry:
                    history_entry = entry
                    break
            time.sleep(poll_interval)

        if history_entry is None:
            return self._timeout(timeout)

        # 3) Locate and retrieve the produced image.
        image_ref = _first_image_output(history_entry)
        if not image_ref:
            return self._generation_failed(detail="workflow produced no image output")

        _safe_progress(progress_cb, "Retrieving generated image…")
        try:
            vresp = httpx.get(base + VIEW_PATH, params=image_ref, timeout=REQUEST_TIMEOUT)
        except (httpx.RequestError, OSError) as e:
            return self._unreachable(detail=str(e))
        vcode = getattr(vresp, "status_code", None)
        if not (isinstance(vcode, int) and 200 <= vcode < 300):
            return self._generation_failed(detail=f"image retrieval returned HTTP {vcode}")
        image_bytes = getattr(vresp, "content", b"") or b""
        if not image_bytes:
            return self._generation_failed(detail="ComfyUI returned an empty image")

        content_type = "image/png"
        headers = getattr(vresp, "headers", None)
        if headers is not None:
            try:
                content_type = headers.get("content-type") or content_type
            except Exception:
                pass

        return {
            "ok": True,
            "available": True,
            "status": "generated",
            "provider": PROVIDER_TYPE,
            "endpoint": endpoint,
            "prompt_id": prompt_id,
            "seed": int(seed),
            "width": int(width),
            "height": int(height),
            "image_bytes": image_bytes,
            "content_type": content_type,
        }

    # ── Result builders (shape-compatible with media_registry.degraded_state) ──

    def _result(
        self,
        status: str,
        *,
        ok: bool,
        message: str,
        checked_status: str,
        next_steps: Optional[list] = None,
        detail: Optional[str] = None,
        via: Optional[str] = None,
    ) -> Dict[str, Any]:
        if ok:
            base: Dict[str, Any] = {
                "ok": True,
                "available": True,
                "status": status,
                "kind": "image",
                "message": message,
                "checked": [{"provider": PROVIDER_TYPE, "status": checked_status}],
                "next_steps": next_steps or [],
                "detail": detail,
            }
        else:
            base = media_registry.degraded_state(
                status,
                kind="image",
                message=message,
                checked=[{"provider": PROVIDER_TYPE, "status": checked_status}],
                next_steps=next_steps or [],
                detail=detail,
            )
        base["provider"] = PROVIDER_TYPE
        base["endpoint"] = self.endpoint_url
        base["via"] = via
        return base

    def _online(self, *, via: str, data: Dict[str, Any]) -> Dict[str, Any]:
        version = None
        try:
            version = (data.get("system") or {}).get("comfyui_version")
        except Exception:
            version = None
        detail = f"Reachable via {via}." + (f" ComfyUI version {version}." if version else "")
        return self._result(
            "online",
            ok=True,
            message=f"ComfyUI is reachable at {self.endpoint_url}.",
            checked_status=f"online (via {via})",
            detail=detail,
            via=via,
        )

    def _not_configured(self) -> Dict[str, Any]:
        return self._result(
            "not_configured",
            ok=False,
            message="ComfyUI endpoint is not configured.",
            checked_status="not configured",
            next_steps=[
                f"Set the ComfyUI endpoint URL (suggested: {SUGGESTED_ENDPOINT}).",
                "Run the provider probe again.",
            ],
        )

    def _unreachable(self, *, detail: Optional[str]) -> Dict[str, Any]:
        return self._result(
            "unreachable",
            ok=False,
            message=f"ComfyUI is configured but unavailable at {self.endpoint_url}.",
            checked_status="unreachable",
            next_steps=[
                f"Start ComfyUI and ensure it is listening at {self.endpoint_url}.",
                "Verify the endpoint URL (comfyui_endpoint_url) in settings.",
                "Run the provider probe again.",
            ],
            detail=detail or None,
        )

    def _auth_error(self, code: Optional[int]) -> Dict[str, Any]:
        return self._result(
            "auth_error",
            ok=False,
            message=f"ComfyUI at {self.endpoint_url} rejected the request (HTTP {code}).",
            checked_status=f"auth error (HTTP {code})",
            next_steps=[
                "Check whether the ComfyUI endpoint requires authentication or is behind a proxy.",
                "Verify the endpoint URL points directly at ComfyUI.",
            ],
            detail=f"HTTP {code} from probe.",
        )

    def _unavailable(self, *, last_code: Optional[int]) -> Dict[str, Any]:
        if last_code is not None:
            detail = (
                f"Neither {PROBE_PRIMARY_PATH} nor {PROBE_FALLBACK_PATH} returned a "
                f"valid response (last HTTP status: {last_code})."
            )
        else:
            detail = (
                f"Neither {PROBE_PRIMARY_PATH} nor {PROBE_FALLBACK_PATH} returned a "
                "valid response."
            )
        return self._result(
            "unavailable",
            ok=False,
            message=f"ComfyUI at {self.endpoint_url} responded but the probe did not succeed.",
            checked_status="unavailable",
            next_steps=[
                f"Confirm the endpoint URL points at a ComfyUI server (suggested: {SUGGESTED_ENDPOINT}).",
                "Check the ComfyUI version exposes /system_stats or /object_info.",
                "Run the provider probe again.",
            ],
            detail=detail,
        )

    def _generation_failed(self, *, detail: Optional[str]) -> Dict[str, Any]:
        return self._result(
            "generation_failed",
            ok=False,
            message=f"ComfyUI could not complete the image generation at {self.endpoint_url}.",
            checked_status="generation failed",
            next_steps=[
                "Check the ComfyUI server logs for the failed prompt.",
                "Confirm the configured workflow/checkpoint is installed in ComfyUI.",
                "Try again.",
            ],
            detail=detail or None,
        )

    def _workflow_missing(self) -> Dict[str, Any]:
        return self._result(
            "workflow_missing",
            ok=False,
            message="No usable ComfyUI workflow template is available.",
            checked_status="workflow missing",
            next_steps=[
                "Ensure the bundled text-to-image workflow template is present.",
            ],
        )

    def _timeout(self, timeout: float) -> Dict[str, Any]:
        return self._result(
            "timeout",
            ok=False,
            message=(
                f"ComfyUI did not finish the image within {int(timeout)}s at {self.endpoint_url}."
            ),
            checked_status="timeout",
            next_steps=[
                "The ComfyUI server may be overloaded or the workflow is slow.",
                "Try again, or reduce image size.",
            ],
            detail=f"Polling exceeded the {int(timeout)}s budget.",
        )


def generate(
    endpoint_url: Optional[str] = None,
    *,
    settings: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Convenience: build a provider and run a generation job."""
    if endpoint_url is not None:
        provider = ComfyUIProvider(endpoint_url=endpoint_url)
    else:
        provider = ComfyUIProvider.from_settings(settings=settings)
    return provider.generate(**kwargs)


def probe(
    endpoint_url: Optional[str] = None,
    *,
    settings: Optional[Dict[str, Any]] = None,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
) -> Dict[str, Any]:
    """Convenience: build a provider and probe it.

    ``endpoint_url`` takes precedence; otherwise the URL is read from settings
    (``comfyui_endpoint_url``).
    """
    if endpoint_url is not None:
        provider = ComfyUIProvider(endpoint_url=endpoint_url, timeout=timeout)
    else:
        provider = ComfyUIProvider.from_settings(settings=settings, timeout=timeout)
    return provider.probe()
