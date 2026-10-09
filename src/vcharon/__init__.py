"""vcharon: file-based channels for AI agents, on one machine or across machines over plain ssh."""

VERSION = "0.5.0rc3"
# Both ends run the same bundled code, so this changes only when the frames or messages do.
PROTOCOL = 3
# The oldest Python either end may run.
FLOOR = (3, 13)
