#!/usr/bin/env python3
"""factory: the agent's line to the factory, from inside a dev run VM.

    factory ticket                          show the ticket
    factory notes                           show the ticket's notes
    factory note TEXT | factory note -      add a note (- reads stdin)
    factory submit --summary TEXT [--review]
                                            submit HEAD (commit first)

Reads FACTORY_URL, FACTORY_TOKEN, FACTORY_TICKET and FACTORY_BASE_SHA from
the environment. Standard library only; this runs on the guest's python3.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid


def env(name):
    value = os.environ.get(name)
    if not value:
        sys.exit(f"factory: {name} is not set")
    return value


def request(method, path, body=None, content_type=None, retries=3):
    url = env("FACTORY_URL").rstrip("/") + path
    headers = {"Authorization": f"Bearer {env('FACTORY_TOKEN')}"}
    if content_type:
        headers["Content-Type"] = content_type
    for attempt in range(retries):
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()
        except OSError as e:
            if attempt == retries - 1:
                sys.exit(f"factory: {method} {path}: {e}")
            time.sleep(2**attempt)


def report(status, text):
    if status >= 400:
        try:
            text = json.loads(text).get("error", text)
        except ValueError:
            pass
        sys.exit(f"factory: error {status}: {text}")
    print(text)


def git(*args):
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def multipart(fields, file_field, file_path):
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode()
        )
    with open(file_path, "rb") as f:
        data = f.read()
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
        f'filename="submission.bundle"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n".encode()
        + data
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def submit(summary, review):
    if git("status", "--porcelain"):
        sys.exit("factory: the working tree has uncommitted changes; commit them first")
    base = env("FACTORY_BASE_SHA")
    head = git("rev-parse", "HEAD")
    if head == base:
        sys.exit("factory: HEAD is still the base commit; nothing to submit")
    if not summary.strip():
        sys.exit("factory: --summary must not be empty")
    with tempfile.TemporaryDirectory() as tmp:
        bundle = os.path.join(tmp, "submission.bundle")
        git("bundle", "create", "--quiet", bundle, f"{base}..HEAD")
        body, content_type = multipart(
            {
                # Same ID on every retry, so a resent upload isn't a second one.
                "submission_id": str(uuid.uuid4()),
                "ticket": env("FACTORY_TICKET"),
                "summary": summary,
                "review": "true" if review else "false",
                "head_sha": head,
            },
            "bundle",
            bundle,
        )
        report(*request("POST", "/submissions/", body, content_type))


def main(argv):
    parser = argparse.ArgumentParser(prog="factory")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ticket", help="show the ticket")
    sub.add_parser("notes", help="show the ticket's notes")
    note = sub.add_parser("note", help="add a note to the ticket")
    note.add_argument("text", help="the note, as markdown; - reads stdin")
    s = sub.add_parser("submit", help="submit HEAD for validation")
    s.add_argument("--summary", required=True, help="what changed and why")
    s.add_argument("--review", action="store_true", help="ask for human review")
    args = parser.parse_args(argv)

    ticket = env("FACTORY_TICKET")
    match args.command:
        case "ticket":
            report(*request("GET", f"/tickets/{ticket}/"))
        case "notes":
            report(*request("GET", f"/tickets/{ticket}/notes/"))
        case "note":
            text = sys.stdin.read() if args.text == "-" else args.text
            report(
                *request(
                    "POST",
                    f"/tickets/{ticket}/notes/",
                    text.encode(),
                    "text/markdown",
                )
            )
        case "submit":
            submit(args.summary, args.review)


if __name__ == "__main__":
    main(sys.argv[1:])
