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
import re
import secrets
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from bip32 import BIP32
from coincurve import PrivateKey
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from filelock import FileLock, Timeout
from mnemonic import Mnemonic
from platformdirs import user_data_path

from btc.chainparams import (
    NETWORK_MAINNET,
    NETWORK_TESTNET4,
    get_chain_params,
)
from btc.btc_address_gen import (
    p2pkh_script_pubkey,
    p2sh_p2wpkh_address,
    p2sh_p2wpkh_script_pubkey,
    p2tr_address,
    p2tr_script_pubkey,
    p2wpkh_bech32_address,
    p2wpkh_script_pubkey,
    privkey_to_pubkey,
    pubkey_to_p2pkh,
)


WALLET_FILENAMES = {
    NETWORK_MAINNET: "wallets.json",
    NETWORK_TESTNET4: "wallets_testnet4.json",
}
APP_NAME = "bitcoin-tool"
DATADIR_ENV = "BITCOIN_TOOL_DATADIR"
WALLET_VERSION = 3
BTC_RECEIVE_BRANCH = 0
BTC_CHANGE_BRANCH = 1
DEFAULT_ADDRESS_TYPE = "p2wpkh"
PBKDF2_ITERATIONS = 200_000
WALLET_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

ACCOUNT_DEFINITIONS = {
    "p2pkh": {
        "account_id": "bip44-account-0",
        "standard": "BIP44",
        "address_type": "P2PKH",
        "purpose": 44,
    },
    "p2sh-p2wpkh": {
        "account_id": "bip49-account-0",
        "standard": "BIP49",
        "address_type": "P2SH-P2WPKH",
        "purpose": 49,
    },
    "p2wpkh": {
        "account_id": "bip84-account-0",
        "standard": "BIP84",
        "address_type": "P2WPKH",
        "purpose": 84,
    },
    "p2tr": {
        "account_id": "bip86-account-0",
        "standard": "BIP86",
        "address_type": "P2TR",
        "purpose": 86,
    },
}
SUPPORTED_ADDRESS_TYPES = tuple(ACCOUNT_DEFINITIONS)


class WalletError(Exception):
    pass


def default_data_dir(data_dir: str | Path | None = None) -> Path:
    configured_dir = data_dir or os.environ.get(DATADIR_ENV)
    if configured_dir:
        return Path(configured_dir).expanduser().resolve()
    return user_data_path(APP_NAME, appauthor=False)


def default_wallet_file(
    data_dir: str | Path | None = None,
    network: str = NETWORK_MAINNET,
) -> Path:
    try:
        filename = WALLET_FILENAMES[network]
    except (KeyError, TypeError) as exc:
        raise WalletError(f"unsupported wallet network: {network}") from exc
    return default_data_dir(data_dir) / filename


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@contextmanager
def _locked_wallet_file(wallet_file: Path):
    try:
        wallet_file.parent.mkdir(parents=True, exist_ok=True)
        lock = FileLock(f"{wallet_file}.lock")
        with lock.acquire(timeout=10):
            yield
    except Timeout as exc:
        raise WalletError(f'wallet file is busy: "{wallet_file}"') from exc
    except OSError as exc:
        raise WalletError(f'cannot lock wallet file "{wallet_file}": {exc}') from exc


def _derive_key(password: str, salt: bytes, iterations: int) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=iterations,
    )
    return kdf.derive(password.encode("utf-8"))


def _load_wallets(wallet_file: Path) -> dict:
    try:
        with wallet_file.open("r", encoding="utf-8") as file:
            wallets = json.load(file)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise WalletError(f'cannot read wallet file "{wallet_file}": {exc}') from exc

    if not isinstance(wallets, dict):
        raise WalletError(f'invalid wallet file "{wallet_file}"')
    return wallets


def _save_wallets(wallets: dict, wallet_file: Path) -> None:
    wallet_file.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=wallet_file.parent,
            prefix=f".{wallet_file.name}.",
            suffix=".tmp",
            delete=False,
        ) as file:
            temporary_path = Path(file.name)
            json.dump(wallets, file, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, wallet_file)
    except OSError as exc:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise WalletError(f'cannot write wallet file "{wallet_file}": {exc}') from exc


def _validate_wallet_name(wallet_name: str) -> None:
    if not WALLET_NAME_PATTERN.fullmatch(wallet_name):
        raise WalletError(
            "wallet name may contain only letters, numbers, underscores, and hyphens"
        )


def _normalize_address_type(address_type: str) -> str:
    normalized = address_type.strip().lower()
    if normalized not in ACCOUNT_DEFINITIONS:
        supported = ", ".join(SUPPORTED_ADDRESS_TYPES)
        raise WalletError(f"unsupported address type; choose one of: {supported}")
    return normalized


