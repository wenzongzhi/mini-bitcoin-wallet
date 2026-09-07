"""
Copyright 2026 温中志 (Wen Zhongzhi)

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock, Timeout

from btc.chainparams import NETWORK_MAINNET, NETWORK_TESTNET4
from .wallet import WalletError, default_data_dir


WALLET_CACHE_FILENAMES = {
    NETWORK_MAINNET: "wallet_cache.json",
    NETWORK_TESTNET4: "wallet_cache_testnet4.json",
}
CACHE_VERSION = 2


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_wallet_cache_file(
    data_dir: str | Path | None = None,
    network: str = NETWORK_MAINNET,
) -> Path:
    try:
        filename = WALLET_CACHE_FILENAMES[network]
    except (KeyError, TypeError) as exc:
        raise WalletError(f"unsupported wallet network: {network}") from exc
    return default_data_dir(data_dir) / filename


@contextmanager
def locked_cache_file(cache_file: Path):
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        lock = FileLock(f"{cache_file}.lock")
        with lock.acquire(timeout=10):
            yield
    except Timeout as exc:
        raise WalletError(f'wallet cache file is busy: "{cache_file}"') from exc
    except OSError as exc:
        raise WalletError(f'cannot lock wallet cache file "{cache_file}": {exc}') from exc


def load_wallet_cache(cache_file: Path) -> dict:
    try:
        with cache_file.open("r", encoding="utf-8") as file:
            cache = json.load(file)
    except FileNotFoundError:
        return {"version": CACHE_VERSION, "wallets": {}}
    except (OSError, json.JSONDecodeError) as exc:
        raise WalletError(f'cannot read wallet cache file "{cache_file}": {exc}') from exc

    if not isinstance(cache, dict) or cache.get("version") != CACHE_VERSION:
        raise WalletError(f'invalid wallet cache file "{cache_file}"')
    wallets = cache.get("wallets")
    if not isinstance(wallets, dict):
        raise WalletError(f'invalid wallet cache file "{cache_file}"')
    return cache


def save_wallet_cache(cache: dict, cache_file: Path) -> None:
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=cache_file.parent,
            prefix=f".{cache_file.name}.",
            suffix=".tmp",
            delete=False,
        ) as file:
            temporary_path = Path(file.name)
            json.dump(cache, file, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, cache_file)
    except OSError as exc:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise WalletError(f'cannot write wallet cache file "{cache_file}": {exc}') from exc


def read_wallet_cache_entry(wallet_name: str, cache_file: Path) -> dict:
    with locked_cache_file(cache_file):
        cache = load_wallet_cache(cache_file)
        wallet_cache = cache.get("wallets", {}).get(wallet_name)
    if not isinstance(wallet_cache, dict):
        raise WalletError(
            f'wallet "{wallet_name}" has no synced cache; run syncwallet first'
        )
    return wallet_cache
