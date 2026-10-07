"""Scan local blobs reachable from all refs and HEAD, without retrieving missing objects.

Print categories, escaped path hints and author names, not matches or author addresses;
author/committer e-mails other than GitHub's noreply ones are counted for review.
Exit 0: complete, no candidates; 1: complete, review candidates; 2: incomplete/refused.
This is a pattern diagnostic, not proof that arbitrary secrets are absent.

    uv run --frozen python scripts/history-scan.py --repo <public-clone>

Run it on a separate complete clone holding only the public refs to inspect (not a development
checkout with private backups). It reads local blobs reachable from all refs and HEAD, binary
content included, and checks every name in the historical raw diffs. It does not fetch missing
objects, apply filters, honour replacement objects, rewrite refs or inspect untracked files.
Metadata logs disable signature verification, mailmaps and display settings: the configured
signature verifier is not launched and author names are not mapped. Unreachable or reflog-only
objects and arbitrary secret formats are outside its claim. Exit 1 includes synthetic test data
that needs review.

Budgets: a blob larger than --max-blob-bytes is listed and makes the scan incomplete; each
pattern operation has two seconds (the locked `regex` dependency in compatibility mode); Git
metadata and reader calls have 120 seconds. An exceeded budget or a native failure is never a
clean result. Raw matches, author email addresses and native error details are not printed;
path hints are redacted and escaped; match totals count each blob once, even with aliases.

Attribution trailers (Co-Authored-By, "Generated with", names of code assistants and models) are
counted in blobs and in commit messages, never printed: the project has one author.

Pull-request refs (refs/pull/*) are not fetched by a normal clone; to include them, scan a
`git clone --mirror` of the repository.

Needs Git 2.36 or later (`cat-file --batch-command`): an unsupported command is an incomplete
scan, not an empty history. https://git-scm.com/docs/git-cat-file#_batch_output
"""

import argparse
import codecs
import collections
import contextlib
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

import regex

REPO = Path(__file__).resolve().parents[1]
MAX_BLOB_BYTES = 5_000_000
GIT_TIMEOUT = 120
MATCH_TIMEOUT = 2
OID = rb"(?:[0-9a-f]{40}|[0-9a-f]{64})"
HEADER = re.compile(rb"(" + OID + rb") (blob|tree|commit|tag) (0|[1-9][0-9]*)\n")
RAW_CHANGE = re.compile(rb":[0-7]{6} [0-7]{6} (" + OID + rb") (" + OID + rb") [ACDMTUXB]")
RISKY_PATH = re.compile(
    rb"(^|/)(data(?:/|$)|config\.yaml$|secrets(?:\.[^/]*)?$|master\.key$|\.coverage(?:\.[^/]*)?$"
    rb"|lan-auth(?:\.[^/]*)?$|tow\.jsonl$|state\.json$|download_history(?:\.json)?$)"
)
# The only author/committer addresses public history may carry: GitHub's private ones.
NOREPLY_EMAIL = re.compile(rb"[^@\s]+@users\.noreply\.github\.com|noreply@github\.com", re.IGNORECASE)
DISPLAY_EMAIL = regex.compile(rb"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", regex.VERSION0)
# Assistant and model names, kept in ROT13 so that this file does not name them itself.
ASSISTANT_NAMES = codecs.decode("pynhqr|naguebcvp|bcranv|pungtcg|tcg-[0-9]|pbcvybg|trzvav|pbqrk", "rot13").encode()
ATTRIBUTION = re.compile(rb"(?i)co-authored-by\s*:|generated with\b|\b(?:" + ASSISTANT_NAMES + rb")\b")
PATTERNS = {
    "telegram bot token": re.compile(rb"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),
    "telegram chat id": re.compile(rb"chat_ids?\W{0,6}-?\d{6,}"),
    "password value": re.compile(
        rb"(?i)(?:password|passwd|pw)\s*[:=]\s*['\"]?(?!\s|['\"]?$|\*|<|\{|None|null|\"\"|''|ask|\$)[^\s'\"]{4,}"
    ),
    "cookie value": re.compile(rb"(?i)(?:uid|pass|phpbb\w*|bb_session|cf_clearance)\s*[:=]\s*['\"]?[A-Za-z0-9%]{12,}"),
    "fernet / master key": re.compile(rb"\b[A-Za-z0-9_-]{43}=(?![A-Za-z0-9])"),
    "private IP": re.compile(rb"\b(?:192\.168|10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"),
    "user profile path": re.compile(rb"(?i)C:\\\\?Users\\\\?[A-Za-z0-9._-]+"),
    "email": re.compile(rb"[A-Za-z0-9._%+-]+@(?!users\.noreply\.github\.com|example\.)[A-Za-z0-9.-]+\.[a-z]{2,}"),
    "attribution trailer": ATTRIBUTION,
}
SEARCH_PATTERNS = {label: regex.compile(pattern.pattern, regex.VERSION0) for label, pattern in PATTERNS.items()}


class ScanError(RuntimeError):
    """Only fixed diagnostic text may cross the command boundary."""


def git_environment():
    # --repo must not be redirected by inherited repository/namespace/object variables.
    kept = {"GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM"}
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_") or key in kept}
    env.update(GIT_NO_REPLACE_OBJECTS="1", GIT_NO_LAZY_FETCH="1", GIT_ALLOW_PROTOCOL="", GIT_OPTIONAL_LOCKS="0")
    return env


def git(repo, *args):
    if args and args[0] == "log":
        # Read stored metadata, not signatures, external author maps or display settings.
        args = (
            args[0],
            "--no-show-signature",
            "--no-use-mailmap",
            "--no-notes",
            "--no-decorate",
            "--no-color",
            *args[1:],
        )
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        env=git_environment(),
        timeout=GIT_TIMEOUT,
        check=False,
    )
    if result.returncode:
        raise ScanError("Git metadata command failed")
    return result.stdout


