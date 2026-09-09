from unittest.mock import patch

import pytest

from network.esplora_backend import EsploraBackend, EsploraError


class FakeResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, limit=-1):
        return self.payload if limit < 0 else self.payload[:limit]


@patch("network.esplora_backend.urlopen")
def test_transaction_status_uses_one_lightweight_esplora_request(urlopen) -> None:
    txid = "ab" * 32
    urlopen.return_value = FakeResponse(
        b'{"confirmed":true,"block_height":123,"block_hash":"'
        + b"cd" * 32
        + b'","block_time":1700000000}'
    )
    backend = EsploraBackend("https://example.invalid/api", network="testnet4")

    status = backend.get_transaction_status(txid.upper())

    assert status["confirmed"] is True
    assert status["block_height"] == 123
    request = urlopen.call_args.args[0]
    assert request.full_url == f"https://example.invalid/api/tx/{txid}/status"
    assert urlopen.call_count == 1


def test_transaction_status_rejects_an_invalid_txid_before_network_access() -> None:
    backend = EsploraBackend("https://example.invalid/api", network="testnet4")

    with pytest.raises(EsploraError, match="64 hexadecimal"):
        backend.get_transaction_status("123")

