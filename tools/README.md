# bitcoin-tool Platform Sync

`sync_bitcoin_tool_platform.py` mirrors the committed Platform code from a
local `bitcoin-tool` Git checkout into `mini-bitcoin-wallet`.

It synchronizes only these directories:

```text
btc/
network/
wallet/
tx/
```

Tests, CLI code, documentation, and build files are not copied. The script also
updates `tools/platform_upstream.json` with the source repository, branch, and
commit.

## Safety rules

- The script reads the source repository's committed `HEAD`, not arbitrary
  working-tree files.
- Commit all changes inside the four Platform directories before syncing.
  Uncommitted files elsewhere in `bitcoin-tool` do not block the operation.
- Syncing is a mirror operation. Files under the Mini Platform directories that
  do not exist upstream may be deleted.
- Keep Mini-specific code outside those directories, such as in `adapters/` or
  `wallet_core/`.
- Review or save any local Mini Platform changes before running a real sync.

The source checkout must be the `bitcoin-tool` repository root and must have an
`origin` remote.

## Recommended workflow

The examples below assume:

```text
E:\github\bitcoin-tool
E:\github\mini-bitcoin-wallet
```

### 1. Test and commit bitcoin-tool

```powershell
cd E:\github\bitcoin-tool
git status --short -- btc network wallet tx
python -m pytest -q

git add btc network wallet tx tests
git commit -m "Update bitcoin-tool platform"
```

Staging files is not enough. The Platform changes must be included in `HEAD`.

### 2. Check the expected Mini changes

```powershell
cd E:\github\mini-bitcoin-wallet
python .\tools\sync_bitcoin_tool_platform.py `
  --source E:\github\bitcoin-tool `
  --check
```

`--check` never writes files. Exit code `1` is expected when the selected
bitcoin-tool commit has not been synchronized yet.

### 3. Synchronize

```powershell
python .\tools\sync_bitcoin_tool_platform.py `
  --source E:\github\bitcoin-tool
```

The output shows the source revision and every added, modified, or deleted file.

### 4. Verify and test

```powershell
python .\tools\sync_bitcoin_tool_platform.py `
  --source E:\github\bitcoin-tool `
  --check

git status --short
git diff --check
python -m pytest -q
```

A successful check prints:

```text
Platform check passed: vendored files and manifest match upstream.
```

Commit the four Platform directories and the manifest together:

```powershell
git add btc network wallet tx tools\platform_upstream.json
git commit -m "Sync bitcoin-tool platform"
```

## Updating from the remote repository

The sync script does not fetch, pull, or clone repositories. Update the local
bitcoin-tool checkout first:

```powershell
git -C E:\github\bitcoin-tool fetch origin
git -C E:\github\bitcoin-tool pull --ff-only
git -C E:\github\bitcoin-tool branch --show-current
git -C E:\github\bitcoin-tool rev-parse HEAD
```

Then run the check, sync, and final check from the workflow above. A detached
`HEAD` is supported; the manifest records its branch as `null`.

## Running from another directory

The script locates the Mini repository from its own path, so it can run from any
working directory:

```powershell
python E:\github\mini-bitcoin-wallet\tools\sync_bitcoin_tool_platform.py `
  --source E:\github\bitcoin-tool
```

Quote paths that contain spaces.

## Arguments and exit codes

```text
python sync_bitcoin_tool_platform.py --source PATH [--check]
```

| Argument | Description |
| --- | --- |
| `--source PATH` | Required path to the local bitcoin-tool repository root. |
| `--check` | Verify files and manifest without modifying Mini. |
| `-h`, `--help` | Show command help. |

| Exit code | Meaning |
| --- | --- |
| `0` | Sync completed, or the check passed. |
| `1` | The check found source drift. |
| `2` | Invalid arguments, repository state, or Git configuration. |

File statuses are `A` for add, `M` for modify, and `D` for delete.

## Common errors

- **Platform paths contain uncommitted changes:** commit or discard changes in
  `btc/`, `network/`, `wallet/`, and `tx/` in bitcoin-tool.
- **Source must be the Git repository root:** pass the bitcoin-tool root, not a
  subdirectory such as `wallet/`.
- **Missing `origin` remote:** inspect the source with `git remote -v` and
  configure the correct upstream remote.
- **Missing required Platform paths:** switch bitcoin-tool to the intended
  branch or commit.
- **Check still fails after syncing:** confirm both commands used the same
  source path and inspect `git rev-parse HEAD` plus
  `tools/platform_upstream.json`.