def historical_paths(repo):
    # rev-list --objects supplies only one ambiguous name per object. Raw NUL-delimited
    # diffs preserve every changed name, including deleted aliases and separate merge parents.
    changes = git(
        repo,
        "log",
        "--all",
        "--root",
        "--full-history",
        "-m",
        "--raw",
        "-z",
        "--format=",
        "--no-abbrev",
        "--no-renames",
        "--no-ext-diff",
        "--no-textconv",
    ).split(b"\0")
    paths = collections.defaultdict(set)
    index = 0
    while index < len(changes):
        header = changes[index].lstrip(b"\n")
        index += 1
        if not header:
            continue
        match = RAW_CHANGE.fullmatch(header)
        if match is None or index >= len(changes) or not changes[index]:
            raise ScanError("Git path history is incomplete")
        name = changes[index]
        index += 1
        for oid in match.groups():
            if oid.strip(b"0"):
                paths[oid].add(name)
    return paths


class GitBatch:
    """One owned, deadline-bounded child; no filters, textconv or network fetches."""

    def __init__(self, repo):
        self.process = subprocess.Popen(
            ["git", "-C", str(repo), "cat-file", "--batch-command"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=git_environment(),
        )
        self.drainer = threading.Thread(target=self.discard_errors, daemon=True)
        self.timer = threading.Timer(GIT_TIMEOUT, self.abort)
        self.timer.daemon = True
        try:
            self.drainer.start()
            self.timer.start()
        except RuntimeError:
            self.__exit__(RuntimeError, None, None)
            raise ScanError("Git reader threads could not start") from None

    def abort(self):
        # Only this reader's child, never a runtime service or a discovered PID.
        with contextlib.suppress(OSError):
            self.process.kill()

    def discard_errors(self):
        # Drain while talking to Git, without retaining or exposing native diagnostics.
        with contextlib.suppress(OSError, ValueError):
            while self.process.stderr.read(65536):
                pass

    def __enter__(self):
        return self

    def __exit__(self, kind, _value, _traceback):
        self.timer.cancel()
        close_failed = False
        try:
            self.process.stdin.close()
        except OSError:
            close_failed = True
        if kind is not None or close_failed:
            self.abort()
        try:
            code = self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.abort()
            self.process.wait(timeout=10)
            raise ScanError("Git object reader did not finish") from None
        finally:
            try:
                self.process.stdout.close()
            finally:
                if self.drainer.ident is not None:
                    self.drainer.join(timeout=10)
                if self.timer.ident is not None:
                    self.timer.join(timeout=10)
                self.process.stderr.close()
        if kind is None and (code or close_failed):
            raise ScanError("Git object reader failed")

    def request(self, command, oid):
        if re.fullmatch(OID, oid) is None:
            raise ScanError("Git returned an invalid object identity")
        self.process.stdin.write(command + b" " + oid + b"\n")
        self.process.stdin.flush()
        header = self.process.stdout.readline(256)
        match = HEADER.fullmatch(header)
        if match is None or match[1] != oid:
            raise ScanError("Git object metadata is missing or incomplete")
        return match[2], int(match[3])

    def read(self, oid, size):
        if self.request(b"contents", oid) != (b"blob", size):
            raise ScanError("Git object metadata changed")
        data = bytearray()
        while len(data) < size:
            block = self.process.stdout.read(min(65536, size - len(data)))
            if not block:
                raise ScanError("Git object content is incomplete")
            data.extend(block)
        if self.process.stdout.read(1) != b"\n":
            raise ScanError("Git object content terminator is invalid")
        return bytes(data)


def match_count(label, pattern, blob):
    if label == "email" and b"@" not in blob:
        return 0
    try:
        return sum(1 for _ in pattern.finditer(blob, timeout=MATCH_TIMEOUT))
    except TimeoutError:
        raise ScanError("pattern matching budget exceeded") from None


def safe_hint(value):
    try:
        for pattern in (*SEARCH_PATTERNS.values(), DISPLAY_EMAIL):
            value = pattern.sub(b"[redacted]", value, timeout=MATCH_TIMEOUT)
    except TimeoutError:
        raise ScanError("diagnostic redaction budget exceeded") from None
    return json.dumps(value.decode("utf-8", "surrogateescape"), ensure_ascii=True)


def scan(repo, max_blob_bytes):
    shallow = git(repo, "rev-parse", "--is-shallow-repository").strip()
    if shallow != b"false":
        raise ScanError("shallow or unverified repository history")
    objects = git(repo, "rev-list", "--objects", "--all", "--no-object-names").splitlines()
    paths = historical_paths(repo)
    authors = git(repo, "log", "--all", "--format=%an%x00").split(b"\0")
    exposed_commits, exposed_addresses = exposed_identities(git(repo, "log", "--all", "--format=%ae%x00%ce"))
    attributed = attributed_messages(git(repo, "log", "--all", "--format=%B%x00"))
    hits, totals = collections.defaultdict(collections.Counter), collections.Counter()
    risky_paths, skipped = set(), []
    scanned = 0
    with GitBatch(repo) as batch:
        for oid in dict.fromkeys(objects):
            kind, size = batch.request(b"info", oid)
            if kind != b"blob":
                continue
            names = paths.get(oid, {b"(unnamed blob " + oid[:12] + b")"})
            risky_paths.update(name for name in names if RISKY_PATH.search(name.lower()))
            if size > max_blob_bytes:
                skipped.append((oid, size))
                continue
            blob = batch.read(oid, size)
            scanned += 1
            for label, pattern in SEARCH_PATTERNS.items():
                count = match_count(label, pattern, blob)
                if count:
                    totals[label] += count
                    hits[label].update(dict.fromkeys(names, count))
    current = git(repo, "rev-list", "--objects", "--all", "--no-object-names").splitlines()
    if set(current) != set(objects):
        raise ScanError("reachable history changed during the scan; retry on a stable clone")
    print("blobs scanned:", scanned)
    print("blobs skipped:", len(skipped))
    print("runtime/secret-looking paths:", ", ".join(safe_hint(name) for name in sorted(risky_paths)) or "none")
    for label, files in hits.items():
        print(f"\n{label}: {totals[label]} matches in {len(files)} historical path hints")
        for name, count in sorted(files.items(), key=lambda row: (-row[1], row[0]))[:8]:
            print(f"   {count:4} {safe_hint(name)}")
    names = collections.Counter(name.strip(b"\n") for name in authors if name.strip(b"\n"))
    print(
        "\nauthor names:", ", ".join(f"{safe_hint(name)} ({count})" for name, count in sorted(names.items())) or "none"
    )
    print(
        "author/committer e-mails not on GitHub's noreply domain:",
        f"{exposed_addresses} in {exposed_commits} commits" if exposed_commits else "none",
    )
    print("attribution trailers in commit messages:", f"{attributed} commits" if attributed else "none")
    for oid, size in skipped:
        print(f"unscanned blob: {oid.decode('ascii')} ({size} bytes)")
    if skipped:
        print("scan incomplete: blob budget exceeded; increase --max-blob-bytes to review these objects")
        return 2
    review = bool(hits or risky_paths or exposed_commits or attributed)
    print("scan complete: review required" if review else "scan complete: no pattern candidates")
    return 1 if review else 0


def exposed_identities(log):
    """(commits, distinct addresses) whose author or committer e-mail is not a GitHub noreply
    address. Only counted: an address is never printed."""
    commits, addresses = 0, set()
    for line in log.splitlines():
        fields = line.split(b"\0")
        if len(fields) != 2:
            raise ScanError("Git identity metadata is incomplete")
        exposed = {email for email in fields if NOREPLY_EMAIL.fullmatch(email) is None}
        commits += bool(exposed)
        addresses |= exposed
    return commits, len(addresses)


def attributed_messages(log):
    """How many commit messages carry an attribution trailer or an assistant's name; the
    messages themselves are never printed."""
    return sum(1 for message in log.split(b"\0") if ATTRIBUTION.search(message))


def positive_int(value):
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", type=Path, default=REPO, help="the clone to scan (default: this checkout)")
    parser.add_argument(
        "--max-blob-bytes",
        type=positive_int,
        default=MAX_BLOB_BYTES,
        help=f"per-blob memory budget (default {MAX_BLOB_BYTES:,}); larger blobs make the scan incomplete",
    )
    args = parser.parse_args(argv)
    try:
        return scan(args.repo, args.max_blob_bytes)
    except ScanError as error:
        print(f"scan incomplete: {error}", file=sys.stderr)
    except OSError, subprocess.SubprocessError, UnicodeError, MemoryError:
        # Native failures may contain private paths, stderr or command data.
        print("scan incomplete: local Git scan failed", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