def _account_definitions(network: str) -> dict[str, dict]:
    try:
        coin_type = get_chain_params(network).coin_type
    except ValueError as exc:
        raise WalletError(str(exc)) from exc
    return {
        address_type: {
            **definition,
            "account_derivation_path": (
                f"m/{definition['purpose']}'/{coin_type}'/0'"
            ),
        }
        for address_type, definition in ACCOUNT_DEFINITIONS.items()
    }


def _require_current_wallet(wallet: dict, network: str = NETWORK_MAINNET) -> dict:
    if wallet.get("version") != WALLET_VERSION:
        raise WalletError(f"unsupported wallet format; version {WALLET_VERSION} is required")
    if wallet.get("network") != network:
        raise WalletError(
            f'wallet network is "{wallet.get("network")}", expected "{network}"'
        )
    accounts = wallet.get("accounts")
    if not isinstance(accounts, dict):
        raise WalletError("wallet accounts are invalid")
    for definition in _account_definitions(network).values():
        if not isinstance(accounts.get(definition["account_id"]), dict):
            raise WalletError("wallet accounts are invalid")
    return accounts


def _get_account(
    wallet: dict,
    address_type: str,
    network: str = NETWORK_MAINNET,
) -> tuple[str, dict, dict]:
    normalized_type = _normalize_address_type(address_type)
    definition = _account_definitions(network)[normalized_type]
    accounts = _require_current_wallet(wallet, network)
    account = accounts.get(definition["account_id"])
    if not isinstance(account, dict):
        raise WalletError("wallet account is invalid")
    return normalized_type, definition, account


def mnemonic_from_entropy_hex(entropy_hex: str) -> str:
    try:
        entropy = bytes.fromhex(entropy_hex)
    except ValueError as exc:
        raise WalletError("entropy contains invalid hexadecimal characters") from exc
    try:
        return Mnemonic("english").to_mnemonic(entropy)
    except ValueError as exc:
        raise WalletError("entropy must be 128, 160, 192, 224, or 256 bits") from exc


def entropy_hex_from_mnemonic(mnemonic: str) -> str:
    normalized_mnemonic = normalize_mnemonic(mnemonic)
    try:
        return Mnemonic("english").to_entropy(normalized_mnemonic).hex()
    except ValueError as exc:
        raise WalletError("mnemonic is invalid") from exc


def normalize_mnemonic(mnemonic: str) -> str:
    normalized_mnemonic = " ".join(mnemonic.strip().split())
    if not Mnemonic("english").check(normalized_mnemonic):
        raise WalletError("mnemonic is invalid")
    return normalized_mnemonic


def _mnemonic_from_entropy(entropy_hex: str | None) -> str:
    if entropy_hex is None:
        entropy = secrets.token_bytes(32)
        return Mnemonic("english").to_mnemonic(entropy)
    if len(entropy_hex) != 64:
        raise WalletError("entropy must be exactly 256 bits (64 hex characters)")
    return mnemonic_from_entropy_hex(entropy_hex)


def _new_account(bip32: BIP32, definition: dict, coin_type: int) -> dict:
    account_path = definition["account_derivation_path"]
    return {
        "standard": definition["standard"],
        "address_type": definition["address_type"],
        "purpose": definition["purpose"],
        "coin_type": coin_type,
        "account_index": 0,
        "account_derivation_path": account_path,
        "account_xpub": bip32.get_xpub_from_path(account_path),
        "receive_branch": BTC_RECEIVE_BRANCH,
        "change_branch": BTC_CHANGE_BRANCH,
        "next_receive_index": 0,
        "next_change_index": 0,
        "issued_addresses": [],
    }


def _derive_wallet_metadata(mnemonic: str, network: str) -> dict:
    try:
        params = get_chain_params(network)
    except ValueError as exc:
        raise WalletError(str(exc)) from exc
    bip32 = BIP32.from_seed(
        Mnemonic.to_seed(mnemonic, passphrase=""),
        network=params.bip32_network,
    )
    return {
        "master_fingerprint": bip32.get_fingerprint().hex(),
        "accounts": {
            definition["account_id"]: _new_account(
                bip32,
                definition,
                params.coin_type,
            )
            for definition in _account_definitions(network).values()
        },
    }


