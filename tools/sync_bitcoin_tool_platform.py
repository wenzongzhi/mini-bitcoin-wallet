"""Synchronize the vendored bitcoin-tool Platform from a local Git checkout.

Only the Platform-owned ``btc``, ``network``, ``wallet``, and ``tx`` trees are
mirrored.  Content is read from the source repository's committed HEAD rather
than from arbitrary working-tree files, so the recorded commit always describes
the exact vendored source.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Iterable, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_RELATIVE_PATH = "tools/platform_upstream.json"
MANIFEST_PATH = PROJECT_ROOT / MANIFEST_RELATIVE_PATH
PLATFORM_PATHS = ("btc", "network", "wallet", "tx")


class PlatformSyncError(RuntimeError):
    """Raised when a source checkout cannot be synchronized safely."""


@dataclass(frozen=True)
class UpstreamRevision:
    """Identity of the local upstream checkout used for synchronization."""

    repository: str
    branch: str | None
    commit: str

    def as_manifest(self) -> dict[str, str | None]:
        return {
            "repository": self.repository,
            "branch": self.branch,
            "commit": self.commit,
        }


@dataclass(frozen=True)
class PlatformFile:
    """A committed Platform file and its Git object metadata."""

    relative_path: str
    object_id: str
    executable: bool


@dataclass(frozen=True)
class FileChange:
    """One add, modify, or delete required to mirror the upstream tree."""

    status: str
    relative_path: str


def _run_git(
    repository: Path,
    arguments: Sequence[str],
    *,
    text: bool = True,
) -> str | bytes:
    command = ["git", "-C", str(repository), *arguments]
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=text,
        )
    except FileNotFoundError as exc:
        raise PlatformSyncError("Git is required to synchronize the Platform.") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        detail = (stderr or "Git command failed.").strip()
        raise PlatformSyncError(detail) from exc
    return completed.stdout


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))


def _require_repository_root(path: Path, *, label: str) -> Path:
    candidate = path.expanduser().resolve()
    if not candidate.is_dir():
        raise PlatformSyncError(f"{label} does not exist or is not a directory: {candidate}")

    root_text = str(_run_git(candidate, ["rev-parse", "--show-toplevel"])).strip()
    repository_root = Path(root_text).resolve()
    if not _same_path(candidate, repository_root):
        raise PlatformSyncError(
            f"{label} must be the Git repository root: {repository_root}"
        )
    return repository_root


def _normalize_repository(remote: str) -> str:
    """Return ``owner/repository`` for common GitHub remote URL forms."""

    value = remote.strip().rstrip("/")
    github_match = re.match(
        r"^(?:https?://github\.com/|ssh://git@github\.com/|git@github\.com:)([^/]+/[^/]+)$",
        value,
        flags=re.IGNORECASE,
    )
    if github_match:
        value = github_match.group(1)
    if value.endswith(".git"):
        value = value[:-4]
    return value


def _upstream_revision(source: Path) -> UpstreamRevision:
    commit = str(_run_git(source, ["rev-parse", "HEAD"])).strip()
    branch = str(_run_git(source, ["branch", "--show-current"])).strip() or None
    try:
        remote = str(_run_git(source, ["remote", "get-url", "origin"])).strip()
    except PlatformSyncError as exc:
        raise PlatformSyncError(
            "The source checkout must have an 'origin' remote for the manifest."
        ) from exc
    return UpstreamRevision(
        repository=_normalize_repository(remote),
        branch=branch,
        commit=commit,
    )


def _ensure_platform_is_committed(source: Path) -> None:
    status = _run_git(
        source,
        [
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--",
            *PLATFORM_PATHS,
        ],
        text=False,
    )
    if status:
        raise PlatformSyncError(
            "The source Platform paths contain uncommitted changes. Commit or "
            "discard them before syncing so the manifest commit stays reproducible."
        )


def _platform_files(source: Path, commit: str) -> tuple[PlatformFile, ...]:
    raw_tree = _run_git(
        source,
        ["ls-tree", "-r", "-z", commit, "--", *PLATFORM_PATHS],
        text=False,
    )
    files: list[PlatformFile] = []
    for raw_entry in raw_tree.split(b"\0"):
        if not raw_entry:
            continue
        header, raw_path = raw_entry.split(b"\t", maxsplit=1)
        mode, object_type, object_id = header.decode("ascii").split()
        relative_path = raw_path.decode("utf-8")
        if object_type != "blob" or mode not in {"100644", "100755"}:
            raise PlatformSyncError(
                f"Unsupported Git entry in Platform allow-list: {relative_path} ({mode})"
            )
        files.append(
            PlatformFile(
                relative_path=relative_path,
                object_id=object_id,
                executable=mode == "100755",
            )
        )
    if not files:
        raise PlatformSyncError("The source commit contains no Platform files.")
    present_roots = {item.relative_path.split("/", maxsplit=1)[0] for item in files}
    missing_roots = sorted(set(PLATFORM_PATHS) - present_roots)
    if missing_roots:
        raise PlatformSyncError(
            "The source commit is missing required Platform paths: "
            + ", ".join(missing_roots)
        )
    return tuple(sorted(files, key=lambda item: item.relative_path))


def _blob_contents(source: Path, files: Iterable[PlatformFile]) -> dict[str, bytes]:
    return {
        platform_file.relative_path: _run_git(
            source,
            ["cat-file", "blob", platform_file.object_id],
            text=False,
        )
        for platform_file in files
    }


def _destination_files() -> set[str]:
    raw_files = _run_git(
        PROJECT_ROOT,
        [
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            *PLATFORM_PATHS,
        ],
        text=False,
    )
    relative_paths = {
        entry.decode("utf-8").replace("\\", "/")
        for entry in raw_files.split(b"\0")
        if entry
    }
    # ``git ls-files --cached`` also reports tracked paths already removed from
    # the working tree.  They are already synchronized deletions, not drift.
    return {
        relative_path
        for relative_path in relative_paths
        if (PROJECT_ROOT / relative_path).exists()
        or (PROJECT_ROOT / relative_path).is_symlink()
    }


def _manifest_bytes(revision: UpstreamRevision) -> bytes:
    document = json.dumps(revision.as_manifest(), indent=2, ensure_ascii=False)
    return f"{document}\n".encode("utf-8")


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _worktree_object_id(relative_path: str) -> str:
    """Hash a destination file using the mini repository's Git filters.

    Git may check a text blob out with CRLF on Windows even though the committed
    object uses LF.  Comparing the normalized object identity avoids reporting
    that platform-neutral line-ending conversion as source drift.
    """

    return str(
        _run_git(
            PROJECT_ROOT,
            ["hash-object", f"--path={relative_path}", "--", relative_path],
        )
    ).strip()


def _manifest_matches(revision: UpstreamRevision) -> bool:
    try:
        current = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        return False
    return current == revision.as_manifest()


def _required_changes(
    files: Sequence[PlatformFile],
    contents: Mapping[str, bytes],
    revision: UpstreamRevision,
) -> tuple[FileChange, ...]:
    expected_paths = set(contents)
    files_by_path = {item.relative_path: item for item in files}
    changes: list[FileChange] = []

    for relative_path in sorted(expected_paths):
        current = _read_bytes(PROJECT_ROOT / relative_path)
        if current is None:
            changes.append(FileChange("A", relative_path))
        elif (
            current != contents[relative_path]
            and _worktree_object_id(relative_path)
            != files_by_path[relative_path].object_id
        ):
            changes.append(FileChange("M", relative_path))

    for relative_path in sorted(_destination_files() - expected_paths):
        changes.append(FileChange("D", relative_path))

    if not MANIFEST_PATH.exists():
        changes.append(FileChange("A", MANIFEST_RELATIVE_PATH))
    elif not _manifest_matches(revision):
        changes.append(FileChange("M", MANIFEST_RELATIVE_PATH))

    return tuple(sorted(changes, key=lambda change: change.relative_path))


def _set_executable(path: Path, executable: bool) -> None:
    current_mode = path.stat().st_mode
    execute_bits = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    desired_mode = current_mode | execute_bits if executable else current_mode & ~execute_bits
    if desired_mode != current_mode:
        path.chmod(desired_mode)


def _write_platform(
    source: Path,
    files: Sequence[PlatformFile],
    contents: Mapping[str, bytes],
    revision: UpstreamRevision,
) -> None:
    expected_paths = set(contents)
    for relative_path in sorted(_destination_files() - expected_paths):
        destination = PROJECT_ROOT / relative_path
        if destination.is_file() or destination.is_symlink():
            destination.unlink()

    for platform_file in files:
        destination = PROJECT_ROOT / platform_file.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        expected = contents[platform_file.relative_path]
        if _read_bytes(destination) != expected:
            destination.write_bytes(expected)
        _set_executable(destination, platform_file.executable)

    MANIFEST_PATH.write_bytes(_manifest_bytes(revision))


def _print_revision(revision: UpstreamRevision) -> None:
    branch = revision.branch or "(detached HEAD)"
    print(f"Repository: {revision.repository}")
    print(f"Branch:     {branch}")
    print(f"Commit:     {revision.commit}")


def _print_changes(changes: Sequence[FileChange]) -> None:
    if not changes:
        print("No changed files.")
        return
    print(f"Changed files ({len(changes)}):")
    for change in changes:
        print(f"  {change.status} {change.relative_path}")


def synchronize(source_argument: Path, *, check: bool) -> int:
    _require_repository_root(PROJECT_ROOT, label="mini-bitcoin-wallet checkout")
    source = _require_repository_root(source_argument, label="bitcoin-tool source")
    if _same_path(source, PROJECT_ROOT):
        raise PlatformSyncError("Source and destination repositories must be different.")

    _ensure_platform_is_committed(source)
    revision = _upstream_revision(source)
    files = _platform_files(source, revision.commit)
    contents = _blob_contents(source, files)
    changes = _required_changes(files, contents, revision)

    _print_revision(revision)
    if check:
        if changes:
            print("Platform check failed: vendored files or manifest are out of date.")
            _print_changes(changes)
            return 1
        print("Platform check passed: vendored files and manifest match upstream.")
        return 0

    _write_platform(source, files, contents, revision)
    _print_changes(changes)
    print("Platform synchronization complete.")
    return 0


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Synchronize vendored bitcoin-tool Platform sources.",
    )
    parser.add_argument(
        "--source",
        required=True,
        type=Path,
        help="Path to the root of a local bitcoin-tool Git checkout.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify files and manifest without changing the mini-wallet checkout.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _argument_parser().parse_args(argv)
    try:
        return synchronize(arguments.source, check=arguments.check)
    except PlatformSyncError as exc:
        print(f"Platform sync error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
