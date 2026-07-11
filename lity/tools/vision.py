"""Vision: let any agent look at an image file with the configured vision model."""

import base64
import mimetypes

from . import params, tool


@tool("analyze_image",
      "Look at an image file in the workspace with a vision model and answer a question about it "
      "(contents, text in the image, layout, anything visual).",
      params({"path": {"type": "string", "description": "image path relative to the workspace"},
              "question": {"type": "string", "description": "what to find out; default: describe it"}},
             required=["path"]), level=1, direct=True)
async def analyze_image(ctx, args):
    ws = ctx.app.cfg.workspace
    p = (ws / args["path"]).resolve()
    if not p.is_relative_to(ws) or not p.is_file():
        return f"Error: no such workspace image '{args['path']}'."
    mime = mimetypes.guess_type(p.name)[0] or ""
    if not mime.startswith("image/"):
        return f"Error: '{args['path']}' is not an image ({mime or 'unknown type'})."
    if p.stat().st_size > 6_000_000:
        return "Error: image larger than 6 MB."
    data = base64.b64encode(p.read_bytes()).decode()
    model = ctx.app.cfg.get_path("models.vision") or ctx.app.cfg.get_path("models.main")
    question = args.get("question") or "Describe this image precisely but briefly."
    msg, _ = await ctx.app.llm.chat(model, [{
        "role": "user",
        "content": [{"type": "text", "text": question},
                    {"type": "image_url",
                     "image_url": {"url": f"data:{mime};base64,{data}"}}],
    }], max_tokens=800)
    return (msg.get("content") or "").strip() or "(the vision model returned nothing)"