def create_wallet(
    wallet_name: str,
    password: str | None = None,
    entropy_hex: str | None = None,
    wallet_file: Path | None = None,
    *,
    mnemonic: str | None = None,
    network: str = NETWORK_MAINNET,
) -> dict:
    _validate_wallet_name(wallet_name)
    if password == "":
        raise WalletError("password must not be empty; omit it to create a plaintext wallet")
    if entropy_hex is not None and mnemonic is not None:
        raise WalletError("entropy and mnemonic are mutually exclusive")

    try:
        get_chain_params(network)
    except ValueError as exc:
        raise WalletError(str(exc)) from exc
    path = wallet_file or default_wallet_file(network=network)
    mnemonic = normalize_mnemonic(mnemonic) if mnemonic is not None else _mnemonic_from_entropy(entropy_hex)
    metadata = _derive_wallet_metadata(mnemonic, network)
    wallet = {
        "version": WALLET_VERSION,
        "network": network,
        "encrypted": password is not None,
        "master_fingerprint": metadata["master_fingerprint"],
    }

    if password is None:
        wallet["mnemonic"] = mnemonic
    else:
        salt = secrets.token_bytes(16)
        nonce = secrets.token_bytes(12)
        key = _derive_key(password, salt, PBKDF2_ITERATIONS)
        ciphertext = AESGCM(key).encrypt(
            nonce,
            mnemonic.encode("utf-8"),
            wallet_name.encode("utf-8"),
        )
        wallet["encryption"] = {
            "cipher": "AES-256-GCM",
            "kdf": "PBKDF2-HMAC-SHA256",
            "iterations": PBKDF2_ITERATIONS,
            "salt": salt.hex(),
            "nonce": nonce.hex(),
            "ciphertext": ciphertext.hex(),
        }
    wallet["accounts"] = metadata["accounts"]

    with _locked_wallet_file(path):
        wallets = _load_wallets(path)
        if wallet_name in wallets:
            raise WalletError(f'wallet "{wallet_name}" already exists')
        wallets[wallet_name] = wallet
        _save_wallets(wallets, path)

    return {
        "wallet_name": wallet_name,
        "network": network,
        "mnemonic": mnemonic if password is None else None,
        "encrypted": wallet["encrypted"],
        "wallet_file": str(path),
        "account_count": len(metadata["accounts"]),
    }


def _read_mnemonic(
    wallet_name: str,
    wallet: dict,
    password: str | None,
    network: str = NETWORK_MAINNET,
) -> str:
    _require_current_wallet(wallet, network)
    if not wallet.get("encrypted"):
        mnemonic = wallet.get("mnemonic")
        if not isinstance(mnemonic, str):
            raise WalletError("wallet does not contain a valid mnemonic")
        return mnemonic
    if password is None:
        raise WalletError("password is required for this encrypted wallet")

    encryption = wallet.get("encryption")
    if not isinstance(encryption, dict):
        raise WalletError("wallet encryption metadata is missing")
    try:
        if encryption.get("cipher") != "AES-256-GCM":
            raise WalletError("unsupported wallet cipher")
        if encryption.get("kdf") != "PBKDF2-HMAC-SHA256":
            raise WalletError("unsupported wallet key derivation function")
        iterations = int(encryption["iterations"])
        if not 100_000 <= iterations <= 10_000_000:
            raise WalletError("wallet PBKDF2 iteration count is invalid")
        salt = bytes.fromhex(encryption["salt"])
        nonce = bytes.fromhex(encryption["nonce"])
        ciphertext = bytes.fromhex(encryption["ciphertext"])
        key = _derive_key(password, salt, iterations)
        plaintext = AESGCM(key).decrypt(
            nonce,
            ciphertext,
            wallet_name.encode("utf-8"),
        )
        return plaintext.decode("utf-8")
    except InvalidTag as exc:
        raise WalletError("incorrect password or corrupted wallet data") from exc
    except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
        raise WalletError("invalid wallet encryption metadata") from exc


def _validate_mnemonic(mnemonic: str) -> None:
    if not Mnemonic("english").check(mnemonic):
        raise WalletError("wallet contains an invalid mnemonic")


def get_mnemonic(
    wallet_name: str,
    password: str | None = None,
    wallet_file: Path | None = None,
    network: str = NETWORK_MAINNET,
) -> dict:
    _validate_wallet_name(wallet_name)
    path = wallet_file or default_wallet_file(network=network)
    with _locked_wallet_file(path):
        wallets = _load_wallets(path)
        wallet = wallets.get(wallet_name)
        if not isinstance(wallet, dict):
            raise WalletError(f'wallet "{wallet_name}" does not exist in "{path}"')
        mnemonic = _read_mnemonic(wallet_name, wallet, password, network)
        _validate_mnemonic(mnemonic)
    return {"wallet_name": wallet_name, "mnemonic": mnemonic}


def _branch_purpose(branch: int) -> str:
    if branch == BTC_RECEIVE_BRANCH:
        return "receive"
    if branch == BTC_CHANGE_BRANCH:
        return "change"
    raise WalletError("branch must be 0 receiving or 1 change")


