"""The host's bare repo, driven through the git CLI.

Nothing here checks anything out: submissions are fetched into refs, merged
with merge-tree and committed with commit-tree. Every operation is safe to
repeat.
"""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

SUBMISSION_REFS = "refs/factory/submissions"
VALIDATION_REFS = "refs/factory/validations"


class GitError(Exception):
    def __init__(self, args: list[str], result: subprocess.CompletedProcess):
        self.stderr = result.stderr.strip()
        super().__init__(
            f"git {' '.join(args)}: exit {result.returncode}: {self.stderr}"
        )


@dataclass(frozen=True)
class MergeTree:
    tree: str | None  # None if it conflicts
    conflicts: list[str]


@dataclass(frozen=True)
class Identity:
    name: str
    email: str


class Repo:
    def __init__(self, path: Path, main: str = "main"):
        self.path = Path(path)
        self.main_ref = f"refs/heads/{main}"

    def _git(
        self, *args: str, check: bool = True, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess:
        argv = ["git", "-C", str(self.path), *args]
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})},
        )
        if check and result.returncode != 0:
            raise GitError(list(args), result)
        return result

    def rev_parse(self, ref: str) -> str | None:
        result = self._git(
            "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False
        )
        return result.stdout.strip() or None

    def main_sha(self) -> str:
        sha = self.rev_parse(self.main_ref)
        if sha is None:
            raise GitError(["rev-parse", self.main_ref], _missing(self.main_ref))
        return sha

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        result = self._git(
            "merge-base", "--is-ancestor", ancestor, descendant, check=False
        )
        if result.returncode not in (0, 1):
            raise GitError(["merge-base", "--is-ancestor"], result)
        return result.returncode == 0

    # -- bundles -----------------------------------------------------------

    def bundle_main(self, dest: Path) -> str:
        "Bundle main's full history; returns the commit it holds."
        self._git("bundle", "create", "--quiet", str(dest), self.main_ref)
        return self.bundle_heads(dest)[self.main_ref]

    def verify_bundle(self, bundle: Path) -> str | None:
        "None if the bundle is sound and its prerequisites are here, else why not."
        result = self._git("bundle", "verify", "--quiet", str(bundle), check=False)
        return (
            None
            if result.returncode == 0
            else (result.stderr.strip() or "invalid bundle")
        )

    def bundle_heads(self, bundle: Path) -> dict[str, str]:
        out = self._git("bundle", "list-heads", str(bundle)).stdout
        heads = {}
        for line in out.splitlines():
            sha, _, ref = line.partition(" ")
            heads[ref] = sha
        return heads

    def fetch_bundle(self, bundle: Path, ref: str) -> str:
        "Fetch the bundle's HEAD into `ref`, checking every object; returns its sha."
        self._git(
            "-c", "fetch.fsckObjects=true",
            "-c", "transfer.fsckObjects=true",
            "fetch", "--quiet", "--no-tags", str(bundle), f"+HEAD:{ref}",
        )  # fmt: skip
        return self.rev_parse(ref)

    # -- inspecting a change -----------------------------------------------

    def changed_files(self, base: str, head: str) -> list[str]:
        out = self._git("diff", "--name-only", "--no-renames", "-z", base, head).stdout
        return [p for p in out.split("\0") if p]

    def added_lines(self, base: str, head: str) -> list[tuple[str, str]]:
        "(path, line) for every line the change adds."
        out = self._git(
            "diff", "--no-renames", "--no-color", "--no-ext-diff", "-U0", base, head
        ).stdout
        path, added = None, []
        for line in out.splitlines():
            if line.startswith("+++ "):
                target = line[4:]
                path = target[2:] if target.startswith("b/") else None
            elif line.startswith("+") and path is not None:
                added.append((path, line[1:]))
        return added

    # -- merging -----------------------------------------------------------

    def merge_tree(self, base: str, head: str) -> MergeTree:
        result = self._git(
            "merge-tree", "--write-tree", "--name-only", "--no-messages", base, head,
            check=False,
        )  # fmt: skip
        lines = result.stdout.splitlines()
        if result.returncode == 0:
            return MergeTree(tree=lines[0], conflicts=[])
        if result.returncode == 1:
            return MergeTree(tree=None, conflicts=lines[1:])
        raise GitError(["merge-tree", base, head], result)

    def commit_tree(
        self,
        tree: str,
        parents: list[str],
        message: str,
        identity: Identity,
        date: str,
    ) -> str:
        "A commit that's the same every time for the same inputs."
        args = ["commit-tree", tree]
        for p in parents:
            args += ["-p", p]
        args += ["-m", message]
        env = {
            "GIT_AUTHOR_NAME": identity.name,
            "GIT_AUTHOR_EMAIL": identity.email,
            "GIT_AUTHOR_DATE": date,
            "GIT_COMMITTER_NAME": identity.name,
            "GIT_COMMITTER_EMAIL": identity.email,
            "GIT_COMMITTER_DATE": date,
        }
        return self._git(*args, env=env).stdout.strip()

    def update_ref(self, ref: str, new: str, old: str | None = None) -> bool:
        "Point `ref` at `new`, only if it's at `old` (when given). False if it moved."
        args = ["update-ref", ref, new] + ([old] if old else [])
        result = self._git(*args, check=False)
        return result.returncode == 0

    def archive(self, sha: str, dest: Path) -> None:
        self._git("archive", "--format=tar", f"--output={dest}", sha)


def _missing(ref: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 128, "", f"{ref} does not exist")


def get_repo() -> Repo:
    "The repo configured by settings."
    from django.conf import settings

    return Repo(settings.WORKFLOW_REPO, settings.WORKFLOW_MAIN_BRANCH)
