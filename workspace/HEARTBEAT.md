# HEARTBEAT

Evaluated every heartbeat tick by a cheap utility model. If nothing below
needs action, the tick answers `HB_OK` and costs nothing further.

## Standing checks
- If a delegated task has been running for more than 30 minutes, report it
  to the Home thread.
- If a scheduled job failed on its last run, report it.
- If an active goal has no review time set and looks stalled, mention it once.

## Environment checks
(The ENVIRONMENT block in the heartbeat state carries the latest sensor and
system observations — write plain-language conditions against it.)
- If anything in the environment snapshot looks clearly wrong or unsafe
  (device unavailable for a long time, implausible sensor reading), report it.

## User-defined checks
(Add lines like: "Every morning around 9, if there are unread task results
from overnight, summarise them for me." or "If the balcony door is open and
the outside temperature is below 10°C, tell me.")
