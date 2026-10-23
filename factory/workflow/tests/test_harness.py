"""The CI harness's phases, run on the host against stand-in tools."""

import io
import json
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path
from unittest import TestCase

from workflow.guest.ci_harness import Harness

# Stand-ins for `eyg eval -` and the interpreter: read the source JSON on
# stdin; an integer evaluates to itself, anything else is an error.
ORACLE = """
import json, sys
src = json.load(sys.stdin)
if src["0"] == "i":
    print(src["v"])
else:
    sys.exit(print("UndefinedVariable x", file=sys.stderr) or 1)
"""
WRONG_ON_TWO = ORACLE.replace(
    'print(src["v"])', 'print(0 if src["v"] == 2 else src["v"])'
)


def tree_tar(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class HarnessTests(TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        (self.tmp / "tree.tar").write_bytes(
            tree_tar({"oracle.py": ORACLE, "interp.py": WRONG_ON_TWO})
        )
        cases = [
            {"id": "a/0", "area": "a", "name": "one", "source": {"0": "i", "v": 1}},
            {"id": "a/1", "area": "a", "name": "two", "source": {"0": "i", "v": 2}},
            {"id": "b/0", "area": "b", "name": "var", "source": {"0": "v", "l": "x"}},
        ]
        (self.tmp / "holdout.json").write_text(json.dumps(cases))
        script = self.tmp / "conformance"
        script.write_text("#!/bin/sh\necho checking in $(pwd)\nexit 3\n")
        script.chmod(0o700)
        self.harness = Harness(
            {
                "work_dir": str(self.tmp / "work"),
                "tree_tar": str(self.tmp / "tree.tar"),
                "path": "/usr/bin:/bin",
                "build": ["true"],
                "lints": {"ok": ["true"], "bad": ["sh", "-c", "echo nope; exit 1"]},
                "oracle": [sys.executable, "oracle.py"],
                "interpreter": [sys.executable, "interp.py"],
                "conformance_script": str(script),
                "ticket_check": "echo secret; false",
                "holdout": str(self.tmp / "holdout.json"),
                "build_timeout_s": 30,
                "case_timeout_s": 10,
                "output_limit": 200,
            }
        )
        self.harness.unpack()

    def test_conformance_runs_the_tickets_script_in_the_tree(self):
        self.assertEqual(
            self.harness.conformance(),
            {
                "passed": False,
                "exit_code": 3,
                "output": f"checking in {self.tmp / 'work'}\n",
            },
        )

    def test_no_conformance_script_skips_the_phase(self):
        self.harness.c["conformance_script"] = ""
        self.assertIsNone(self.harness.conformance())

    def test_ticket_check_is_pass_fail_only(self):
        self.assertEqual(self.harness.ticket_check(), {"passed": False})

    def test_holdout_reports_counts_only_and_removes_the_file(self):
        report = self.harness.holdout()
        self.assertEqual(
            report,
            {
                "passed": False,
                "total": 3,
                "failed": 1,
                "by_area": {
                    "a": {"total": 2, "failed": 1},
                    "b": {"total": 1, "failed": 0},
                },
            },
        )
        self.assertFalse((self.tmp / "holdout.json").exists())

    def test_report_runs_every_phase(self):
        report = self.harness.report()
        self.assertEqual(
            [k for k, v in report.items() if v and not v["passed"]],
            ["conformance", "lint", "ticket", "holdout"],
        )
        self.assertEqual(report["lint"]["checks"]["bad"]["output"], "nope\n")

    def test_failed_build_stops_there(self):
        self.harness.c["build"] = ["false"]
        self.assertEqual(list(self.harness.report()), ["build"])