def _next_index_key(branch: int) -> str:
    _branch_purpose(branch)
    return "next_receive_index" if branch == BTC_RECEIVE_BRANCH else "next_change_index"


def _entry_branch(entry: dict) -> int:
    branch = entry.get("branch")
    if isinstance(branch, int):
        _branch_purpose(branch)
        return branch
    raise WalletError("wallet address book is invalid")


def _read_account_state(
    account: dict,
    definition: dict,
    branch: int,
    network: str = NETWORK_MAINNET,
) -> tuple[str, str, int]:
    index_key = _next_index_key(branch)
    try:
        account_xpub = account["account_xpub"]
        account_path = account["account_derivation_path"]
        index = account[index_key]
        receive_branch = account["receive_branch"]
        change_branch = account["change_branch"]
    except KeyError as exc:
        raise WalletError("wallet account metadata is invalid") from exc

    try:
        coin_type = get_chain_params(network).coin_type
    except ValueError as exc:
        raise WalletError(str(exc)) from exc

    if (
        account.get("standard") != definition["standard"]
        or account.get("address_type") != definition["address_type"]
        or account.get("purpose") != definition["purpose"]
        or account.get("coin_type") != coin_type
        or account.get("account_index") != 0
        or account_path != definition["account_derivation_path"]
        or not isinstance(account_xpub, str)
        or not account_xpub
        or isinstance(index, bool)
        or not isinstance(index, int)
        or isinstance(receive_branch, bool)
        or not isinstance(receive_branch, int)
        or isinstance(change_branch, bool)
        or not isinstance(change_branch, int)
        or receive_branch != BTC_RECEIVE_BRANCH
        or change_branch != BTC_CHANGE_BRANCH
        or not 0 <= index < 2**31
    ):
        raise WalletError("wallet account metadata is invalid")
    return account_xpub, account_path, index


def _derive_public_key(
    account_xpub: str,
    branch: int,
    index: int,
    network: str = NETWORK_MAINNET,
) -> bytes:
    _branch_purpose(branch)
    if not 0 <= index < 2**31:
        raise WalletError("index must be non-hardened and in range 0..2147483647")
    if not isinstance(account_xpub, str) or not account_xpub:
        raise WalletError("account xpub is required")
    try:
        bip32 = BIP32.from_xpub(account_xpub)
        if bip32.network != get_chain_params(network).bip32_network:
            raise WalletError(f'account xpub does not belong to network "{network}"')
        return bip32.get_pubkey_from_path(f"m/{branch}/{index}")
    except WalletError:
        raise
    except Exception as exc:
        raise WalletError("cannot derive public key from account xpub") from exc


def _address_and_script(
    public_key: bytes,
    address_type: str,
    network: str = NETWORK_MAINNET,
) -> tuple[str, str]:
    normalized_type = _normalize_address_type(address_type)
    if normalized_type == "p2pkh":
        return pubkey_to_p2pkh(public_key, network), p2pkh_script_pubkey(public_key)
    if normalized_type == "p2sh-p2wpkh":
        return (
            p2sh_p2wpkh_address(public_key, network),
            p2sh_p2wpkh_script_pubkey(public_key),
        )
    if normalized_type == "p2wpkh":
        return (
            p2wpkh_bech32_address(public_key, network),
            p2wpkh_script_pubkey(public_key),
        )
    if normalized_type == "p2tr":
        return p2tr_address(public_key, network), p2tr_script_pubkey(public_key)
    raise WalletError("unsupported address type")


def _derive_address_entry(
    account_xpub: str,
    account_path: str,
    address_type: str,
    branch: int,
    index: int,
    created_at: str | None,
    label: str = "",
    network: str = NETWORK_MAINNET,
) -> dict:
    relative_path = f"m/{branch}/{index}"
    public_key = _derive_public_key(account_xpub, branch, index, network)
    address, script_pubkey = _address_and_script(public_key, address_type, network)
    return {
        "index": index,
        "branch": branch,
        "relative_path": relative_path,
        "path": f"{account_path}/{branch}/{index}",
        "address": address,
        "script_pubkey": script_pubkey,
        "purpose": _branch_purpose(branch),
        "used": False,
        "label": label,
        "created_at": created_at,
    }


