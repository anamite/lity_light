"""The Google Calendar kernel tool — thin dispatcher over app.gcal
(lity/modules/gcal.py), same pattern as the quick tools."""

from . import INTERNAL, params, tool


@tool("calendar",
      "Google Calendar (gcal module) — instant, never delegate calendar work. Actions: "
      "agenda (a day's events; day = 'today'/'tomorrow'/'yesterday'/'YYYY-MM-DD'), "
      "add (title + start as 'YYYY-MM-DD HH:MM', 'HH:MM' for today, or 'YYYY-MM-DD' for "
      "all-day; optional duration like '45m'/'2h' (default 1h), details, location), "
      "update (day + event + any fields to change), delete (day + event), status, "
      "setup (returns the step-by-step SETUP MANUAL — when the user wants Google "
      "Calendar connected, read it and walk them through it one step at a time). "
      "'event' = the number or title shown by agenda.",
      params({"action": {"type": "string",
                         "enum": ["agenda", "add", "update", "delete", "status", "setup"]},
              "day": {"type": "string",
                      "description": "today (default) / tomorrow / yesterday / YYYY-MM-DD"},
              "event": {"type": "string",
                        "description": "for update/delete: number or title from agenda"},
              "title": {"type": "string"},
              "start": {"type": "string",
                        "description": "'YYYY-MM-DD HH:MM', 'HH:MM' (today) or 'YYYY-MM-DD' (all-day)"},
              "duration": {"type": "string", "description": "e.g. '30m', '2h' — default 1h"},
              "details": {"type": "string"},
              "location": {"type": "string"}},
             required=["action"]), level=2, direct=True)
async def calendar(ctx, args):
    g = ctx.app.gcal
    a = str(args.get("action") or "").lower()
    # setup/status are INTERNAL reference docs — the marker makes the kernel
    # refuse raw delivery; the model must relay them in its own words.
    if a == "setup":
        return INTERNAL + g.setup_manual()
    if a == "status":
        return INTERNAL + g.status()
    if not g.configured:
        return INTERNAL + ("Google Calendar isn't connected yet. Offer to walk the user "
                           "through setup — call calendar(action='setup') for the manual. "
                           + g.status())
    try:
        if a == "agenda":
            return await g.agenda(args.get("day"))
        if a == "add":
            return await g.add_event(args.get("title"), args.get("start"),
                                     args.get("duration"), args.get("details"),
                                     args.get("location"))
        if a == "update":
            return await g.update_event(args.get("day"), args.get("event"),
                                        args.get("title"), args.get("start"),
                                        args.get("duration"), args.get("details"),
                                        args.get("location"))
        if a == "delete":
            return await g.delete_event(args.get("day"), args.get("event"))
    except ValueError as e:
        return str(e)
    return "Unknown action — use agenda, add, update, delete, status or setup."
