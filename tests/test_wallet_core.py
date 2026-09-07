from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from adapters import BitcoinToolWalletService, DemoWalletService
from btc.chainparams import NETWORK_TESTNET4
from wallet_core import BitcoinAmount, DisplayUnit, WalletApplication
from wallet import get_wallet_address_book


class FakeEsploraBackend:
    """Deterministic backend for address-discovery integration tests."""

    def __init__(self, network="mainnet", funded_ordinals=None):
        self.network = network
        self.base_url = "https://example.invalid/api"
        self.funded_ordinals = funded_ordinals or {}
        self._address_ordinals = {}

    def verify_network(self):
        return None

    def get_tip_height(self):
        return 100

    def get_tip_hash(self):
        return "00" * 32

    def _ordinal(self, address):
        return self._address_ordinals.setdefault(address, len(self._address_ordinals))

    def get_address(self, address):
        amount = self.funded_ordinals.get(self._ordinal(address), 0)
        count = 1 if amount else 0
        return {
            "chain_stats": {
                "tx_count": count,
                "funded_txo_count": count,
                "funded_txo_sum": amount,
                "spent_txo_count": 0,
                "spent_txo_sum": 0,
            },
            "mempool_stats": {},
        }

    def get_address_utxos(self, address):
        ordinal = self._ordinal(address)
        amount = self.funded_ordinals.get(ordinal, 0)
        if not amount:
            return []
        return [
            {
                "txid": f"{ordinal + 1:064x}",
                "vout": 0,
                "value": amount,
                "status": {"confirmed": True, "block_height": 90},
            }
        ]

    def get_address_transactions(self, address):
        ordinal = self._ordinal(address)
        amount = self.funded_ordinals.get(ordinal, 0)
        if not amount:
            return []
        return [
            {
                "txid": f"{ordinal + 1:064x}",
                "vin": [],
                "vout": [{"scriptpubkey_address": address, "value": amount}],
                "fee": 100,
                "status": {
                    "confirmed": True,
                    "block_height": 90,
                    "block_time": 1_700_000_000 + ordinal,
                },
            }
        ]


class BitcoinAmountTests(TestCase):
    def test_parses_btc_without_floating_point_rounding(self):
        self.assertEqual(BitcoinAmount.parse("0.00408", DisplayUnit.BTC).sats, 408_000)

    def test_parses_grouped_satoshis(self):
        self.assertEqual(BitcoinAmount.parse("408,000", DisplayUnit.SATS).sats, 408_000)

    def test_rejects_sub_satoshi_amount(self):
        with self.assertRaisesRegex(ValueError, "fraction of a satoshi"):
            BitcoinAmount.parse("0.000000001", DisplayUnit.BTC)

    def test_formats_btc_and_satoshis(self):
        amount = BitcoinAmount(408_000)
        self.assertEqual(amount.format(DisplayUnit.BTC), "0.00408")
        self.assertEqual(amount.format(DisplayUnit.SATS), "408,000")
        self.assertEqual(BitcoinAmount(0).format(DisplayUnit.BTC), "0")


class WalletApplicationTests(TestCase):
    def setUp(self):
        self.application = WalletApplication(DemoWalletService())

    def test_send_preview_converts_amount_and_calculates_fee(self):
        preview = self.application.preview_send(
            "bc1qdestination", "0.001", DisplayUnit.BTC, 3
        )
        self.assertEqual(preview.amount.sats, 100_000)
        self.assertEqual(preview.fee.sats, 420)
        self.assertEqual(preview.total.sats, 100_420)

    def test_send_all_leaves_fee_out_of_destination_amount(self):
        preview = self.application.preview_send(
            "bc1qdestination", "", DisplayUnit.BTC, 3, send_all=True
        )
        self.assertEqual(preview.amount.sats, 407_580)
        self.assertEqual(preview.total.sats, 408_000)

    def test_rejects_spend_larger_than_balance(self):
        with self.assertRaisesRegex(ValueError, "exceed"):
            self.application.preview_send(
                "bc1qdestination", "1", DisplayUnit.BTC, 3
            )


