"""Approval coverage for destructive remote-ref mutations (no remote writes)."""

import pytest

from tools.approval import detect_dangerous_command


@pytest.mark.parametrize("command", [
    "git push origin --delete feature",
    "git push --delete origin refs/heads/feature",
    "git push -d origin feature",
    "git push origin -d feature",
    "git push -vd origin feature",
    "git push origin -dv feature",
    "git push --dry-run --delete origin feature",
])
def test_push_delete_flags_require_approval(command):
    dangerous, key, description = detect_dangerous_command(command)
    assert dangerous is True
    assert key == description == "git push deleting a remote ref (rewrites remote history)"


@pytest.mark.parametrize("command", [
    "git push origin :feature",
    "git push origin :refs/heads/x",
    "git push origin ':refs/tags/v1'",
    'git push origin ":refs/heads/x"',
    "git push origin main :feature",
])
def test_empty_source_refspecs_require_approval(command):
    dangerous, key, description = detect_dangerous_command(command)
    assert dangerous is True
    assert key == description == "git push deleting a remote ref (rewrites remote history)"


@pytest.mark.parametrize(("command", "description"), [
    ("git push origin +branch", "git push force refspec (rewrites remote history)"),
    ("git push origin +HEAD:main", "git push force refspec (rewrites remote history)"),
    ("git push origin '+HEAD:refs/heads/main'", "git push force refspec (rewrites remote history)"),
    ("git push --mirror origin", "git push --mirror (rewrites and deletes remote refs)"),
    ("git push origin --mirror", "git push --mirror (rewrites and deletes remote refs)"),
    ("git push --prune origin", "git push --prune (deletes remote refs)"),
    ("git push origin --prune", "git push --prune (deletes remote refs)"),
])
def test_other_destructive_push_forms_require_approval(command, description):
    dangerous, key, actual_description = detect_dangerous_command(command)
    assert dangerous is True
    assert key == actual_description == description


@pytest.mark.parametrize("command", [
    "gh api -X DELETE repos/o/r/git/refs/heads/x",
    "gh api repos/o/r/git/refs/heads/x -X DELETE",
    "gh api --method DELETE /repos/o/r/git/refs/tags/v1",
    "gh api /repos/o/r/git/refs/heads/x --method=DELETE",
    "gh api -XDELETE repos/o/r/git/refs/heads/x",
    'gh api -X "DELETE" "repos/o/r/git/refs/heads/x"',
])
def test_api_ref_deletions_require_approval(command):
    dangerous, key, description = detect_dangerous_command(command)
    assert dangerous is True
    assert key == description == "gh api deleting a remote ref (rewrites remote history)"


@pytest.mark.parametrize("command", [
    "gh api -X PATCH repos/o/r/git/refs/heads/x -F force=true -f sha=abc",
    "gh api repos/o/r/git/refs/heads/x -F force=true -X PATCH",
    "gh api --method PATCH repos/o/r/git/refs/heads/x --field force=true",
    "gh api repos/o/r/git/refs/heads/x --method=PATCH --field=force=true",
    'gh api -X PATCH "repos/o/r/git/refs/heads/x" -F "force=true"',
])
def test_api_force_ref_updates_require_approval(command):
    dangerous, key, description = detect_dangerous_command(command)
    assert dangerous is True
    assert key == description == "gh api force updating a remote ref (rewrites remote history)"


@pytest.mark.parametrize("command", [
    "gh api -X PATCH repos/o/r/git/refs/heads/x -F force=false",
    "gh api -X PATCH repos/o/r/git/refs/heads/x -f sha=abc",
    "gh api -X DELETE repos/o/r/issues/1",
    "gh api -X GET repos/o/r/git/refs/heads/x",
    "gh api repos/o/r/git/refs/heads/x; echo -X DELETE",
    "gh api -X DELETE repos/o/r/issues/1 && echo repos/o/r/git/refs/heads/x",
    "gh api -X PATCH repos/o/r/git/refs/heads/x | echo force=true",
    "gh api -X PATCH repos/o/r/git/refs/heads/x\necho force=true",
    "gh api -X PATCH repos/o/r/git/refs/heads/x\nforce=true",
    "gh api -X PATCH repos/o/r/git/refs/heads/x -F force=trueish",
    "gh api -X PATCH repos/o/r/git/refs/heads/x -F myforce=true",
    "gh api -X GET repos/o/r/git/refs/heads/x -F force=true",
])
def test_other_api_requests_stay_safe(command):
    assert detect_dangerous_command(command) == (False, None, None)


@pytest.mark.parametrize("payload", [
    "git push --force",
    "git push --delete",
    "git push -d origin feature",
    "git push origin :feature",
    "git push origin +HEAD:main",
    "git push --mirror",
    "git push --prune",
    "gh api -X DELETE repos/o/r/git/refs/heads/x",
    "gh api -X PATCH repos/o/r/git/refs/heads/x -F force=true",
])
def test_quoted_prose_matches_existing_force_push_policy(payload):
    # These unanchored heuristics conservatively gate prose too; do not lower the existing gate.
    assert detect_dangerous_command(f'git commit -m "use {payload}"')[0] is True


