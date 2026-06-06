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

import logging
from typing import Any, Dict, Optional, Tuple

import httpx

from src import media_registry

logger = logging.getLogger(__name__)

PROVIDER_TYPE = "comfyui"

# Standard ComfyUI HTTP API paths used for probing (OQ-3).
PROBE_PRIMARY_PATH = "/system_stats"
PROBE_FALLBACK_PATH = "/object_info"

# Bounded per-request timeout (seconds). Worst case is two sequential probes
# (primary + fallback), i.e. ~2x this — still well under interactive limits.
DEFAULT_PROBE_TIMEOUT = 5.0

# Suggested default endpoint surfaced in guidance (mirrors media_registry).
SUGGESTED_ENDPOINT = media_registry.SUGGESTED_COMFYUI_ENDPOINT


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
