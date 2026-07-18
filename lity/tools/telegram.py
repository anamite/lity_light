"""The Telegram kernel tool — thin dispatcher over app.telegram
(lity/modules/telegram.py). Sending is ON PURPOSE only: the kernel uses this
when the user asks for something on Telegram, never as a default channel."""

from . import INTERNAL, params, tool


@tool("telegram",
      "Send to the user's Telegram (module) — ONLY when the user asks for it or a "
      "situation clearly needs their phone (they're away, a file belongs on their "
      "phone). Normal replies never go through this tool. Actions: send (message "
      "text), send_file (workspace path + optional caption), status, setup (returns "
      "the SETUP MANUAL — when the user wants Telegram connected, read it and walk "
      "them through it one step at a time). Incoming Telegram messages already reach "
      "you automatically, tagged [via Telegram] — replies to those route back on "
      "their own; don't call this tool for them.",
      params({"action": {"type": "string",
                         "enum": ["send", "send_file", "status", "setup"]},
              "text": {"type": "string", "description": "for send: the message"},
              "path": {"type": "string", "description": "for send_file: workspace-relative path"},
              "caption": {"type": "string", "description": "for send_file: optional caption"}},
             required=["action"]), level=2)
async def telegram(ctx, args):
    t = ctx.app.telegram
    a = str(args.get("action") or "").lower()
    if a == "setup":
        return INTERNAL + t.setup_manual()
    if a == "status":
        return INTERNAL + t.status()
    if not t.configured:
        return INTERNAL + ("Telegram isn't connected yet. Offer to walk the user "
                           "through setup — call telegram(action='setup') for the "
                           "manual. " + t.status())
    try:
        if a == "send":
            text = str(args.get("text") or "").strip()
            if not text:
                return "Nothing to send — give the message text."
            await t.send_message(text)
            return "Sent to Telegram."
        if a == "send_file":
            return await t.send_file(args.get("path") or "", args.get("caption") or "")
    except ValueError as e:
        return str(e)
    return "Unknown action — use send, send_file, status or setup."