def get_new_address(
    wallet_name: str,
    wallet_file: Path | None = None,
    change: bool = False,
    address_type: str = DEFAULT_ADDRESS_TYPE,
    network: str = NETWORK_MAINNET,
) -> dict:
    _validate_wallet_name(wallet_name)
    branch = BTC_CHANGE_BRANCH if change else BTC_RECEIVE_BRANCH
    path = wallet_file or default_wallet_file(network=network)
    with _locked_wallet_file(path):
        wallets = _load_wallets(path)
        wallet = wallets.get(wallet_name)
        if not isinstance(wallet, dict):
            raise WalletError(f'wallet "{wallet_name}" does not exist in "{path}"')

        normalized_type, definition, account = _get_account(
            wallet,
            address_type,
            network,
        )
        account_xpub, account_path, index = _read_account_state(
            account,
            definition,
            branch,
            network,
        )
        issued_addresses = account.get("issued_addresses")
        if not isinstance(issued_addresses, list):
            raise WalletError("wallet address book is invalid")
        if any(
            isinstance(entry, dict)
            and _entry_branch(entry) == branch
            and entry.get("index") == index
            for entry in issued_addresses
        ):
            raise WalletError("wallet address index is already present in the address book")

        entry = _derive_address_entry(
            account_xpub,
            account_path,
            normalized_type,
            branch,
            index,
            created_at=_utc_now(),
            network=network,
        )
        issued_addresses.append(entry)
        account[_next_index_key(branch)] = index + 1
        _save_wallets(wallets, path)

    return {
        "wallet_name": wallet_name,
        "network": network,
        "account_id": definition["account_id"],
        "address": entry["address"],
        "address_type": definition["address_type"],
        "purpose": entry["purpose"],
        "branch": branch,
        "index": entry["index"],
        "relative_derivation_path": entry["relative_path"],
        "derivation_path": entry["path"],
    }


def derive_p2wpkh_public_key_from_account_xpub(
    account_xpub: str,
    branch: int,
    index: int,
    network: str = NETWORK_MAINNET,
) -> bytes:
    return _derive_public_key(account_xpub, branch, index, network)


def derive_p2wpkh_from_account_xpub(
    account_xpub: str,
    branch: int,
    index: int,
    network: str = NETWORK_MAINNET,
) -> str:
    return p2wpkh_bech32_address(
        derive_p2wpkh_public_key_from_account_xpub(
            account_xpub,
            branch,
            index,
            network,
        ),
        network,
    )


def _normalize_address_entry(
    account_id: str,
    definition: dict,
    account_path: str,
    entry: dict,
) -> dict:
    if not isinstance(entry, dict):
        raise WalletError("wallet address book is invalid")
    try:
        branch = _entry_branch(entry)
        index = int(entry["index"])
        address = entry["address"]
        relative_path = entry["relative_path"]
        path_value = entry["path"]
        script_pubkey = entry["script_pubkey"]
        purpose = entry["purpose"]
        used = entry["used"]
        label = entry["label"]
        created_at = entry["created_at"]
    except (KeyError, TypeError, ValueError) as exc:
        raise WalletError("wallet address book is invalid") from exc

    if (
        not 0 <= index < 2**31
        or not isinstance(address, str)
        or not address
        or relative_path != f"m/{branch}/{index}"
        or path_value != f"{account_path}/{branch}/{index}"
        or not isinstance(script_pubkey, str)
        or not re.fullmatch(r"[0-9a-f]+", script_pubkey)
        or purpose != _branch_purpose(branch)
        or not isinstance(used, bool)
        or not isinstance(label, str)
        or (created_at is not None and not isinstance(created_at, str))
    ):
        raise WalletError("wallet address book is invalid")

    return {
        **entry,
        "account_id": account_id,
        "address_type": definition["address_type"],
        "account_derivation_path": account_path,
    }


def get_wallet_address_book(
    wallet_name: str,
    wallet_file: Path | None = None,
    address_type: str | None = None,
    network: str = NETWORK_MAINNET,
) -> dict:
    _validate_wallet_name(wallet_name)
    path = wallet_file or default_wallet_file(network=network)
    selected_types = (
        (_normalize_address_type(address_type),)
        if address_type is not None
        else SUPPORTED_ADDRESS_TYPES
    )
    normalized_entries = []

    with _locked_wallet_file(path):
        wallets = _load_wallets(path)
        wallet = wallets.get(wallet_name)
        if not isinstance(wallet, dict):
            raise WalletError(f'wallet "{wallet_name}" does not exist in "{path}"')
        _require_current_wallet(wallet, network)

        for selected_type in selected_types:
            _, definition, account = _get_account(wallet, selected_type, network)
            _, account_path, _ = _read_account_state(
                account,
                definition,
                BTC_RECEIVE_BRANCH,
                network,
            )
            issued_addresses = account.get("issued_addresses")
            if not isinstance(issued_addresses, list):
                raise WalletError("wallet address book is invalid")
            normalized_entries.extend(
                _normalize_address_entry(
                    definition["account_id"],
                    definition,
                    account_path,
                    entry,
                )
                for entry in issued_addresses
            )

    return {
        "wallet_name": wallet_name,
        "network": network,
        "wallet_file": str(path),
        "account_count": len(selected_types),
        "address_count": len(normalized_entries),
        "addresses": normalized_entries,
    }


