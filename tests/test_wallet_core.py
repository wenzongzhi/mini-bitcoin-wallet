import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from adapters import BitcoinToolWalletService, DemoWalletService
from app_settings import ApplicationSettingsStore
from btc.chainparams import NETWORK_TESTNET4
from tx.codec import deserialize_transaction_hex, transaction_txid
from wallet_core import BitcoinAmount, DisplayUnit, WalletApplication
from wallet import get_new_address, get_wallet_address_book


class FakeEsploraBackend:
    """Deterministic backend for address-discovery integration tests."""

    def __init__(
        self,
        network="mainnet",
        funded_ordinals=None,
        transaction_confirmed=False,
    ):
        self.network = network
        self.base_url = "https://example.invalid/api"
        self.funded_ordinals = funded_ordinals or {}
        self._address_ordinals = {}
        self.transaction_query_count = 0
        self.transaction_confirmed = transaction_confirmed

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
        self.transaction_query_count += 1
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

    def broadcast_transaction(self, raw_tx_hex):
        """Accept a valid transaction and return its locally computed TXID."""

        transaction = deserialize_transaction_hex(raw_tx_hex)
        return transaction_txid(transaction)

    def get_transaction_status(self, txid):
        return {
            "confirmed": self.transaction_confirmed,
            **(
                {"block_height": 101, "block_time": 1_700_000_100}
                if self.transaction_confirmed
                else {}
            ),
        }


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

    def test_lists_and_selects_wallet_through_application_boundary(self):
        wallets = self.application.list_wallets()

        self.assertEqual([wallet.name for wallet in wallets], ["Bitcoin Wallet"])
        self.assertEqual(
            self.application.select_wallet("Bitcoin Wallet").name,
            "Bitcoin Wallet",
        )

    def test_rejects_zero_fee_before_transaction_preparation(self):
        with self.assertRaisesRegex(ValueError, "Zero-fee"):
            self.application.prepare_withdrawal(
                "bc1qdestination", "0.001", DisplayUnit.BTC, 0
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
        self.assertEqual(self.service.list_wallets(), ())
        settings = json.loads(
            (self.wallet_file.parent / "settings.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            settings,
            {
                "version": 1,
                "active_wallets": {"mainnet": None, "testnet4": None},
            },
        )

    def test_multiple_wallets_can_coexist_and_be_listed_without_secrets(self):
        mnemonics = []
        for name in ("Wallet_A", "Wallet_B", "Wallet_C"):
            mnemonics.append(self.service.create_wallet(name, f"password-{name}").mnemonic)

        summaries = self.service.list_wallets()

        self.assertEqual(
            [wallet.name for wallet in summaries],
            ["Wallet_A", "Wallet_B", "Wallet_C"],
        )
        self.assertTrue(all(wallet.encrypted for wallet in summaries))
        serialized_summaries = repr(summaries)
        self.assertTrue(all(mnemonic not in serialized_summaries for mnemonic in mnemonics))
        settings_text = (self.wallet_file.parent / "settings.json").read_text(
            encoding="utf-8"
        )
        self.assertTrue(all(mnemonic not in settings_text for mnemonic in mnemonics))
        self.assertNotIn('"encryption"', settings_text)

    def test_switching_wallets_changes_the_active_snapshot(self):
        self.service.create_wallet("Wallet_A", "password-a")
        self.service.create_wallet("Wallet_B", "password-b")

        self.assertEqual(self.service.select_wallet("Wallet_B").name, "Wallet_B")
        self.assertEqual(self.service.select_wallet("Wallet_A").name, "Wallet_A")
        self.assertEqual(self.service.snapshot().name, "Wallet_A")

    def test_active_wallet_selection_survives_service_restart(self):
        self.service.create_wallet("Wallet_A", "password-a")
        self.service.create_wallet("Wallet_B", "password-b")
        self.service.select_wallet("Wallet_B")

        restarted = BitcoinToolWalletService(
            self.wallet_file,
            self.wallet_file.parent / "wallet_cache.json",
        )

        self.assertEqual(restarted.snapshot().name, "Wallet_B")

    def test_missing_active_wallet_falls_back_to_first_wallet(self):
        self.service.create_wallet("Wallet_A", "password-a")
        self.service.create_wallet("Wallet_B", "password-b")
        settings = ApplicationSettingsStore(self.wallet_file.parent / "settings.json")
        settings.set_active_wallet("mainnet", "missing-wallet")

        restarted = BitcoinToolWalletService(
            self.wallet_file,
            self.wallet_file.parent / "wallet_cache.json",
        )

        self.assertEqual(restarted.snapshot().name, "Wallet_A")
        self.assertEqual(settings.active_wallet("mainnet"), "Wallet_A")

    def test_selecting_unknown_wallet_does_not_change_active_wallet(self):
        self.service.create_wallet("Wallet_A", "password-a")

        with self.assertRaisesRegex(ValueError, "does not exist"):
            self.service.select_wallet("missing-wallet")

        self.assertEqual(self.service.snapshot().name, "Wallet_A")

    def test_mainnet_and_testnet4_remember_independent_active_wallets(self):
        data_directory = self.wallet_file.parent
        self.service.create_wallet("MainWallet", "main-password")
        testnet_service = BitcoinToolWalletService(
            data_directory / "wallets_testnet4.json",
            data_directory / "wallet_cache_testnet4.json",
            network=NETWORK_TESTNET4,
            settings_file=data_directory / "settings.json",
        )
        testnet_service.create_wallet("TestWallet", "test-password")

        settings = ApplicationSettingsStore(data_directory / "settings.json")
        self.assertEqual(settings.active_wallet("mainnet"), "MainWallet")
        self.assertEqual(settings.active_wallet("testnet4"), "TestWallet")

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

    def test_selected_wallet_prepares_and_broadcasts_withdrawal(self):
        backends = []

        def funded_backend(network):
            backend = FakeEsploraBackend(network, funded_ordinals={0: 100_000})
            backends.append(backend)
            return backend

        service = BitcoinToolWalletService(
            self.wallet_file,
            self.wallet_file.parent / "send-cache.json",
            backend_factory=funded_backend,
        )
        service.create_wallet("Wallet_A", "password-a")
        service.create_wallet("Wallet_B", "password-b")
        service.select_wallet("Wallet_B")
        destination = get_new_address(
            "Wallet_B", wallet_file=self.wallet_file, network="mainnet"
        )["address"]

        draft = service.prepare_withdrawal(
            destination,
            BitcoinAmount(25_000),
            2,
        )
        review = service.sign_withdrawal(draft.draft_id, "password-b")

        self.assertEqual(review.wallet_name, "Wallet_B")
        self.assertEqual(review.amount.sats, 25_000)
        self.assertGreater(review.fee.sats, 0)
        result = service.broadcast_withdrawal(review.review_id)
        self.assertEqual(result.txid, review.txid)
        self.assertEqual(result.explorer_url, f"https://mempool.space/tx/{review.txid}")
        self.assertEqual(sum(item.transaction_query_count for item in backends), 0)
        snapshot = service.snapshot()
        self.assertEqual(snapshot.pending_txids, (review.txid,))
        self.assertEqual(snapshot.transactions[0].txid, review.txid)
        self.assertFalse(snapshot.transactions[0].confirmed)

    def test_withdrawal_utxo_sync_preserves_cached_transaction_history(self):
        backend = FakeEsploraBackend(funded_ordinals={0: 100_000})
        service = BitcoinToolWalletService(
            self.wallet_file,
            self.wallet_file.parent / "history-cache.json",
            backend_factory=lambda _network: backend,
        )
        service.create_wallet("HistoryWallet", "password")
        before = service.synchronize_wallet("HistoryWallet").transactions
        destination = get_new_address(
            "HistoryWallet", wallet_file=self.wallet_file, network="mainnet"
        )["address"]

        draft = service.prepare_withdrawal(
            destination,
            BitcoinAmount(25_000),
            2,
        )
        after = service.snapshot().transactions

        self.assertEqual([item.txid for item in after], [item.txid for item in before])
        service.cancel_withdrawal(draft.draft_id)

    def test_full_sync_and_lightweight_transaction_status_are_separate(self):
        backend = FakeEsploraBackend(
            funded_ordinals={0: 12_345}, transaction_confirmed=True
        )
        service = BitcoinToolWalletService(
            self.wallet_file,
            self.wallet_file.parent / "sync-cache.json",
            backend_factory=lambda _network: backend,
        )
        service.create_wallet("SyncWallet", "password")

        snapshot = service.synchronize_wallet("SyncWallet")
        status = service.transaction_status("ab" * 32)

        self.assertEqual(snapshot.balance.sats, 12_345)
        self.assertIsNotNone(snapshot.synced_at)
        self.assertEqual(backend.transaction_query_count, 1)
        self.assertTrue(status.confirmed)
        self.assertEqual(status.block_height, 101)

    def test_preflight_rejects_invalid_address_and_overspend_before_signing(self):
        service = BitcoinToolWalletService(
            self.wallet_file,
            self.wallet_file.parent / "preflight-cache.json",
            backend_factory=lambda network: FakeEsploraBackend(
                network, funded_ordinals={0: 100_000}
            ),
        )
        service.create_wallet("PreflightWallet", "password")

        with self.assertRaisesRegex(ValueError, "address"):
            service.prepare_withdrawal("123456", BitcoinAmount(10_000), 2)

        destination = get_new_address(
            "PreflightWallet", wallet_file=self.wallet_file, network="mainnet"
        )["address"]
        with self.assertRaisesRegex(ValueError, "insufficient funds"):
            service.prepare_withdrawal(
                destination,
                BitcoinAmount(100_000),
                2,
            )

    def test_max_withdrawal_spends_balance_and_cancel_releases_reservation(self):
        cache_file = self.wallet_file.parent / "max-cache.json"
        service = BitcoinToolWalletService(
            self.wallet_file,
            cache_file,
            backend_factory=lambda network: FakeEsploraBackend(
                network, funded_ordinals={0: 100_000}
            ),
        )
        service.create_wallet("MaxWallet", "password")
        destination = get_new_address(
            "MaxWallet", wallet_file=self.wallet_file, network="mainnet"
        )["address"]

        draft = service.prepare_withdrawal(
            destination,
            None,
            2,
            send_all=True,
        )

        self.assertTrue(draft.send_all)
        self.assertEqual(draft.amount.sats + draft.estimated_fee.sats, 100_000)
        service.cancel_withdrawal(draft.draft_id)
        cache = json.loads(cache_file.read_text(encoding="utf-8"))
        self.assertEqual(
            cache["wallets"]["MaxWallet"].get("reserved_outpoints", {}),
            {},
        )

    def test_testnet4_withdrawal_uses_isolated_wallet_and_explorer(self):
        data_directory = self.wallet_file.parent
        wallet_file = data_directory / "wallets_testnet4.json"
        service = BitcoinToolWalletService(
            wallet_file,
            data_directory / "send-cache-testnet4.json",
            network=NETWORK_TESTNET4,
            backend_factory=lambda network: FakeEsploraBackend(
                network, funded_ordinals={0: 50_000}
            ),
        )
        service.create_wallet("TestnetWallet", "password")
        destination = get_new_address(
            "TestnetWallet",
            wallet_file=wallet_file,
            network=NETWORK_TESTNET4,
        )["address"]

        draft = service.prepare_withdrawal(
            destination,
            BitcoinAmount(10_000),
            2,
        )
        review = service.sign_withdrawal(draft.draft_id, "password")
        result = service.broadcast_withdrawal(review.review_id)

        self.assertEqual(review.network, NETWORK_TESTNET4)
        self.assertEqual(
            result.explorer_url,
            f"https://mempool.space/testnet4/tx/{review.txid}",
        )
