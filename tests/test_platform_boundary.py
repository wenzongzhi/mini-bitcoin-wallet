"""Architecture checks for the mini-wallet/bitcoin-tool boundary."""

import ast
from pathlib import Path


def test_production_adapter_imports_only_platform_service_modules() -> None:
    adapter_file = Path(__file__).parents[1] / "adapters" / "bitcoin_tool_wallet.py"
    tree = ast.parse(adapter_file.read_text(encoding="utf-8"))
    bitcoin_tool_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module is not None
        and node.module.startswith(("wallet", "tx"))
    }

    assert "wallet.wallet_cache" not in bitcoin_tool_modules
    assert "tx.workflow" not in bitcoin_tool_modules
    assert bitcoin_tool_modules == {
        "wallet.service",
        "tx.service",
        "wallet_core.models",
        "wallet_core.ports",
    }