def mark_wallet_addresses_used(
    wallet_name: str,
    used_addresses: set[str],
    wallet_file: Path | None = None,
    network: str = NETWORK_MAINNET,
) -> int:
    _validate_wallet_name(wallet_name)
    path = wallet_file or default_wallet_file(network=network)
    changed_count = 0
    with _locked_wallet_file(path):
        wallets = _load_wallets(path)
        wallet = wallets.get(wallet_name)
        if not isinstance(wallet, dict):
            raise WalletError(f'wallet "{wallet_name}" does not exist in "{path}"')
        accounts = _require_current_wallet(wallet, network)
        for definition in _account_definitions(network).values():
            account = accounts[definition["account_id"]]
            issued_addresses = account.get("issued_addresses")
            if not isinstance(issued_addresses, list):
                raise WalletError("wallet address book is invalid")
            for entry in issued_addresses:
                if not isinstance(entry, dict):
                    raise WalletError("wallet address book is invalid")
                address = entry.get("address")
                if (
                    isinstance(address, str)
                    and address in used_addresses
                    and not entry.get("used")
                ):
                    entry["used"] = True
                    changed_count += 1
        if changed_count:
            _save_wallets(wallets, path)
    return changed_count


def _descriptor_like(
    account_xpub: str,
    master_fingerprint: str,
    definition: dict,
) -> str:
    account_suffix = definition["account_derivation_path"].removeprefix("m/")
    key = f"[{master_fingerprint}/{account_suffix}]{account_xpub}/0/*"
    address_type = definition["address_type"]
    if address_type == "P2PKH":
        return f"pkh({key})"
    if address_type == "P2SH-P2WPKH":
        return f"sh(wpkh({key}))"
    if address_type == "P2WPKH":
        return f"wpkh({key})"
    if address_type == "P2TR":
        return f"tr({key})"
    raise WalletError("unsupported wallet address type")


def export_account_xpub(
    wallet_name: str,
    password: str | None = None,
    wallet_file: Path | None = None,
    address_type: str = DEFAULT_ADDRESS_TYPE,
    network: str = NETWORK_MAINNET,
) -> dict:
    _validate_wallet_name(wallet_name)
    path = wallet_file or default_wallet_file(network=network)
    with _locked_wallet_file(path):
        wallets = _load_wallets(path)
        wallet = wallets.get(wallet_name)
        if not isinstance(wallet, dict):
            raise WalletError(f'wallet "{wallet_name}" does not exist in "{path}"')
        _require_current_wallet(wallet, network)

        if wallet.get("encrypted"):
            if not password:
                raise WalletError("password is required to export account xpub")
            mnemonic = _read_mnemonic(wallet_name, wallet, password, network)
            _validate_mnemonic(mnemonic)

        _, definition, account = _get_account(wallet, address_type, network)
        account_xpub, account_path, _ = _read_account_state(
            account,
            definition,
            BTC_RECEIVE_BRANCH,
            network,
        )
        master_fingerprint = wallet.get("master_fingerprint")
        if not isinstance(master_fingerprint, str) or not re.fullmatch(
            r"[0-9a-fA-F]{8}",
            master_fingerprint,
        ):
            raise WalletError("wallet master fingerprint metadata is invalid")

    return {
        "wallet_name": wallet_name,
        "network": network,
        "account_id": definition["account_id"],
        "standard": definition["standard"],
        "address_type": definition["address_type"],
        "account_derivation_path": account_path,
        "account_xpub": account_xpub,
        "descriptor_like": _descriptor_like(
            account_xpub,
            master_fingerprint.lower(),
            definition,
        ),
    }


