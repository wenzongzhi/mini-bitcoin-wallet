"""
Copyright 2026 Wen Zhongzhi

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

from dataclasses import dataclass


NETWORK_MAINNET = "mainnet"
NETWORK_TESTNET4 = "testnet4"
SUPPORTED_NETWORKS = (NETWORK_MAINNET, NETWORK_TESTNET4)


@dataclass(frozen=True)
class ChainParams:
    name: str
    coin_type: int
    bip32_network: str
    p2pkh_version: bytes
    p2sh_version: bytes
    bech32_hrp: str
    default_esplora_url: str
    genesis_hash: str


CHAIN_PARAMS = {
    NETWORK_MAINNET: ChainParams(
        name=NETWORK_MAINNET,
        coin_type=0,
        bip32_network="main",
        p2pkh_version=b"\x00",
        p2sh_version=b"\x05",
        bech32_hrp="bc",
        default_esplora_url="https://blockstream.info/api",
        genesis_hash=(
            "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f"
        ),
    ),
    NETWORK_TESTNET4: ChainParams(
        name=NETWORK_TESTNET4,
        coin_type=1,
        bip32_network="test",
        p2pkh_version=b"\x6f",
        p2sh_version=b"\xc4",
        bech32_hrp="tb",
        default_esplora_url="https://mempool.space/testnet4/api",
        genesis_hash=(
            "00000000da84f2bafbbc53dee25a72ae507ff4914b867c565be350b0da8bf043"
        ),
    ),
}


def get_chain_params(network: str) -> ChainParams:
    try:
        return CHAIN_PARAMS[network]
    except (KeyError, TypeError) as exc:
        supported = ", ".join(SUPPORTED_NETWORKS)
        raise ValueError(f"unsupported network; choose one of: {supported}") from exc
