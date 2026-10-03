"""Publish an existing, gated release tag after synchronizing the optional local mirror.

Run from the development checkout after its release commit is merged into origin/main:
    uv run --frozen python scripts/publish-release.py v1.22.17

No force pushes, history changes, new commits or tags. Git's pre-push gate still applies.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?")


class PublishError(RuntimeError):
    pass


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=False)
    if result.returncode:
        raise PublishError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _verify_remote(root: Path, remote: str, expected: dict[str, str]) -> None:
    rows = git(root, "ls-remote", "--refs", remote, *expected).splitlines()
    actual = {ref: oid for oid, ref in (row.split() for row in rows)}
    if actual != expected:
        raise PublishError(f"{remote}: release references were not confirmed after push")


def publish(tag: str, *, root: Path = ROOT) -> None:
    if not TAG.fullmatch(tag):
        raise PublishError("use a release tag such as v1.22.17")
    tag_ref = f"refs/tags/{tag}"
    if git(root, "cat-file", "-t", tag_ref) != "tag":
        raise PublishError("the release tag must be annotated")
    tag_oid = git(root, "rev-parse", tag_ref)
    commit = git(root, "rev-parse", f"{tag_ref}^{{commit}}")
    version = tomllib.loads(git(root, "show", f"{tag_ref}:pyproject.toml"))["project"]["version"]
    if tag != f"v{version}":
        raise PublishError("the release tag does not match its package version")
    git(root, "fetch", "--no-tags", "origin", "main")
    main = git(root, "rev-parse", "refs/remotes/origin/main")
    git(root, "merge-base", "--is-ancestor", commit, main)
    if "backup" in git(root, "remote").splitlines():
        # Atomic and fast-forward only: neither an unavailable mirror nor a divergent branch
        # can leave a published tag that the runtime's local origin cannot fetch.
        git(root, "push", "--atomic", "backup", f"{main}:refs/heads/main", f"{tag_ref}:{tag_ref}")
        _verify_remote(root, "backup", {"refs/heads/main": main, tag_ref: tag_oid})
    git(root, "push", "origin", f"{tag_ref}:{tag_ref}")
    _verify_remote(root, "origin", {tag_ref: tag_oid})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    args = parser.parse_args()
    try:
        publish(args.tag)
    except (PublishError, KeyError, ValueError) as exc:
        print(f"release not confirmed: {exc}")
        return 1
    print(f"release tag {args.tag} confirmed on origin and the configured mirror")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
