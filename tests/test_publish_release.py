"""Release publication must synchronize the runtime's optional mirror before GitHub."""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "publish-release.py"


@pytest.fixture
def publisher(monkeypatch):
    spec = importlib.util.spec_from_file_location("publish_release_under_test", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    tag_ref = "refs/tags/v1.2.3"
    replies = {
        ("cat-file", "-t", tag_ref): "tag",
        ("rev-parse", tag_ref): "a" * 40,
        ("rev-parse", f"{tag_ref}^{{commit}}"): "b" * 40,
        ("rev-parse", "refs/remotes/origin/main"): "c" * 40,
        ("show", f"{tag_ref}:pyproject.toml"): '[project]\nversion="1.2.3"\n',
        ("remote",): "origin\nbackup",
        ("ls-remote", "--refs", "backup", "refs/heads/main", tag_ref): (
            f"{'c' * 40}\trefs/heads/main\n{'a' * 40}\t{tag_ref}"
        ),
        ("ls-remote", "--refs", "origin", tag_ref): f"{'a' * 40}\t{tag_ref}",
    }

    def git(_root, *args):
        calls.append(args)
        reply = replies.get(args, "")
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(module, "git", git)
    return module, calls, replies


def test_mirror_push_and_readback_precede_publication(publisher):
    module, calls, _replies = publisher
    module.publish("v1.2.3")
    pushes = [args for args in calls if args[0] == "push"]
    assert pushes == [
        ("push", "--atomic", "backup", f"{'c' * 40}:refs/heads/main", "refs/tags/v1.2.3:refs/tags/v1.2.3"),
        ("push", "origin", "refs/tags/v1.2.3:refs/tags/v1.2.3"),
    ]
    readback = ("ls-remote", "--refs", "backup", "refs/heads/main", "refs/tags/v1.2.3")
    assert calls.index(readback) < calls.index(pushes[1])


@pytest.mark.parametrize("stage", ["push", "readback"])
def test_an_unavailable_or_unconfirmed_mirror_stops_publication(publisher, stage):
    module, calls, replies = publisher
    if stage == "push":
        replies[("push", "--atomic", "backup", f"{'c' * 40}:refs/heads/main", "refs/tags/v1.2.3:refs/tags/v1.2.3")] = (
            module.PublishError("mirror unavailable")
        )
    else:
        replies[("ls-remote", "--refs", "backup", "refs/heads/main", "refs/tags/v1.2.3")] = ""
    with pytest.raises(module.PublishError):
        module.publish("v1.2.3")
    assert not any(args[:2] == ("push", "origin") for args in calls)


def test_a_checkout_without_a_mirror_can_publish(publisher):
    module, calls, replies = publisher
    replies[("remote",)] = "origin"
    module.publish("v1.2.3")
    assert not any("backup" in args for args in calls)
    assert ("push", "origin", "refs/tags/v1.2.3:refs/tags/v1.2.3") in calls


@pytest.mark.parametrize("reason", ["lightweight", "version", "not_merged"])
def test_an_invalid_release_never_reaches_a_remote(publisher, reason):
    module, calls, replies = publisher
    if reason == "lightweight":
        replies[("cat-file", "-t", "refs/tags/v1.2.3")] = "commit"
    elif reason == "version":
        replies[("show", "refs/tags/v1.2.3:pyproject.toml")] = '[project]\nversion="1.2.4"'
    else:
        replies[("merge-base", "--is-ancestor", "b" * 40, "c" * 40)] = module.PublishError("not merged")
    with pytest.raises(module.PublishError):
        module.publish("v1.2.3")
    assert not any(args[0] == "push" for args in calls)


@pytest.mark.parametrize("tag", ["--all", "main", "v1.2.3;bad", "v1.2.3\nmain", "refs/tags/v1.2.3"])
def test_invalid_tag_names_do_not_even_call_git(publisher, tag):
    module, calls, _replies = publisher
    with pytest.raises(module.PublishError):
        module.publish(tag)
    assert calls == []


def test_origin_readback_is_required_for_success(publisher):
    module, _calls, replies = publisher
    replies[("ls-remote", "--refs", "origin", "refs/tags/v1.2.3")] = ""
    with pytest.raises(module.PublishError, match="not confirmed"):
        module.publish("v1.2.3")
