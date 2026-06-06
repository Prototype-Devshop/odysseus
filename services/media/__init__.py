# services/media/__init__.py
"""Media generation provider modules (image MVP; video later).

Isolated provider clients for the media generation layer. S3 ships the ComfyUI
connection probe only; generation arrives in S4.
"""

from .comfyui import ComfyUIProvider, probe

__all__ = ["ComfyUIProvider", "probe"]
