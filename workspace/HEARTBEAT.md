# HEARTBEAT

Evaluated every heartbeat tick by a cheap utility model. If nothing below
needs action, the tick answers `HB_OK` and costs nothing further.

## Standing checks
- If a sub-agent task has been running for more than 30 minutes, report it
  to the Home thread.
- If a scheduled job failed on its last run, report it.

## User-defined checks
(Add lines like: "Every morning around 9, if there are unread task results
from overnight, summarise them for me.")