class BitcoinToolWalletServiceTests(TestCase):
    def setUp(self):
        self.temporary_directory = TemporaryDirectory()
        data_directory = Path(self.temporary_directory.name)
        self.wallet_file = data_directory / "wallets.json"
        self.service = BitcoinToolWalletService(
            self.wallet_file,
            data_directory / "wallet_cache.json",
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_missing_wallet_has_zero_empty_snapshot(self):
        snapshot = self.service.snapshot()
        self.assertFalse(snapshot.is_initialized)
        self.assertEqual(snapshot.balance.sats, 0)
        self.assertEqual(snapshot.receive_address, "")
        self.assertEqual(snapshot.transactions, ())

    def test_create_wallet_encrypts_mnemonic_and_issues_one_receive_address(self):
        creation = self.service.create_wallet("alice", "correct horse battery staple")
        self.assertTrue(creation.snapshot.is_initialized)
        self.assertTrue(creation.snapshot.receive_address.startswith("bc1q"))
        self.assertEqual(len(creation.mnemonic.split()), 24)

        wallet_text = self.wallet_file.read_text(encoding="utf-8")
        self.assertIn('"encrypted": true', wallet_text)
        self.assertNotIn(creation.mnemonic, wallet_text)

        # Loading again reuses the current receive index instead of consuming it.
        address = creation.snapshot.receive_address
        self.assertEqual(self.service.snapshot().receive_address, address)

    def test_import_wallet_restores_the_same_receive_address(self):
        generated = self.service.create_wallet("source", "source-password")
        source_address = generated.snapshot.receive_address

        imported_file = Path(self.temporary_directory.name) / "imported-wallets.json"
        imported_service = BitcoinToolWalletService(
            imported_file,
            Path(self.temporary_directory.name) / "imported-cache.json",
            backend_factory=lambda network: FakeEsploraBackend(network),
        )
        imported = imported_service.create_wallet(
            "restored",
            "restored-password",
            generated.mnemonic,
        )

        self.assertTrue(imported.imported)
        self.assertEqual(imported.snapshot.receive_address, source_address)
        self.assertNotIn(generated.mnemonic, imported_file.read_text(encoding="utf-8"))

    def test_import_scans_receive_and_change_and_selects_next_unused(self):
        data_directory = Path(self.temporary_directory.name)
        discovered_service = BitcoinToolWalletService(
            data_directory / "discovered-wallets.json",
            data_directory / "discovered-cache.json",
            backend_factory=lambda network: FakeEsploraBackend(
                network,
                # Ordinal 3 is receive index 3; ordinal 22 is change index 2.
                funded_ordinals={3: 1_000, 22: 2_000},
            ),
        )
        words = self.service.create_wallet("seed", "seed-password").mnemonic
        imported = discovered_service.create_wallet(
            "discovered", "import-password", words
        )
        address_book = get_wallet_address_book(
            "discovered",
            wallet_file=data_directory / "discovered-wallets.json",
            address_type="p2wpkh",
        )
        receive_index_four = next(
            entry["address"]
            for entry in address_book["addresses"]
            if entry["branch"] == 0 and entry["index"] == 4
        )

        self.assertEqual(address_book["address_count"], 40)
        self.assertEqual(imported.snapshot.receive_address, receive_index_four)
        self.assertEqual(imported.snapshot.balance.sats, 3_000)
        self.assertEqual(len(imported.snapshot.transactions), 2)
        first_transaction = imported.snapshot.transactions[0]
        self.assertIsNotNone(first_transaction.block_time)
        self.assertEqual(first_transaction.received.sats, 2_000)
        self.assertEqual(first_transaction.sent.sats, 0)
        self.assertEqual(first_transaction.fee.sats, 100)
        self.assertEqual(first_transaction.confirmations, 11)
        self.assertTrue(first_transaction.confirmed)
        self.assertEqual(first_transaction.block_height, 90)
        self.assertTrue(first_transaction.explorer_url.startswith("https://mempool.space/tx/"))

    def test_testnet4_wallet_uses_an_isolated_tb1_receive_address(self):
        data_directory = Path(self.temporary_directory.name)
        testnet_service = BitcoinToolWalletService(
            data_directory / "wallets_testnet4.json",
            data_directory / "wallet_cache_testnet4.json",
            network=NETWORK_TESTNET4,
        )
        creation = testnet_service.create_wallet("testnet", "test-password")

        self.assertTrue(creation.snapshot.receive_address.startswith("tb1q"))
        self.assertTrue((data_directory / "wallets_testnet4.json").exists())
        self.assertFalse((data_directory / "wallets.json").exists())

    def test_testnet4_import_uses_the_same_discovery_flow(self):
        data_directory = Path(self.temporary_directory.name)
        testnet_service = BitcoinToolWalletService(
            data_directory / "restored_testnet4.json",
            data_directory / "restored_testnet4_cache.json",
            network=NETWORK_TESTNET4,
            backend_factory=lambda network: FakeEsploraBackend(
                network, funded_ordinals={1: 4_000, 20: 5_000}
            ),
        )
        words = self.service.create_wallet("seed2", "seed-password").mnemonic
        restored = testnet_service.create_wallet("testnet_restore", "password", words)

        self.assertTrue(restored.snapshot.receive_address.startswith("tb1q"))
        self.assertEqual(restored.snapshot.balance.sats, 9_000)
        self.assertEqual(len(restored.snapshot.transactions), 2)
        self.assertTrue(
            restored.snapshot.transactions[0].explorer_url.startswith(
                "https://mempool.space/testnet4/tx/"
            )
        )
