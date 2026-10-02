"""vcharon: move files between the two ends of an ssh connection."""

VERSION = "0.1.0"
# Both ends run the same bundled code, so this changes only when the frames or messages do.
PROTOCOL = 3
# The oldest Python either end may run: Apple's /usr/bin/python3 is 3.9.
FLOOR = (3, 9)
