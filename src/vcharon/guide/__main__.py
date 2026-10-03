"""python -m vcharon.guide --write PATH: write docs/GUIDE.md from the topic files (or, without
--write, print it)."""

from __future__ import annotations

import argparse
import os
import sys

from . import document


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m vcharon.guide",
                                     description="Make docs/GUIDE.md from the guide's topics.")
    parser.add_argument("--write", metavar="PATH", help="write it to PATH instead of stdout")
    args = parser.parse_args(argv)
    text = document()
    if args.write is None:
        sys.stdout.write(text)
        return 0
    tmp = args.write + ".tmp"
    # newline="": the same bytes on every OS, so the test that compares them holds on Windows
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, args.write)
    print("wrote %s" % args.write)
    return 0


if __name__ == "__main__":
    sys.exit(main())
