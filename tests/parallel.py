"""Runs the unit suite in parallel, one process per test class, for a faster local run.

    .venv/bin/python tests/parallel.py [-j N]

From the repo root; add CI=1 and the venv first on PATH to run as CI does (AGENTS.md,
"Commands"). The serial run stays the one to trust before a push: it is what CI runs.

It discovers the suite as the serial run does (discover -s tests -t .), in a child, and runs
`python -m unittest module.Class` for each class, N at a time, from the repo root, with this
process's environment unchanged. Each child imports the tests package, so each gets a sandbox
of its own (tests/__init__.py). A module that fails to import is run as `python -m unittest
module`, which fails with the import error. The output of every class that fails is printed in
full; the run ends with a summary and exits 1 if any class failed, or if the children ran a
different number of tests than discovery found (a guard that nothing was dropped).

Not handled: a child that hangs hangs the run (with CI=1 each child's own hang dump ends it
after 15 minutes); a test that depends on another class having run first in the same process
would fail here only; a module's load_tests isn't run for a class, so tests it adds are run
by no child (the count guard fails, or, for a test whose id isn't module.Class.method, that
name's child fails); a module that raises unittest.SkipTest at import, skipped in the serial
run, fails here; a class whose tests print a `Ran N tests` line of their own confuses the
count."""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import os
import re
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Measured on a 40-core box: from about 32 children on, the run takes as long as its slowest
# class, so more only add contention (each class starts processes of its own: the fake ssh,
# the helper). A smaller box gets one per core.
DEFAULT_JOBS = min(32, os.cpu_count() or 1)

# Prints one line per discovered test id; a child, so this process never imports the tests
# package, whose sandbox would change the environment the classes get.
LIST = """\
import unittest

def ids(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from ids(test)
        else:
            yield test.id()

for name in ids(unittest.defaultTestLoader.discover("tests", top_level_dir=".")):
    print("id", name)
"""

FAILED_IMPORT = "unittest.loader._FailedTest."
RAN = re.compile(r"^Ran (\d+) tests? in ", re.MULTILINE)
RESULT = re.compile(r"^(?:OK|FAILED)(?: \((.*)\))?$", re.MULTILINE)


def discover():
    """({unit: number of tests}, [modules that fail to import]): a unit is module.Class, or a
    module that fails to import."""
    out = subprocess.run([sys.executable, "-c", LIST], cwd=REPO, capture_output=True,
                         text=True, check=True).stdout
    units = collections.Counter()
    failed_imports = []
    for line in out.splitlines():
        if line.startswith("id "):
            name = line[3:]
            if name.startswith(FAILED_IMPORT):
                failed_imports.append(name[len(FAILED_IMPORT):])
                units[failed_imports[-1]] += 1
            else:
                units[name.rsplit(".", 1)[0]] += 1
    return units, failed_imports


def run_unit(unit):
    start = time.monotonic()
    proc = subprocess.run([sys.executable, "-m", "unittest", unit], cwd=REPO, check=False,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return unit, proc.returncode, proc.stdout, time.monotonic() - start


def counts(output):
    """(tests run, {failures, errors, skipped...}) from a unittest run's last lines."""
    ran = RAN.findall(output)
    result = collections.Counter()
    found = RESULT.findall(output)
    if found and found[-1]:
        for part in found[-1].split(", "):
            key, value = part.split("=")
            result[key] += int(value)
    return int(ran[-1]) if ran else 0, result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the unit suite one class per process.")
    parser.add_argument("-j", type=int, default=DEFAULT_JOBS, metavar="N",
                        help="children at a time (default %d)" % DEFAULT_JOBS)
    args = parser.parse_args(argv)

    start = time.monotonic()
    units, failed_imports = discover()
    # Longest first, by the number of tests in the class: a cheap stand-in for its time, so the
    # big classes don't start last and stretch the run's tail.
    order = sorted(units, key=lambda unit: (-units[unit], unit))
    ran = 0
    totals = collections.Counter()
    failed = []
    times = []
    with concurrent.futures.ThreadPoolExecutor(args.j) as pool:
        for unit, code, output, seconds in pool.map(run_unit, order):
            n, result = counts(output)
            ran += n
            totals.update(result)
            times.append((seconds, unit))
            if code != 0:
                failed.append(unit)
                print("=" * 70)
                print("FAILED: %s (exit %d)" % (unit, code))
                print(output, end="" if output.endswith("\n") else "\n", flush=True)

    discovered = sum(units.values())
    print("=" * 70)
    print("slowest: " + ", ".join("%s %.0f s" % (unit, seconds)
                                  for seconds, unit in sorted(times, reverse=True)[:3]))
    print("%d classes, %d tests run (%d discovered), %s; %.0f s with -j %d"
          % (len(units), ran, discovered,
             ", ".join("%s=%d" % item for item in sorted(totals.items())) or "no failures",
             time.monotonic() - start, args.j))
    for unit in failed_imports:
        print("failed to import: %s" % unit)
    for unit in failed:
        print("failed: %s" % unit)
    if ran != discovered:
        print("the children ran %d tests, discovery found %d" % (ran, discovered))
    ok = not failed and not failed_imports and ran == discovered
    print("OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
