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
import re
import time
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from btc.chainparams import NETWORK_MAINNET, get_chain_params

DEFAULT_ESPLORA_URL = get_chain_params(NETWORK_MAINNET).default_esplora_url
MAX_RESPONSE_SIZE = 10 * 1024 * 1024
RETRYABLE_HTTP_STATUS_CODES = {429, 500, 502, 503, 504}


class EsploraError(Exception):
    pass


class EsploraBackend:
    def __init__(
        self,
        base_url: str | None = None,
        timeout: int = 20,
        network: str = NETWORK_MAINNET,
        retries: int = 2,
    ):
        try:
            self.params = get_chain_params(network)
        except ValueError as exc:
            raise EsploraError(str(exc)) from exc
        self.network = network
        base_url = base_url or self.params.default_esplora_url
        self.base_url = base_url.rstrip("/")
        if not self.base_url.startswith(("http://", "https://")):
            raise EsploraError("backend URL must start with http:// or https://")
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
            raise EsploraError("backend timeout must be positive")
        if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
            raise EsploraError("backend retries must be a non-negative integer")
        self.timeout = timeout
        self.retries = retries

    @staticmethod
    def _error_detail(exc: BaseException) -> str:
        if isinstance(exc, URLError):
            return str(exc.reason)
        return str(exc)

    def _request_text(
        self,
        path: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
        content_type: str | None = None,
    ) -> str:
        headers = {"User-Agent": "bitcoin-tool/transaction-client"}
        if content_type is not None:
            headers["Content-Type"] = content_type
        maximum_attempts = self.retries + 1 if method == "GET" else 1
        for attempt in range(1, maximum_attempts + 1):
            request = Request(
                f"{self.base_url}{path}",
                data=data,
                headers=headers,
                method=method,
            )
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    payload = response.read(MAX_RESPONSE_SIZE + 1)
                    if len(payload) > MAX_RESPONSE_SIZE:
                        raise EsploraError("Esplora response is too large")
                    return payload.decode("utf-8")
            except HTTPError as exc:
                should_retry = (
                    method == "GET"
                    and exc.code in RETRYABLE_HTTP_STATUS_CODES
                    and attempt < maximum_attempts
                )
                if not should_retry:
                    detail = exc.read(4096).decode("utf-8", errors="replace").strip()
                    suffix = f": {detail}" if detail else ""
                    raise EsploraError(
                        f"Esplora {method} {path} returned HTTP {exc.code}{suffix}"
                    ) from exc
            except UnicodeDecodeError as exc:
                raise EsploraError(
                    f"Esplora {method} {path} response is not valid UTF-8"
                ) from exc
            except (URLError, OSError) as exc:
                if attempt >= maximum_attempts:
                    attempts = f" after {attempt} attempts" if attempt > 1 else ""
                    raise EsploraError(
                        f"Esplora {method} {path} failed{attempts}: "
                        f"{self._error_detail(exc)}"
                    ) from exc
            time.sleep(0.5 * (2 ** (attempt - 1)))

        raise EsploraError(f"Esplora {method} {path} request failed")

    def _get_text(self, path: str) -> str:
        return self._request_text(path)

    def _get_json(self, path: str):
        text = self._get_text(path)
        try:
            return json.loads(text, parse_float=Decimal)
        except json.JSONDecodeError as exc:
            raise EsploraError(f"invalid JSON response for {path}") from exc

    def get_tip_height(self) -> int:
        try:
            return int(self._get_text("/blocks/tip/height"))
        except ValueError as exc:
            raise EsploraError("invalid tip height response") from exc

    def get_tip_hash(self) -> str:
        tip_hash = self._get_text("/blocks/tip/hash").strip()
        if not re.fullmatch(r"[0-9a-fA-F]{64}", tip_hash):
            raise EsploraError("invalid tip hash response")
        return tip_hash.lower()

    def get_block_hash(self, height: int) -> str:
        if isinstance(height, bool) or not isinstance(height, int) or height < 0:
            raise EsploraError("block height must be a non-negative integer")
        block_hash = self._get_text(f"/block-height/{height}").strip()
        if not re.fullmatch(r"[0-9a-fA-F]{64}", block_hash):
            raise EsploraError("invalid block hash response")
        return block_hash.lower()

    def verify_network(self) -> None:
        actual_genesis = self.get_block_hash(0)
        if actual_genesis != self.params.genesis_hash:
            raise EsploraError(
                f'Esplora backend is not connected to network "{self.network}"'
            )

    def get_address(self, address: str) -> dict:
        data = self._get_json(f"/address/{quote(address, safe='')}")
        if not isinstance(data, dict):
            raise EsploraError("invalid address response")
        return data

    def get_address_utxos(self, address: str) -> list[dict]:
        data = self._get_json(f"/address/{quote(address, safe='')}/utxo")
        if not isinstance(data, list):
            raise EsploraError("invalid address UTXO response")
        return data

    def get_address_transactions(self, address: str) -> list[dict]:
        data = self._get_json(f"/address/{quote(address, safe='')}/txs")
        if not isinstance(data, list):
            raise EsploraError("invalid address transaction response")
        return data

    def get_transaction_hex(self, txid: str) -> str:
        if not isinstance(txid, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", txid):
            raise EsploraError("transaction id must be 64 hexadecimal characters")
        raw_hex = self._get_text(f"/tx/{txid.lower()}/hex").strip()
        if not raw_hex or len(raw_hex) % 2 or not re.fullmatch(r"[0-9a-fA-F]+", raw_hex):
            raise EsploraError("invalid raw transaction response")
        return raw_hex.lower()

    def get_transaction_status(self, txid: str) -> dict:
        """Return the confirmation state for one transaction."""

        if not isinstance(txid, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", txid):
            raise EsploraError("transaction id must be 64 hexadecimal characters")
        status = self._get_json(f"/tx/{txid.lower()}/status")
        if not isinstance(status, dict) or not isinstance(status.get("confirmed"), bool):
            raise EsploraError("invalid transaction status response")
        for field in ("block_height", "block_time"):
            value = status.get(field)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise EsploraError("invalid transaction status response")
        block_hash = status.get("block_hash")
        if block_hash is not None and (
            not isinstance(block_hash, str)
            or not re.fullmatch(r"[0-9a-fA-F]{64}", block_hash)
        ):
            raise EsploraError("invalid transaction status response")
        return status

    def get_fee_estimates(self) -> dict[str, Decimal]:
        data = self._get_json("/fee-estimates")
        if not isinstance(data, dict):
            raise EsploraError("invalid fee estimates response")
        estimates = {}
        for target, rate in data.items():
            if not isinstance(target, str) or not isinstance(rate, (int, Decimal)):
                raise EsploraError("invalid fee estimates response")
            normalized_rate = Decimal(rate)
            if isinstance(rate, bool) or not normalized_rate.is_finite() or normalized_rate <= 0:
                raise EsploraError("invalid fee estimates response")
            estimates[target] = normalized_rate
        return estimates

    def broadcast_transaction(self, raw_tx_hex: str) -> str:
        if (
            not isinstance(raw_tx_hex, str)
            or not raw_tx_hex
            or len(raw_tx_hex) % 2
            or not re.fullmatch(r"[0-9a-fA-F]+", raw_tx_hex)
        ):
            raise EsploraError("raw transaction must be an even-length hexadecimal string")
        txid = self._request_text(
            "/tx",
            method="POST",
            data=raw_tx_hex.lower().encode("ascii"),
            content_type="text/plain",
        ).strip()
        if not re.fullmatch(r"[0-9a-fA-F]{64}", txid):
            raise EsploraError("invalid broadcast transaction id response")
        return txid.lower()
