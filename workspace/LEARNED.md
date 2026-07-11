# LEARNED
- For Google Calendar auth setup, instruct the user to create a Google Cloud OAuth desktop client, then paste the Client ID and Client Secret and connect the Google service with workspace-mcp.
- If the calendar service is unavailable, clearly say the event could not be added and offer to format the details for manual entry.
- When shell access is needed, ask the user to approve the environment/UI prompt for the `shell` tool so workspace commands can proceed; if approvals are timed out or disabled, wait until they are re-enabled before retrying.
- When re-running failed coder tasks, ask whether the Python script should compute the sum dynamically or just print a known precomputed result.
- If an image’s song titles are unreadable, ask for a text list or a clearer re-upload, then return exactly 5 names only once the titles are readable.
- When extracting specific items from web pages, only return results if the required content is visible in accessible HTML; otherwise request alternative URLs or pasted data.
- When fetching release dates from YouTube/Spotify, treat rough relative timestamps and blocked/consent-gated pages as insufficient for trustworthy per-track verification.
- When delegating to another agent or sub-agent, account for possible HTTP 429 rate limits by retrying or using additional API/integration capacity as needed.
