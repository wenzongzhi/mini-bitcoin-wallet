"""Tests for deterministic bitcoin-tool Platform vendoring."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys


PROJECT_ROOT = Path(__file__).parents[1]
SYNC_SCRIPT = PROJECT_ROOT / "tools" / "sync_bitcoin_tool_platform.py"


def _run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True)


def _git(repository: Path, *arguments: str) -> str:
    completed = _run(["git", *arguments], cwd=repository)
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def _create_source_checkout(root: Path) -> tuple[Path, str]:
    source = root / "bitcoin-tool"
    source.mkdir()
    _git(source, "init", "-b", "feature/refactor-wallet-data-format")
    _git(source, "config", "user.name", "Platform Sync Test")
    _git(source, "config", "user.email", "platform-sync@example.invalid")
    _git(source, "remote", "add", "origin", "https://github.com/example/bitcoin-tool.git")

    for directory in ("btc", "network", "wallet", "tx"):
        platform_directory = source / directory
        platform_directory.mkdir()
        (platform_directory / "__init__.py").write_text(
            f'"""{directory} Platform package."""\n',
            encoding="utf-8",
        )
    (source / "wallet" / "service.py").write_text("PLATFORM_VERSION = 1\n", encoding="utf-8")
    (source / "not_platform.py").write_text("MUST_NOT_BE_COPIED = True\n", encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "Create test Platform")
    return source, _git(source, "rev-parse", "HEAD")


def _create_product_checkout(root: Path) -> Path:
    product = root / "mini-bitcoin-wallet"
    (product / "tools").mkdir(parents=True)
    shutil.copy2(SYNC_SCRIPT, product / "tools" / SYNC_SCRIPT.name)
    (product / "adapters").mkdir()
    (product / "adapters" / "product.py").write_text("PRODUCT = True\n", encoding="utf-8")
    (product / "wallet").mkdir()
    (product / "wallet" / "obsolete.py").write_text("OLD = True\n", encoding="utf-8")

    _git(product, "init", "-b", "master")
    _git(product, "config", "user.name", "Platform Sync Test")
    _git(product, "config", "user.email", "platform-sync@example.invalid")
    _git(product, "add", ".")
    _git(product, "commit", "-m", "Create test product")
    return product


def _sync(product: Path, source: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return _run(
        [
            sys.executable,
            str(product / "tools" / SYNC_SCRIPT.name),
            "--source",
            str(source),
            *arguments,
        ],
        cwd=product,
    )


def test_sync_mirrors_only_allow_list_and_records_exact_revision(tmp_path: Path) -> None:
    source, commit = _create_source_checkout(tmp_path)
    product = _create_product_checkout(tmp_path)
    # An unrelated local note is not part of the Platform and must not block or
    # leak into synchronization.
    (source / "developer-notes.txt").write_text("local only\n", encoding="utf-8")

    completed = _sync(product, source)

    assert completed.returncode == 0, completed.stderr
    assert (product / "wallet" / "service.py").read_text(encoding="utf-8") == (
        "PLATFORM_VERSION = 1\n"
    )
    assert not (product / "wallet" / "obsolete.py").exists()
    assert (product / "adapters" / "product.py").exists()
    assert not (product / "not_platform.py").exists()
    assert json.loads(
        (product / "tools" / "platform_upstream.json").read_text(encoding="utf-8")
    ) == {
        "repository": "example/bitcoin-tool",
        "branch": "feature/refactor-wallet-data-format",
        "commit": commit,
    }
    assert "D wallet/obsolete.py" in completed.stdout
    assert "A wallet/service.py" in completed.stdout

    checked = _sync(product, source, "--check")
    assert checked.returncode == 0, checked.stderr
    assert "Platform check passed" in checked.stdout


def test_check_detects_vendored_drift_but_ignores_product_files(tmp_path: Path) -> None:
    source, _commit = _create_source_checkout(tmp_path)
    product = _create_product_checkout(tmp_path)
    assert _sync(product, source).returncode == 0

    (product / "adapters" / "product.py").write_text("PRODUCT = 'changed'\n", encoding="utf-8")
    assert _sync(product, source, "--check").returncode == 0

    (product / "wallet" / "service.py").write_text("PLATFORM_VERSION = 99\n", encoding="utf-8")
    checked = _sync(product, source, "--check")
    assert checked.returncode == 1
    assert "M wallet/service.py" in checked.stdout


def test_sync_rejects_uncommitted_upstream_platform_changes(tmp_path: Path) -> None:
    source, _commit = _create_source_checkout(tmp_path)
    product = _create_product_checkout(tmp_path)
    (source / "wallet" / "service.py").write_text("PLATFORM_VERSION = 2\n", encoding="utf-8")

    completed = _sync(product, source)

    assert completed.returncode == 2
    assert "uncommitted changes" in completed.stderr
    assert not (product / "tools" / "platform_upstream.json").exists()


def test_committed_upstream_manifest_has_pinned_commit() -> None:
    manifest = json.loads(
        (PROJECT_ROOT / "tools" / "platform_upstream.json").read_text(
            encoding="utf-8"
        )
    )

    assert manifest["repository"] == "wenzongzhi/bitcoin-tool"
    assert manifest["branch"] == "feature/refactor-wallet-data-format"
    assert len(manifest["commit"]) == 40
    int(manifest["commit"], 16)
