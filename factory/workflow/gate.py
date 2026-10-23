"""Static checks on what a submission changes, before anything runs.

Protected paths reject the submission outright. Review triggers let it
through validation but hold the merge for Nat.
"""

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

# Added lines in Rust sources that can make tests or behaviour differ under
# test. Plain #[cfg(test)] modules are normal and not flagged.
SUSPICIOUS_LINES = [
    ("ignore", re.compile(r"#\s*\[\s*ignore\b")),
    ("cfg-test", re.compile(r"cfg!\s*\(\s*(not\s*\(\s*)?test\b")),
    ("cfg-test", re.compile(r"#\s*\[\s*cfg\s*\(\s*not\s*\(\s*test\b")),
    ("cfg-test", re.compile(r"#\s*\[\s*cfg_attr\s*\(\s*(not\s*\(\s*)?test\b")),
]


@dataclass
class GateResult:
    protected: list[str] = field(default_factory=list)  # paths that reject it
    review: list[str] = field(default_factory=list)  # "<rule>: <path>"

    @property
    def rejected(self) -> bool:
        return bool(self.protected)

    @property
    def review_rules(self) -> list[str]:
        return sorted({r.split(":", 1)[0] for r in self.review})


def _matches(path: str, patterns: list[str]) -> str | None:
    p = PurePosixPath(path)
    return next((pat for pat in patterns if p.full_match(pat)), None)


def check(
    changed: list[str],
    added: list[tuple[str, str]],
    *,
    protected: list[str],
    review_triggers: list[str],
) -> GateResult:
    result = GateResult()
    for path in changed:
        if _matches(path, protected):
            result.protected.append(path)
        elif rule := _matches(path, review_triggers):
            result.review.append(f"{PurePosixPath(rule).name}: {path}")
    for path, line in added:
        if not path.endswith(".rs"):
            continue
        for rule, pattern in SUSPICIOUS_LINES:
            if pattern.search(line):
                result.review.append(f"{rule}: {path}")
                break
    result.review = sorted(set(result.review))
    return result