@pytest.mark.parametrize("separator", [";", "&&", "|", "\n"])
@pytest.mark.parametrize("later_argument", [
    "--delete feature", "-vd feature", ":feature", "+HEAD:main", "--mirror", "--prune",
])
def test_push_rules_do_not_cross_command_segments(separator, later_argument):
    command = f"git push origin main {separator} echo {later_argument}"
    assert detect_dangerous_command(command) == (False, None, None)


@pytest.mark.parametrize(("command", "dangerous"), [
    ("git push origin --delete feature", True),
    ("git push origin :feature", True),
    ("git push origin +HEAD:main", True),
    ("git push --mirror origin", True),
    ("git push --prune origin", True),
    ("gh api -X DELETE repos/o/r/git/refs/heads/x", True),
    ("gh api -X PATCH repos/o/r/git/refs/heads/x -F force=true", True),
    ("git push -u origin feature", False),
    ("gh api repos/o/r/git/refs/heads/x", False),
])
def test_remote_ref_mutations_reach_manual_approval(tmp_path, monkeypatch, command, dangerous):
    from hermes_cli import config
    from tools.approval import check_all_command_guards

    home = tmp_path / "hermes"
    home.mkdir()
    (home / "config.yaml").write_text("approvals:\n  mode: manual\ncommand_allowlist: []\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    monkeypatch.setenv("HERMES_SESSION_KEY", "remote-ref-approval-test")
    config._LOAD_CONFIG_CACHE.clear()
    prompts = []

    def refuse(*args, **kwargs):
        prompts.append((args, kwargs))
        return "deny"

    try:
        decision = check_all_command_guards(command, "local", approval_callback=refuse)
        assert decision["approved"] is not dangerous
        assert len(prompts) == int(dangerous)
        if dangerous:
            assert decision["outcome"] == "denied"
    finally:
        config._LOAD_CONFIG_CACHE.clear()


@pytest.mark.parametrize("prefix", [
    "git -C repo", "git -Crepo", "git -c core.bare=false",
    "git --git-dir=repo/.git", "git --work-tree repo", "git --no-pager",
    "git -C 'repo dir' -c core.bare=false",
])
@pytest.mark.parametrize("arguments", [
    "origin --delete feature", "origin -vd feature", "origin :feature",
    "origin +HEAD:main", "--mirror origin", "--prune origin",
])
def test_git_global_options_do_not_hide_remote_ref_mutations(prefix, arguments):
    dangerous, key, description = detect_dangerous_command(f"{prefix} push {arguments}")
    assert dangerous is True
    assert key == description
    assert description.startswith("git push ")


def test_delete_equals_spelling_is_conservatively_gated():
    assert detect_dangerous_command("git push origin --delete=feature")[0] is True


def test_remote_ref_patterns_finish_on_long_nonmatching_arguments():
    import subprocess
    import sys

    # A subprocess deadline can interrupt a regex holding the GIL and reaps the child on failure.
    code = """
from tools.approval_detection import DANGEROUS_PATTERNS_COMPILED
rules = [pattern for pattern, description in DANGEROUS_PATTERNS_COMPILED
         if description.startswith(('git push ', 'gh api '))]
inputs = [
    'git push ' + 'a' * 20000,
    'git ' + '-C repo ' * 2000 + 'status',
    'git push ' + ' ' * 20000 + 'main',
    'gh api ' + 'a' * 20000,
    'gh api -X PATCH ' + 'repos/o/r/git/refs/heads/x ' * 500,
]
for text in inputs:
    assert not any(pattern.search(text) for pattern in rules)
"""
    subprocess.run([sys.executable, "-c", code], check=True, timeout=10, capture_output=True)


@pytest.mark.parametrize("command", [
    "git push",
    "git push -u origin feature",
    "git push origin HEAD:refs/heads/x",
    "git push origin :",
    "git push --dry-run origin main",
    "gh api repos/o/r/git/refs/heads/x",
    "git push origin feature-delete",
    "git push origin v1.0+build",
    "git push origin main+",
    "git push origin 'HEAD:refs/heads/x'",
    "git push origin ':'",
    'git push origin ":"',
    "git push origin --mirror-other",
    "git push origin --prune-other",
    "git push origin --delete-other",
    "git push origin -D feature",
    "git -C repo push -u origin feature",
    "git --no-pager push origin HEAD:refs/heads/x",
    "git -C repo push origin :",
    "git -C repo push --dry-run origin main",
    "git -C push status",
    "git -C repo; echo push origin :feature",
    "git push --dry-run origin main; echo --delete feature",
    "git push origin main && echo -d",
    "git push origin main | echo -vd",
    "git push origin main\necho --delete feature",
])
def test_ordinary_pushes_and_unrelated_delete_flags_stay_safe(command):
    assert detect_dangerous_command(command) == (False, None, None)
