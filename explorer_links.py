"""Product-specific block explorer links kept outside Platform DTOs."""


def transaction_explorer_url(network: str, txid: str) -> str:
    prefix = (
        "https://mempool.space/testnet4/tx"
        if network == "testnet4"
        else "https://mempool.space/tx"
    )
    return f"{prefix}/{txid}"
