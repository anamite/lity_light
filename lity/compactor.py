"""Context compaction: folds older messages into one updatable summary per
thread. The summary is REWRITTEN each pass, so stale lines ('timer running')
get corrected ('timer fired at 14:32') instead of accumulating."""

SUMMARIZE_SYSTEM = """You maintain the rolling summary of a conversation for an AI agent.
Merge the existing summary with the new messages into ONE updated summary (max 200 words).
Keep: user goals, decisions, open tasks and their outcomes, important facts. Update anything
that changed (a task that was 'running' and later finished is recorded as finished).
Drop: small talk, resolved back-and-forth. Output only the summary text."""


class Compactor:
    def __init__(self, app):
        self.app = app

    async def maybe_compact(self, thread_id: int):
        cfg = self.app.cfg
        trigger = int(cfg.get_path("kernel.compact_after_messages", 26))
        keep = int(cfg.get_path("kernel.keep_recent", 10))

        rows = await self.app.db.fetchall(
            "SELECT id, role, content FROM messages WHERE thread_id=? AND collapsed=0 ORDER BY id",
            (thread_id,))
        if len(rows) <= trigger:
            return

        old, recent = rows[:-keep], rows[-keep:]
        if not old:
            return
        transcript = "\n".join(f"{r['role']}: {r['content'][:400]}" for r in old)

        prev = await self.app.db.fetchone(
            "SELECT content FROM summaries WHERE thread_id=?", (thread_id,))
        prompt = (f"EXISTING SUMMARY:\n{prev['content'] if prev else '(none)'}\n\n"
                  f"NEW MESSAGES TO FOLD IN:\n{transcript}")
        try:
            summary = await self.app.llm.complete(
                cfg.get_path("models.utility"), SUMMARIZE_SYSTEM, prompt, max_tokens=400)
        except Exception:
            return  # compaction is best-effort; never lose messages over it
        if not summary:
            return

        last_old_id = old[-1]["id"]
        await self.app.db.execute(
            """INSERT INTO summaries(thread_id, content, covers_until_message_id, updated_at)
               VALUES (?,?,?,datetime('now'))
               ON CONFLICT(thread_id) DO UPDATE SET content=excluded.content,
               covers_until_message_id=excluded.covers_until_message_id, updated_at=datetime('now')""",
            (thread_id, summary, last_old_id))
        await self.app.db.execute(
            "UPDATE messages SET collapsed=1 WHERE thread_id=? AND id<=?",
            (thread_id, last_old_id))
        self.app.bus.emit("thread.compacted", thread_id=thread_id)
