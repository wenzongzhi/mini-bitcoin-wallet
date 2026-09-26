from wallet_settings_dialog import account_type_options


def test_mainnet_account_options_enable_only_supported_payment_types() -> None:
    options = account_type_options("mainnet")

    assert [(item.value, item.enabled) for item in options] == [
        ("p2wpkh", True),
        ("p2pkh", True),
        ("p2sh-p2wpkh", False),
        ("p2tr", False),
    ]
    assert options[0].detail == "BIP84 · bc1q"
    assert options[1].detail == "BIP44 · 1"
    assert options[2].detail == "Coming later"
    assert options[3].detail == "Coming later"


def test_testnet4_account_options_show_testnet_address_prefixes() -> None:
    options = account_type_options("testnet4")

    assert options[0].detail == "BIP84 · tb1q"
    assert options[1].detail == "BIP44 · m/n"
