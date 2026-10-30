"""Cut a release: bump the version via PR, tag the merged commit, then watch the build and publish the draft.

Interactive; re-running after an interruption picks up where it left off (an already-bumped main skips the PR).
See .github/workflows/release.yml for what the tag push triggers.

    just release 0.3.1
    just release 0.4.0-rc.1
"""

import argparse
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(-rc\.\d+)?$")
PYPROJECT_VERSION_RE = re.compile(r'^version = "([^"]*)"', re.MULTILINE)


class ReleaseError(Exception):
    pass


def run(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def run_visible(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(args, cwd=cwd, check=True)


def pyproject_version(text: str) -> str:
    match = PYPROJECT_VERSION_RE.search(text)
    if match is None:
        raise ReleaseError("no [project] version line found in pyproject.toml")
    return match.group(1)


def confirm(prompt: str) -> bool:
    return input(f"{prompt} [y/N] ").strip().lower() == "y"


def check_tag_is_new(tag: str) -> None:
    if run("git", "tag", "--list", tag) or run("git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}"):
        raise ReleaseError(f"tag {tag} already exists")


def open_bump_pr(worktree: Path, tag: str, pep440: str) -> str:
    branch = f"release/{tag}"
    existing = run(
        "gh", "pr", "list", "--head", branch, "--state", "open", "--json", "url", "--jq", ".[0].url // empty"
    )
    if existing:
        print(f"\nfound open PR {existing}")
        return existing
    pyproject = worktree / "pyproject.toml"
    run("git", "switch", "-C", branch, cwd=worktree)
    pyproject.write_text(PYPROJECT_VERSION_RE.sub(f'version = "{pep440}"', pyproject.read_text(), count=1))
    run("git", "commit", "-am", f"Release {tag}", cwd=worktree)
    run_visible("git", "push", "--force", "-u", "origin", branch, cwd=worktree)
    url = run(
        "gh", "pr", "create", "--base", "main", "--head", branch,
        "--title", f"Release {tag}", "--body", f"Bump the version to {pep440} for the {tag} release.",
        cwd=worktree,
    )  # fmt: skip
    print(f"\nopened {url}")
    return url


def bump_via_pr(worktree: Path, tag: str, pep440: str) -> None:
    url = open_bump_pr(worktree, tag, pep440)
    while True:
        input("merge the PR, then press enter to continue... ")
        state = run("gh", "pr", "view", url, "--json", "state", "--jq", ".state")
        if state == "MERGED":
            return
        if state == "CLOSED":
            raise ReleaseError(f"{url} was closed without merging")
        print(f"PR is still {state}")


def prepare_main(tag: str, pep440: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        worktree = Path(tmp) / "release"
        run("git", "worktree", "add", "--detach", str(worktree), "origin/main")
        try:
            print("checking external pins for drift (the release workflow blocks on this)...")
            pin_check = subprocess.run([sys.executable, str(worktree / "scripts" / "check_pin_drift.py"), "--strict"])
            if pin_check.returncode != 0:
                raise ReleaseError("stale external pins on main; bump them (or re-pin as-is) first")
            current = pyproject_version((worktree / "pyproject.toml").read_text())
            if current == pep440:
                print(f"main is already at {pep440}; skipping the version bump PR")
            else:
                print(f"main is at {current}; opening a PR to bump it to {pep440}")
                bump_via_pr(worktree, tag, pep440)
        finally:
            run("git", "worktree", "remove", "--force", str(worktree))


def push_tag(tag: str, pep440: str) -> None:
    run("git", "fetch", "origin", "main")
    sha = run("git", "rev-parse", "origin/main")
    main_version = pyproject_version(run("git", "show", f"{sha}:pyproject.toml"))
    if main_version != pep440:
        raise ReleaseError(f"origin/main is at {main_version}, expected {pep440}")
    print(f"\norigin/main: {run('git', 'log', '-1', '--format=%h %s', sha)}")
    if not confirm(f"push tag {tag} at this commit? this starts the release"):
        raise ReleaseError("aborted before tagging")
    run("git", "tag", tag, sha)
    run_visible("git", "push", "origin", tag)


def find_release_run(tag: str) -> str:
    for _ in range(30):
        run_id = run(
            "gh", "run", "list", "--workflow", "release.yml", "--branch", tag, "--limit", "1",
            "--json", "databaseId", "--jq", ".[0].databaseId // empty",
        )  # fmt: skip
        if run_id:
            return run_id
        time.sleep(2)
    raise ReleaseError(f"no release workflow run showed up for {tag}")


def watch_and_publish(tag: str) -> None:
    run_id = find_release_run(tag)
    print(f"release workflow: {run('gh', 'run', 'view', run_id, '--json', 'url', '--jq', '.url')}")
    resume = f"gh run watch {run_id} --exit-status && gh release edit {tag} --draft=false"
    if not confirm("watch the build now? it takes up to ~90 min"):
        print(f"to finish later: {resume}")
        return
    if subprocess.run(["gh", "run", "watch", run_id, "--exit-status"]).returncode != 0:
        raise ReleaseError(f"release workflow failed; see `gh run view {run_id} --log-failed`")
    print(f"\ndraft release: {run('gh', 'release', 'view', tag, '--json', 'url', '--jq', '.url')}")
    if confirm("review the notes there; publish it now? hosts will start auto-updating"):
        run_visible("gh", "release", "edit", tag, "--draft=false")
    else:
        print(f"to publish later: gh release edit {tag} --draft=false")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("version", help="X.Y.Z or X.Y.Z-rc.N (no leading v)")
    version: str = parser.parse_args().version
    if not VERSION_RE.match(version):
        parser.error(f"bad version {version!r}; expected X.Y.Z or X.Y.Z-rc.N")
    tag = f"v{version}"
    pep440 = version.replace("-rc.", "rc")

    try:
        run("gh", "auth", "status")
        run("git", "fetch", "origin", "main", "--tags")
        check_tag_is_new(tag)
        prepare_main(tag, pep440)
        push_tag(tag, pep440)
        watch_and_publish(tag)
    except ReleaseError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        print(f"error: {' '.join(e.cmd)} failed\n{e.stderr or ''}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
