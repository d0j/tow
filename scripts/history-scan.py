"""Before publishing the repository: scan every blob ever committed for secrets and private
data. Prints categories, paths and counts only - never the matched values.

    uv run python scripts/history-scan.py
"""

import collections
import re
import subprocess
from pathlib import Path

REPO = str(Path(__file__).resolve().parents[1])
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
    "email": re.compile(
        rb"[A-Za-z0-9._%+-]+@(?!users\.noreply\.github\.com|example\.)[A-Za-z0-9.-]+\.[a-z]{2,}"
    ),
}

objects = subprocess.run(
    ["git", "-C", REPO, "rev-list", "--objects", "--all"], capture_output=True, text=True, check=True
).stdout
paths = {}
for line in objects.splitlines():
    sha, _, path = line.partition(" ")
    if path:
        paths.setdefault(sha, set()).add(path)
hits = collections.defaultdict(lambda: collections.defaultdict(int))
risky_paths = set()
for sha, names in paths.items():
    kind = subprocess.run(
        ["git", "-C", REPO, "cat-file", "-t", sha], capture_output=True, text=True, check=False
    ).stdout.strip()
    if kind != "blob":
        continue
    for name in names:
        low = name.lower()
        if re.search(
            r"(^|/)(data/|config\.yaml$|secrets|master\.key|\.coverage$|lan-auth|tow\.jsonl|state\.json|download_history)",
            low,
        ):
            risky_paths.add(name)
    blob = subprocess.run(["git", "-C", REPO, "cat-file", "-p", sha], capture_output=True, check=False).stdout
    if len(blob) > 5_000_000:
        continue
    for label, pattern in PATTERNS.items():
        count = len(pattern.findall(blob))
        if count:
            for name in names:
                hits[label][name] += count
print("blobs scanned:", sum(1 for _ in paths))
print("runtime/secret-looking paths ever committed:", sorted(risky_paths) or "none")
for label, files in hits.items():
    top = sorted(files.items(), key=lambda kv: -kv[1])[:8]
    print(f"\n{label}: {sum(files.values())} matches in {len(files)} paths")
    for name, count in top:
        print(f"   {count:4} {name}")
authors = subprocess.run(
    ["git", "-C", REPO, "log", "--all", "--format=%an <%ae>"], capture_output=True, text=True, check=False
).stdout
print("\nauthors:", collections.Counter(authors.splitlines()).most_common())
