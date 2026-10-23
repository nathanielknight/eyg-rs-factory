import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.test import Client, TestCase

from runner.runner import IncusRunner
from runner.tests.fake import Clock, Command, FakeIncus
from ticket.models import Ticket
from workflow import dev
from workflow.git import Repo
from workflow.validation import RESULTS

SECRET = "operator-secret"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def passing_results(**overrides) -> dict:
    results = {
        "build": {"passed": True, "exit_code": 0, "output": ""},
        "conformance": {"passed": True, "exit_code": 0, "output": ""},
        "lint": {
            "passed": True,
            "checks": {"clippy": {"passed": True, "exit_code": 0, "output": ""}},
        },
        "ticket": None,
        "holdout": {
            "passed": True,
            "total": 4,
            "failed": 0,
            "by_area": {"builtins": {"total": 4, "failed": 0}},
        },
    }
    return {**results, **overrides}


class WorkflowTestCase(TestCase):
    "A real bare repo, the runner over a fake Incus, and settings to match."

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        suites = self.tmp / "suites"
        suites.mkdir()
        (suites / "secret.json").write_text(
            json.dumps([{"name": "hidden", "source": {"0": "i", "v": 2}}])
        )
        override = self.settings(
            WORKFLOW_ROOT=self.tmp / "data",
            WORKFLOW_REPO=self.tmp / "repo.git",
            WORKFLOW_HOLDOUT_SUITES=[suites / "secret.json"],
            WORKFLOW_REVIEWERS=["nat@example.com"],
            FACTORY_SHARED_SECRET=SECRET,
        )
        override.enable()
        self.addCleanup(override.disable)

        bare = self.tmp / "repo.git"
        git(self.tmp, "init", "--quiet", "--bare", "--initial-branch=main", str(bare))
        self.work = self.tmp / "work"
        git(self.tmp, "clone", "--quiet", str(bare), str(self.work))
        self.write({"Cargo.toml": "[package]\n", "src/main.rs": "fn main() {}\n"})
        git(self.work, "add", "-A")
        git(self.work, "commit", "--quiet", "-m", "init")
        git(self.work, "push", "--quiet", "origin", "HEAD:main")
        self.repo = Repo(bare)

        self.clock = Clock()
        self.incus = FakeIncus(self.clock)
        self.runner = IncusRunner(
            self.incus,
            self.tmp / "runs",
            now=self.clock.now,
            sleep=self.clock.sleep,
            monotonic=self.clock.monotonic,
        )
        self.operator = Client(headers={"Authorization": f"Bearer {SECRET}"})

    # -- git ---------------------------------------------------------------

    def write(self, files: dict[str, str], root: Path | None = None) -> None:
        for name, text in files.items():
            path = (root or self.work) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def advance_main(self, files: dict[str, str]) -> str:
        "Someone else's change lands on main."
        git(self.work, "pull", "--quiet", "origin", "main")
        self.write(files)
        git(self.work, "add", "-A")
        git(self.work, "commit", "--quiet", "-m", "elsewhere")
        git(self.work, "push", "--quiet", "origin", "HEAD:main")
        return self.repo.main_sha()

    def agent_change(self, base: str, files: dict[str, str]) -> tuple[Path, str]:
        "What an agent does in its VM: commit on base, bundle base..HEAD."
        clone = Path(tempfile.mkdtemp(dir=self.tmp))
        git(clone, "clone", "--quiet", str(self.repo.path), ".")
        git(clone, "checkout", "--quiet", base)
        self.write(files, clone)
        git(clone, "add", "-A")
        git(clone, "commit", "--quiet", "-m", "agent work")
        bundle = clone / "submission.bundle"
        git(clone, "bundle", "create", "--quiet", str(bundle), f"{base}..HEAD")
        return bundle, git(clone, "rev-parse", "HEAD")

    # -- workflow ----------------------------------------------------------

    def make_ticket(self, **kwargs) -> Ticket:
        defaults = {
            "title": "Implement let",
            "description": "Add let bindings",
            "test_command": "cargo test",
        }
        return Ticket.objects.create(**{**defaults, **kwargs})

    def dispatch(self, ticket: Ticket | None = None):
        "A dispatched dev run, before its VM runs, and its bridge token."
        ticket = ticket or self.make_ticket()
        dev_run = dev.dispatch(self.runner, self.repo, ticket)
        secrets_env = (dev._staging_dir(dev_run.session_id) / "secrets.env").read_text()
        token = next(
            line.split("=", 1)[1].strip("'")
            for line in secrets_env.splitlines()
            if line.startswith("FACTORY_TOKEN=")
        )
        return dev_run, token

    def submit(self, token: str, bundle: Path, head: str, **fields):
        client = Client(headers={"Authorization": f"Bearer {token}"})
        with bundle.open("rb") as f:
            data = {
                "submission_id": "11111111-2222-3333-4444-555555555555",
                "ticket": "",
                "summary": "Added let.",
                "review": "false",
                "head_sha": head,
                "bundle": f,
                **fields,
            }
            return client.post("/submissions/", data)

    def submitted_run(self, files=None, **fields):
        "A dev run that submitted a change and ended; returns (dev_run, submission)."
        dev_run, token = self.dispatch()
        bundle, head = self.agent_change(
            dev_run.base_sha, files or {"src/let.rs": "// let\n"}
        )
        resp = self.submit(token, bundle, head, ticket=str(dev_run.ticket_id), **fields)
        self.assertEqual(resp.status_code, 201, resp.content)
        self.incus.command = Command(exit_code=0, stdout=b'{"type":"result"}\n')
        dev.drive(self.runner, dev_run)
        dev_run.refresh_from_db()
        return dev_run, dev_run.submission

    def ci_results(self, results: dict) -> None:
        "The next CI run reports `results`."
        self.incus.command = Command(
            exit_code=0, files={RESULTS: json.dumps(results).encode()}
        )
