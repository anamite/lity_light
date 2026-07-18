"""Kernel tools for the environment layer (lity/modules/env.py) and the
long-horizon goal board. Small on purpose: seeing is level 0, switching a
device is level 3 (approval-gated by default — 'always' tames it)."""

from ..context import user_tz
from ..sched.crons import next_run
from . import params, tool


@tool("environment",
      "Live snapshot of the physical environment: this machine's health (disk, memory, "
      "CPU temp) plus every smart-home device and sensor a driver watches (Home Assistant "
      "/ Zigbee mesh). Use for 'is the light on', 'how warm is the room', 'how is the Pi "
      "doing', or before acting on a device.",
      params({}, required=[]), level=0, direct=True)
async def environment(ctx, args):
    return ctx.app.env.snapshot_text(2000)


@tool("env_act",
      "Act on a smart-home device: on / off / toggle for any entity from the "
      "`environment` snapshot (lights, switches, plugs...). Optional service data, "
      'e.g. {"brightness_pct": 40} for a light.',
      params({"target": {"type": "string", "description": "entity id, e.g. light.desk"},
              "action": {"type": "string", "enum": ["on", "off", "toggle"]},
              "data": {"type": "object", "description": "optional extra service data"}},
             required=["target", "action"]), level=3)
async def env_act(ctx, args):
    return await ctx.app.env.act(args["target"], args["action"], args.get("data"))


@tool("goal",
      "Your long-horizon goal board — things you pursue for the user across days: "
      "follow-ups, commitments, ongoing projects ('check how the job application went', "
      "'water-sensor watch until the leak is fixed'). Actions: add(title, detail?, "
      "review?), update(goal_id, title/detail/review), done(goal_id), drop(goal_id), "
      "list. 'review' = when to next look at it: in:2h / in:3d / daily:09:00 (local time); "
      "when due, a system event wakes you to act on it. Active goals are always in your "
      "context. Be proactive: when the user mentions something worth following up on, "
      "add a goal instead of hoping to remember.",
      params({"action": {"type": "string", "enum": ["add", "update", "done", "drop", "list"]},
              "goal_id": {"type": "integer"},
              "title": {"type": "string"},
              "detail": {"type": "string"},
              "review": {"type": "string",
                         "description": "next review: in:2h / in:3d / daily:09:00 (local)"}},
             required=["action"]), level=2, direct=True)
async def goal(ctx, args):
    db, a = ctx.app.db, args.get("action")

    if a == "list":
        rows = await db.fetchall(
            "SELECT * FROM goals WHERE status='active' ORDER BY id")
        if not rows:
            return "No active goals."
        return "\n".join(
            f"#{r['id']} {r['title']}"
            + (f" — {r['detail'][:100]}" if r["detail"] else "")
            + (f" · review {r['review_at']} UTC" if r["review_at"] else " · no review set")
            for r in rows)

    if a == "add":
        title = (args.get("title") or "").strip()
        if not title:
            return "A goal needs a title."
        review = None
        if args.get("review"):
            try:
                review = next_run(args["review"], tz=user_tz(ctx.app))
            except ValueError as e:
                return f"Bad review spec: {e}"
        gid = await db.execute(
            "INSERT INTO goals(title, detail, review_at) VALUES (?,?,?)",
            (title, (args.get("detail") or "").strip(), review))
        return f"Goal #{gid} added" + (f", first review {review} UTC." if review else ".")

    gid = args.get("goal_id")
    row = await db.fetchone("SELECT * FROM goals WHERE id=?", (gid,)) if gid else None
    if not row:
        return "No such goal — goal(action='list') shows the board."

    if a in ("done", "drop"):
        await db.execute(
            "UPDATE goals SET status=?, updated_at=datetime('now') WHERE id=?",
            ("done" if a == "done" else "dropped", gid))
        return f"Goal #{gid} marked {'done' if a == 'done' else 'dropped'}."

    if a == "update":
        sets, vals = [], []
        for field in ("title", "detail"):
            if args.get(field) is not None:
                sets.append(f"{field}=?")
                vals.append(str(args[field]).strip())
        if args.get("review"):
            try:
                sets.append("review_at=?")
                vals.append(next_run(args["review"], tz=user_tz(ctx.app)))
            except ValueError as e:
                return f"Bad review spec: {e}"
        if not sets:
            return "Nothing to update — pass title, detail and/or review."
        await db.execute(
            f"UPDATE goals SET {', '.join(sets)}, updated_at=datetime('now') WHERE id=?",
            (*vals, gid))
        return f"Goal #{gid} updated."

    return f"Unknown action '{a}'."
