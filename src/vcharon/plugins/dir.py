"""The dir sink: apply a plan under a directory (DESIGN §9.2), on either end."""

from __future__ import annotations

import os

from .. import plugin
from ..plan import Plan
from ..plugin import Option
from ..proto import VCharonError

NAME = "dir"
KINDS = ("sink",)
ENDS = ("local", "remote")
NEEDS = ()
CAPS = ()
OPTIONS = {"path": Option(str, required=True), "create": Option(bool, default=False),
           "max_deletes": Option(int, default=500)}


class Sink(plugin.StagerSink):
    def check(self, plan):
        root = self.ctx.resolve(self.options["path"], "to.path")
        self.make_stager(root, create=self.options["create"],
                         max_deletes=self.options["max_deletes"])
        return self.stager.check(plan)

    def doctor(self):
        """ferry doctor: the Stager's own root checks on an empty plan (symlinks on the way,
        the owner rule, create), then that the root is writable. Only reads: an empty plan
        never creates the root or a stage dir."""
        try:
            checked = self.check(Plan([]))
        except VCharonError as e:
            return [("FAIL", e.message, e.hint)]
        return [root_check(checked.root, "to.path %s: a directory you can write" % checked.root,
                           "to.path %s doesn't exist yet; the first run creates it"
                           % checked.root)]


def root_check(root, ok_text, missing_text):
    """The doctor check of a checked root: writable, or missing (so create is on) under an
    ancestor the first run can create it in (review W5)."""
    if os.path.isdir(root):
        if os.access(root, os.W_OK | os.X_OK):
            return ("ok", ok_text, None)
        return ("FAIL", "you can't write in %s" % root, "fix its permissions")
    # the nearest existing ancestor; the Stager's check found it's a directory
    up = root
    while not os.path.exists(up):
        parent = os.path.dirname(up)
        if parent == up:
            break
        up = parent
    if not os.access(up, os.W_OK | os.X_OK):
        return ("FAIL", "can't create %s: you can't write in %s" % (root, up),
                "fix its permissions, or create %s yourself" % root)
    return ("ok", missing_text, None)
