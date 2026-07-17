"""Quick local tools — thin dispatchers over app.quick (lity/quick.py).
Four tools instead of fifteen keeps the per-turn schema budget small; all
validation and the actual work live in the Quick service."""

from . import params, tool


@tool("timer",
      "Local timers and alarms that RING audibly on the server. Actions: set (needs "
      "duration like '30s'/'5m'/'1h30m'), alarm (needs time 'HH:MM' local or "
      "'YYYY-MM-DD HH:MM'), list, cancel (needs id), stop_ringing (silence whatever is "
      "ringing — use when the user says stop/stop it and the task board shows RINGING).",
      params({"action": {"type": "string",
                         "enum": ["set", "alarm", "list", "cancel", "stop_ringing"]},
              "duration": {"type": "string", "description": "for set, e.g. '30s', '5m', '1h30m'"},
              "time": {"type": "string", "description": "for alarm, local 'HH:MM' or 'YYYY-MM-DD HH:MM'"},
              "label": {"type": "string", "description": "short name, e.g. 'Egg boil'"},
              "id": {"type": "integer", "description": "for cancel"}},
             required=["action"]), level=1, direct=True)
async def timer(ctx, args):
    q = ctx.app.quick
    a = str(args.get("action") or "").lower()
    try:
        if a in ("set", "set_timer", "timer"):
            return await q.set_timer(args.get("duration") or args.get("time") or "",
                                     args.get("label"), ctx.user_thread_id)
        if a in ("alarm", "set_alarm"):
            return await q.set_alarm(args.get("time") or args.get("duration") or "",
                                     args.get("label"), ctx.user_thread_id)
        if a == "list":
            return await q.timers_text()
        if a == "cancel":
            if not args.get("id"):
                return "Cancel needs the id — current state: " + await q.timers_text()
            return await q.cancel_timer(int(args["id"]))
        if a in ("stop_ringing", "stop"):
            return await q.stop_ringing()
    except ValueError as e:
        return str(e)
    return "Unknown action — use set, alarm, list, cancel or stop_ringing."


@tool("note",
      "Local quick notes (title + content, stored in Lity's own database). Actions: "
      "add, list (optional query filters), get (by id or title), delete (by id).",
      params({"action": {"type": "string", "enum": ["add", "list", "get", "delete"]},
              "title": {"type": "string"},
              "content": {"type": "string"},
              "id": {"type": "integer"},
              "query": {"type": "string", "description": "for list: filter text"}},
             required=["action"]), level=1, direct=True)
async def note(ctx, args):
    q = ctx.app.quick
    a = str(args.get("action") or "").lower()
    if a == "add":
        return await q.note_add(args.get("title"), args.get("content"))
    if a == "list":
        return await q.note_list(args.get("query"))
    if a == "get":
        return await q.note_get(args.get("id"), args.get("title"))
    if a == "delete":
        return await q.note_delete(args.get("id"))
    return "Unknown action — use add, list, get or delete."


@tool("shopping",
      "Local shopping lists. Actions: add (items to a list; creates the list if new), "
      "remove, check (mark bought), view, lists (all lists), create, delete_list. "
      "'list' is the list's title or id; defaults to 'Shopping'.",
      params({"action": {"type": "string",
                         "enum": ["add", "remove", "check", "view", "lists",
                                  "create", "delete_list"]},
              "list": {"type": "string", "description": "list title or id"},
              "items": {"type": "array", "items": {"type": "string"}}},
             required=["action"]), level=1, direct=True)
async def shopping(ctx, args):
    q = ctx.app.quick
    a = str(args.get("action") or "").lower()
    ref, items = args.get("list"), args.get("items")
    try:
        if a == "add":
            return await q.shop_add(ref, items)
        if a == "remove":
            return await q.shop_remove(ref, items)
        if a == "check":
            return await q.shop_check(ref, items)
        if a == "view":
            return await q.shop_view(ref)
        if a == "lists":
            return await q.shop_lists()
        if a == "create":
            return await q.shop_create(ref)
        if a == "delete_list":
            return await q.shop_delete(ref)
    except ValueError as e:
        return str(e)
    return "Unknown action — use add, remove, check, view, lists, create or delete_list."


@tool("weather",
      "Current weather + today/tomorrow outlook (no API key, Open-Meteo). "
      "Omit city to use the configured default.",
      params({"city": {"type": "string", "description": "city name; omit for default"}},
             required=[]), level=1, direct=True)
async def weather(ctx, args):
    return await ctx.app.quick.weather(args.get("city"))