def get_wallet_signing_key(
    wallet_name: str,
    derivation_path: str,
    password: str | None = None,
    wallet_file: Path | None = None,
    network: str = NETWORK_MAINNET,
) -> dict:
    _validate_wallet_name(wallet_name)
    path = wallet_file or default_wallet_file(network=network)
    with _locked_wallet_file(path):
        wallets = _load_wallets(path)
        wallet = wallets.get(wallet_name)
        if not isinstance(wallet, dict):
            raise WalletError(f'wallet "{wallet_name}" does not exist in "{path}"')
        accounts = _require_current_wallet(wallet, network)

        matches = []
        for normalized_type, definition in _account_definitions(network).items():
            account = accounts[definition["account_id"]]
            account_xpub, account_path, _ = _read_account_state(
                account,
                definition,
                BTC_RECEIVE_BRANCH,
                network,
            )
            issued_addresses = account.get("issued_addresses")
            if not isinstance(issued_addresses, list):
                raise WalletError("wallet address book is invalid")
            for entry in issued_addresses:
                if isinstance(entry, dict) and entry.get("path") == derivation_path:
                    matches.append(
                        (normalized_type, definition, account_xpub, account_path, entry)
                    )
        if len(matches) != 1:
            raise WalletError("path is not a unique issued wallet address")

        normalized_type, definition, account_xpub, account_path, entry = matches[0]
        normalized_entry = _normalize_address_entry(
            definition["account_id"],
            definition,
            account_path,
            entry,
        )
        branch = normalized_entry["branch"]
        index = normalized_entry["index"]
        mnemonic = _read_mnemonic(wallet_name, wallet, password, network)
        _validate_mnemonic(mnemonic)
        try:
            private_key = BIP32.from_seed(
                Mnemonic.to_seed(mnemonic, passphrase="")
            ).get_privkey_from_path(derivation_path)
        except Exception as exc:
            raise WalletError("cannot derive private key for wallet path") from exc

        public_key = privkey_to_pubkey(private_key, compressed=True)
        xpub_public_key = _derive_public_key(account_xpub, branch, index, network)
        address, script_pubkey = _address_and_script(
            public_key,
            normalized_type,
            network,
        )
        if (
            public_key != xpub_public_key
            or address != normalized_entry["address"]
            or script_pubkey != normalized_entry["script_pubkey"]
        ):
            raise WalletError("wallet private and public derivation data do not match")

    return {
        "wallet_name": wallet_name,
        "network": network,
        "account_id": definition["account_id"],
        "address_type": definition["address_type"],
        "derivation_path": derivation_path,
        "address": address,
        "private_key_hex": private_key.hex(),
        "public_key_hex": public_key.hex(),
    }


def wallet_requires_password(
    wallet_name: str,
    wallet_file: Path | None = None,
    network: str = NETWORK_MAINNET,
) -> bool:
    _validate_wallet_name(wallet_name)
    path = wallet_file or default_wallet_file(network=network)
    with _locked_wallet_file(path):
        wallet = _load_wallets(path).get(wallet_name)
        if not isinstance(wallet, dict):
            raise WalletError(f'wallet "{wallet_name}" does not exist in "{path}"')
        _require_current_wallet(wallet, network)
        encrypted = wallet.get("encrypted")
        if not isinstance(encrypted, bool):
            raise WalletError("wallet encryption state is invalid")
        return encrypted


class WalletSigningSession:
    """Short-lived transaction signer that decrypts a wallet only once."""

    def __init__(
        self,
        wallet_name: str,
        password: str | None = None,
        wallet_file: Path | None = None,
        network: str = NETWORK_MAINNET,
    ):
        self.wallet_name = wallet_name
        self.password = password
        self.wallet_file = wallet_file or default_wallet_file(network=network)
        self.network = network
        self._root = None
        self._entries = None

    def __enter__(self):
        _validate_wallet_name(self.wallet_name)
        with _locked_wallet_file(self.wallet_file):
            wallet = _load_wallets(self.wallet_file).get(self.wallet_name)
            if not isinstance(wallet, dict):
                raise WalletError(
                    f'wallet "{self.wallet_name}" does not exist in "{self.wallet_file}"'
                )
            accounts = _require_current_wallet(wallet, self.network)
            mnemonic = _read_mnemonic(
                self.wallet_name,
                wallet,
                self.password,
                self.network,
            )
            _validate_mnemonic(mnemonic)
            entries = {}
            for normalized_type, definition in _account_definitions(self.network).items():
                account = accounts[definition["account_id"]]
                _, account_path, _ = _read_account_state(
                    account,
                    definition,
                    BTC_RECEIVE_BRANCH,
                    self.network,
                )
                issued_addresses = account.get("issued_addresses")
                if not isinstance(issued_addresses, list):
                    raise WalletError("wallet address book is invalid")
                for entry in issued_addresses:
                    normalized = _normalize_address_entry(
                        definition["account_id"],
                        definition,
                        account_path,
                        entry,
                    )
                    path = normalized["path"]
                    if path in entries:
                        raise WalletError("wallet contains duplicate issued paths")
                    entries[path] = (normalized_type, normalized)

        seed = Mnemonic.to_seed(mnemonic, passphrase="")
        self._root = BIP32.from_seed(
            seed,
            network=get_chain_params(self.network).bip32_network,
        )
        self._entries = entries
        mnemonic = None
        seed = None
        return self

    def sign_digest(self, metadata: dict, digest: bytes) -> tuple[bytes, bytes]:
        if self._root is None or self._entries is None:
            raise WalletError("wallet signing session is not active")
        if not isinstance(metadata, dict) or not isinstance(digest, bytes) or len(digest) != 32:
            raise WalletError("invalid transaction signing request")
        path = metadata.get("derivation_path") or metadata.get("path")
        match = self._entries.get(path)
        if match is None:
            raise WalletError("transaction input path is not an issued wallet address")
        normalized_type, entry = match
        expected_type = normalized_type
        provided_type = str(metadata.get("address_type", "")).lower()
        if provided_type != expected_type:
            raise WalletError("transaction input address type does not match wallet")
        try:
            script_pubkey = bytes.fromhex(metadata["script_pubkey"])
        except (KeyError, TypeError, ValueError) as exc:
            raise WalletError("transaction input scriptPubKey is invalid") from exc
        if (
            metadata.get("address") != entry["address"]
            or metadata.get("account_id") != entry["account_id"]
            or script_pubkey.hex() != entry["script_pubkey"]
        ):
            raise WalletError("transaction input metadata does not match wallet")
        try:
            private_key = self._root.get_privkey_from_path(path)
        except Exception as exc:
            raise WalletError("cannot derive transaction signing key") from exc
        public_key = privkey_to_pubkey(private_key, compressed=True)
        address, expected_script = _address_and_script(
            public_key,
            normalized_type,
            self.network,
        )
        if address != entry["address"] or expected_script != entry["script_pubkey"]:
            raise WalletError("derived transaction signing key does not match wallet")
        signature_der = PrivateKey(private_key).sign(digest, hasher=None)
        private_key = None
        return signature_der, public_key

    def __exit__(self, exc_type, exc_value, traceback):
        self._root = None
        self._entries = None
        self.password = None
        return False


