#!/usr/bin/env python3
"""CI harness: runs in an ephemeral CI VM and writes a JSON report.

    ci_harness.py CONFIG

Unpacks the merged tree, builds it, then runs the ticket's conformance script,
the lints, the ticket's held-back check and the holdout suite. The holdout
runs last, so nothing that could have seen it feeds the report's public parts,
and for it the report holds counts only, never cases, inputs or outputs.

Exits 0 once it has written the report, pass or fail; anything else is a
harness fault. Standard library only.
"""

import json
import os
import signal
import subprocess
import sys
import tarfile
from pathlib import Path


class Harness:
    def __init__(self, config: dict):
        self.c = config
        self.work = Path(config["work_dir"])
        self.limit = config["output_limit"]

    # -- running things ----------------------------------------------------

    def run(self, argv, *, input=None, timeout):
        "Run in the work tree; (exit, stdout, stderr), exit None on timeout."
        env = {**os.environ, "PATH": self.c["path"], "CARGO_NET_OFFLINE": "true"}
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=self.work,
            env=env,
            start_new_session=True,
        )
        try:
            out, err = proc.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            out, err = proc.communicate()
            return None, out, err
        return proc.returncode, out, err

    def truncate(self, text: str) -> str:
        if len(text) <= self.limit:
            return text
        return text[: self.limit] + f"\n... ({len(text) - self.limit} more characters)"

    def _step(self, argv, timeout) -> dict:
        code, out, err = self.run(argv, timeout=timeout)
        return {
            "passed": code == 0,
            "exit_code": code,
            "output": self.truncate(out + err),
        }

    # -- phases ------------------------------------------------------------

    def unpack(self) -> None:
        self.work.mkdir(parents=True, exist_ok=True)
        with tarfile.open(self.c["tree_tar"]) as tar:
            tar.extractall(self.work, filter="data")

    def build(self) -> dict:
        return self._step(self.c["build"], self.c["build_timeout_s"])

    def conformance(self) -> dict | None:
        "The ticket's conformance script, if it has one."
        if not self.c["conformance_script"]:
            return None
        return self._step([self.c["conformance_script"]], self.c["build_timeout_s"])

    def lint(self) -> dict:
        checks = {
            name: self._step(argv, self.c["build_timeout_s"])
            for name, argv in self.c["lints"].items()
        }
        return {"passed": all(c["passed"] for c in checks.values()), "checks": checks}

    def ticket_check(self) -> dict | None:
        "The ticket's held-back verify command: pass/fail only."
        if not self.c["ticket_check"]:
            return None
        code, _, _ = self.run(
            ["sh", "-c", self.c["ticket_check"]], timeout=self.c["build_timeout_s"]
        )
        return {"passed": code == 0}

    def _case(self, case: dict) -> bool:
        "Whether the interpreter agrees with the oracle (as scripts/verify.py)."
        source = json.dumps(case["source"])
        timeout = self.c["case_timeout_s"]
        oracle = self.run(self.c["oracle"], input=source, timeout=timeout)
        if oracle[0] is None:
            return False
        code, out, err = self.run(self.c["interpreter"], input=source, timeout=timeout)
        if code is None:
            return False

        def norm(t):
            return " ".join(t.split())

        def first_line(t):
            return norm(t.strip().splitlines()[0]) if t.strip() else ""

        if oracle[0] == 0:
            return code == 0 and norm(out) == norm(oracle[1])
        return code != 0 and first_line(err) == first_line(oracle[2])

    def holdout(self) -> dict:
        path = Path(self.c["holdout"])
        cases = json.loads(path.read_text())
        path.unlink()
        by_area: dict[str, dict] = {}
        failed = 0
        for case in cases:
            area = by_area.setdefault(case["area"], {"total": 0, "failed": 0})
            area["total"] += 1
            if not self._case(case):
                area["failed"] += 1
                failed += 1
        return {
            "passed": failed == 0,
            "total": len(cases),
            "failed": failed,
            "by_area": by_area,
        }

    def report(self) -> dict:
        self.unpack()
        report = {"build": self.build()}
        if not report["build"]["passed"]:
            return report
        report["conformance"] = self.conformance()
        report["lint"] = self.lint()
        report["ticket"] = self.ticket_check()
        # Last: nothing after this reports output.
        report["holdout"] = self.holdout()
        return report


def main(argv):
    config = json.loads(Path(argv[0]).read_text())
    report = Harness(config).report()
    Path(config["results"]).write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    main(sys.argv[1:])
