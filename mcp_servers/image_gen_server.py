"""
image_gen_server.py

MCP server exposing image generation through the canonical Odysseus
image-generation path. The canonical path supports existing OpenAI-compatible
image providers and media-registry providers such as ComfyUI.
"""

import asyncio
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

server = Server("image_gen")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="generate_image",
            description="Generate an image using an image-capable model (e.g. gpt-image-1)",
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Image description prompt"},
                    "model": {"type": "string", "description": "Model name (auto-detects if omitted)"},
                    "size": {"type": "string", "description": "Image size (default 1024x1024)"},
                    "quality": {"type": "string", "description": "Quality: low, medium, high, auto (default medium)"},
                },
                "required": ["prompt"],
            },
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name != "generate_image":
        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    prompt = arguments.get("prompt", "")
    model_spec = arguments.get("model", "")
    size = arguments.get("size", "1024x1024")
    quality = arguments.get("quality", "medium")

    if not prompt:
        return [TextContent(type="text", text="Error: Image prompt is required")]

    try:
        from src.settings import get_setting
        from src.ai_interaction import do_generate_image

        if not get_setting("image_gen_enabled", True):
            return [TextContent(type="text", text="Error: Image generation is disabled by the administrator.")]

        # Delegate to the canonical owner-aware path (OQ-5). This keeps the
        # OpenAI-compatible behavior and adds media-registry / ComfyUI routing
        # in one place rather than duplicating it here.
        #
        # OWNER/SESSION LIMITATION (Gatekeeper F3): this MCP server runs as a
        # separate stdio subprocess and receives no auth/session context, so it
        # cannot attribute generations to an owner. Images created via this path
        # are therefore saved with owner/session unset (acceptable for the
        # single-user local default; revisit before multi-user deployment). The
        # direct chat path (routes/chat_routes.py) DOES pass the real owner and
        # remains owner/session-scoped — do not weaken it.
        content = "\n".join([prompt, model_spec or "", size or "", quality or ""])
        result = await do_generate_image(content, owner=None)

        if isinstance(result, dict) and result.get("error"):
            return [TextContent(type="text", text=f"Error: {result['error']}")]

        image_url = result.get("image_url") if isinstance(result, dict) else None
        if image_url:
            text = (
                f"Generated image for: {prompt[:100]}\n"
                f"Direct link: {image_url}\n"
                f"model: {result.get('image_model', model_spec)}\n"
                f"size: {result.get('image_size', size)}"
            )
            return [TextContent(type="text", text=text)]

        # No image_url and no error → degraded-state informational message.
        msg = result.get("results") if isinstance(result, dict) else None
        return [TextContent(type="text", text=msg or "Error: Unexpected image generation response")]

    except Exception as e:
        return [TextContent(type="text", text=f"Error: {e}")]


async def run():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run())