def rebuild_address_book(
    wallet_name: str,
    wallet_file: Path | None = None,
    address_type: str | None = None,
    network: str = NETWORK_MAINNET,
) -> dict:
    _validate_wallet_name(wallet_name)
    path = wallet_file or default_wallet_file(network=network)
    selected_types = (
        (_normalize_address_type(address_type),)
        if address_type is not None
        else SUPPORTED_ADDRESS_TYPES
    )
    rebuilt_count = 0
    recovered_count = 0
    recovered_at = _utc_now()

    with _locked_wallet_file(path):
        wallets = _load_wallets(path)
        wallet = wallets.get(wallet_name)
        if not isinstance(wallet, dict):
            raise WalletError(f'wallet "{wallet_name}" does not exist')
        _require_current_wallet(wallet, network)

        for selected_type in selected_types:
            _, definition, account = _get_account(wallet, selected_type, network)
            account_xpub, account_path, next_receive_index = _read_account_state(
                account,
                definition,
                BTC_RECEIVE_BRANCH,
                network,
            )
            _, _, next_change_index = _read_account_state(
                account,
                definition,
                BTC_CHANGE_BRANCH,
                network,
            )
            existing_entries = account.get("issued_addresses")
            if not isinstance(existing_entries, list):
                raise WalletError("wallet address book is invalid")
            existing_by_index = {
                (_entry_branch(entry), entry["index"]): entry
                for entry in existing_entries
                if isinstance(entry, dict) and isinstance(entry.get("index"), int)
            }

            rebuilt_entries = []
            for branch, next_index in (
                (BTC_RECEIVE_BRANCH, next_receive_index),
                (BTC_CHANGE_BRANCH, next_change_index),
            ):
                for index in range(next_index):
                    existing = existing_by_index.get((branch, index), {})
                    created_at = existing.get("created_at")
                    label = existing.get("label", "")
                    used = existing.get("used", False)
                    if not isinstance(created_at, str):
                        created_at = None
                    if not isinstance(label, str):
                        label = ""
                    if not isinstance(used, bool):
                        used = False
                    entry = _derive_address_entry(
                        account_xpub,
                        account_path,
                        selected_type,
                        branch,
                        index,
                        created_at,
                        label,
                        network,
                    )
                    entry["used"] = used
                    if created_at is None:
                        entry["recovered_at"] = recovered_at
                        recovered_count += 1
                    rebuilt_entries.append(entry)
            account["issued_addresses"] = rebuilt_entries
            rebuilt_count += len(rebuilt_entries)

        _save_wallets(wallets, path)

    return {
        "wallet_name": wallet_name,
        "network": network,
        "account_count": len(selected_types),
        "address_count": rebuilt_count,
        "recovered_count": recovered_count,
        "wallet_file": str(path),
    }
