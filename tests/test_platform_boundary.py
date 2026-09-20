"""Architecture checks for the mini-wallet/bitcoin-tool boundary."""

import ast
from pathlib import Path


PROJECT_ROOT = Path(__file__).parents[1]


def test_production_adapter_imports_only_platform_service_modules() -> None:
    adapter_file = PROJECT_ROOT / "adapters" / "bitcoin_tool_wallet.py"
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


def test_product_code_has_no_platform_internal_imports() -> None:
    """Keep the frozen boundary enforceable as product modules are added."""

    forbidden_modules = {
        "wallet.wallet_cache",
        "wallet.wallet_sync",
        "wallet.wallet",
        "tx.workflow",
        "tx.coin_selection",
    }
    product_paths = [
        *PROJECT_ROOT.glob("*.py"),
        *PROJECT_ROOT.joinpath("adapters").glob("*.py"),
        *PROJECT_ROOT.joinpath("wallet_core").glob("*.py"),
    ]
    violations = []
    for path in product_paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in forbidden_modules:
                violations.append(f"{path.name}: {node.module}")
            elif isinstance(node, ast.Import):
                violations.extend(
                    f"{path.name}: {alias.name}"
                    for alias in node.names
                    if alias.name in forbidden_modules
                )

    assert violations == []


def test_obsolete_product_workarounds_and_preview_api_are_absent() -> None:
    adapter_source = (
        PROJECT_ROOT / "adapters" / "bitcoin_tool_wallet.py"
    ).read_text(encoding="utf-8")
    product_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for directory in (PROJECT_ROOT / "wallet_core",)
        for path in directory.glob("*.py")
    )

    assert "_platform_wallet_exists" not in adapter_source
    assert "preview_send" not in product_sources
    assert "SendPreview" not in product_sources


def test_import_worker_queue_never_enqueues_recovery_words() -> None:
    dialogs_file = PROJECT_ROOT / "wallet_dialogs.py"
    tree = ast.parse(dialogs_file.read_text(encoding="utf-8"))
    queued_values = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "put"
    ]

    assert queued_values
    for call in queued_values:
        payload = call.args[0]
        assert isinstance(payload, ast.Tuple)
        assert all(
            not isinstance(element, ast.Name)
            or element.id not in {"mnemonic", "exc"}
            for element in payload.elts
        )


def test_import_error_message_redacts_recovery_words() -> None:
    from wallet_dialogs import _import_error_message

    mnemonic = "abandon ability able about"
    message = _import_error_message(
        ValueError(f"backend accidentally echoed {mnemonic}"),
        mnemonic,
    )

    assert mnemonic not in message
    assert "[recovery words redacted]" in message
