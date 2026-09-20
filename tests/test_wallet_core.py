import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from adapters import BitcoinToolWalletService
from app_settings import ApplicationSettingsStore
from btc.chainparams import NETWORK_TESTNET4
from explorer_links import transaction_explorer_url
from tests.ui_test.demo_wallet import DemoWalletService
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
            "mempool_stats": {"tx_count": 0},
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

    def get_all_address_transactions(self, address):
        """The fake dataset is complete, so its one page is the full history."""

        return self.get_address_transactions(address)

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

    def test_prepare_withdrawal_converts_amount_and_calculates_fee(self):
        draft = self.application.prepare_withdrawal(
            "bc1qdestination", "0.001", DisplayUnit.BTC, 3
        )
        self.assertEqual(draft.amount.sats, 100_000)
        self.assertEqual(draft.estimated_fee.sats, 420)
        self.assertEqual(draft.amount.sats + draft.estimated_fee.sats, 100_420)

    def test_send_all_leaves_fee_out_of_destination_amount(self):
        draft = self.application.prepare_withdrawal(
            "bc1qdestination", "", DisplayUnit.BTC, 3, send_all=True
        )
        self.assertEqual(draft.amount.sats, 407_580)
        self.assertEqual(draft.amount.sats + draft.estimated_fee.sats, 408_000)

    def test_rejects_spend_larger_than_balance(self):
        with self.assertRaisesRegex(ValueError, "exceed"):
            self.application.prepare_withdrawal(
                "bc1qdestination", "1", DisplayUnit.BTC, 3
            )

    def test_create_and_import_are_separate_and_import_does_not_return_words(self):
        created = self.application.create_wallet("Created", "password")
        imported = self.application.import_wallet(
            "Imported",
            "password",
            "  demo   mnemonic  ",
        )

        self.assertEqual(created.generated_mnemonic, "demo mnemonic")
        self.assertIsNone(imported.generated_mnemonic)
        self.assertFalse(hasattr(imported, "mnemonic"))
        self.assertNotIn("demo mnemonic", repr(imported))
        self.assertFalse(hasattr(self.application, "preview_send"))

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
        self.settings_store = ApplicationSettingsStore(
            data_directory / "settings.json"
        )
        self.settings_store.ensure_exists()
        self.service = BitcoinToolWalletService(
            self.wallet_file,
            data_directory / "wallet_cache.json",
            settings_store=self.settings_store,
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
        self.assertIs(self.service.settings_store, self.settings_store)
        self.assertIsNone(self.settings_store.active_wallet("mainnet"))

    def test_multiple_wallets_can_coexist_and_be_listed_without_secrets(self):
        mnemonics = []
        for name in ("Wallet_A", "Wallet_B", "Wallet_C"):
            mnemonics.append(
                self.service.create_wallet(
                    name, f"password-{name}"
                ).generated_mnemonic
            )

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

    def test_duplicate_import_name_never_removes_the_existing_wallet(self):
        existing = self.service.create_wallet("Existing", "original-password")

        with self.assertRaisesRegex(ValueError, "already exists"):
            self.service.import_wallet(
                "Existing",
                "different-password",
                existing.generated_mnemonic,
            )

        self.assertEqual(
            [wallet.name for wallet in self.service.list_wallets()],
            ["Existing"],
        )
        self.assertEqual(
            self.service.get_mnemonic("original-password"),
            existing.generated_mnemonic,
        )

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
            settings_store=self.settings_store,
        )

        self.assertEqual(restarted.snapshot().name, "Wallet_B")

    def test_missing_active_wallet_falls_back_to_first_wallet(self):
        self.service.create_wallet("Wallet_A", "password-a")
        self.service.create_wallet("Wallet_B", "password-b")
        self.settings_store.set_active_wallet("mainnet", "missing-wallet")

        restarted = BitcoinToolWalletService(
            self.wallet_file,
            self.wallet_file.parent / "wallet_cache.json",
            settings_store=self.settings_store,
        )

        self.assertEqual(restarted.snapshot().name, "Wallet_A")
        self.assertEqual(self.settings_store.active_wallet("mainnet"), "Wallet_A")

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
            settings_store=self.settings_store,
            network=NETWORK_TESTNET4,
        )
        testnet_service.create_wallet("TestWallet", "test-password")

        self.assertEqual(self.settings_store.active_wallet("mainnet"), "MainWallet")
        self.assertEqual(self.settings_store.active_wallet("testnet4"), "TestWallet")

    def test_create_wallet_encrypts_mnemonic_and_issues_one_receive_address(self):
        creation = self.service.create_wallet("alice", "correct horse battery staple")
        self.assertTrue(creation.snapshot.is_initialized)
        self.assertTrue(creation.snapshot.receive_address.startswith("bc1q"))
        self.assertEqual(len(creation.generated_mnemonic.split()), 24)

        wallet_text = self.wallet_file.read_text(encoding="utf-8")
        self.assertIn('"encrypted": true', wallet_text)
        self.assertNotIn(creation.generated_mnemonic, wallet_text)

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
            settings_store=self.settings_store,
            backend_factory=lambda network: FakeEsploraBackend(network),
        )
        imported = imported_service.import_wallet(
            "restored",
            "restored-password",
            generated.generated_mnemonic,
        )

        self.assertIsNone(imported.generated_mnemonic)
        self.assertEqual(imported.snapshot.receive_address, source_address)
        self.assertNotIn(
            generated.generated_mnemonic,
            imported_file.read_text(encoding="utf-8"),
        )

    def test_import_scans_receive_and_change_and_selects_next_unused(self):
        data_directory = Path(self.temporary_directory.name)
        discovery_backend = FakeEsploraBackend(
            funded_ordinals={3: 1_000, 26: 2_000}
        )
        discovered_service = BitcoinToolWalletService(
            data_directory / "discovered-wallets.json",
            data_directory / "discovered-cache.json",
            settings_store=self.settings_store,
            # Reuse one ordinal-based fake so an address keeps the same
            # identity between lightweight discovery and the following sync.
            backend_factory=lambda _network: discovery_backend,
        )
        words = self.service.create_wallet(
            "seed", "seed-password"
        ).generated_mnemonic
        imported = discovered_service.import_wallet(
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

        # Persist only receive 0..3 plus the next receive address, and change
        # history 0..2. The trailing discovery gap is never written.
        self.assertEqual(address_book["address_count"], 8)
        self.assertEqual(imported.snapshot.receive_address, receive_index_four)
        self.assertIsNotNone(imported.discovery)
        self.assertEqual(imported.discovery.receive_scanned, 24)
        self.assertEqual(imported.discovery.change_scanned, 23)
        self.assertEqual(imported.discovery.receive_used, 1)
        self.assertEqual(imported.discovery.change_used, 1)
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
        self.assertEqual(first_transaction.network, "mainnet")
        self.assertTrue(
            transaction_explorer_url(first_transaction.network, first_transaction.txid)
            .startswith("https://mempool.space/tx/")
        )

    def test_testnet4_wallet_uses_an_isolated_tb1_receive_address(self):
        data_directory = Path(self.temporary_directory.name)
        testnet_service = BitcoinToolWalletService(
            data_directory / "wallets_testnet4.json",
            data_directory / "wallet_cache_testnet4.json",
            settings_store=self.settings_store,
            network=NETWORK_TESTNET4,
        )
        creation = testnet_service.create_wallet("testnet", "test-password")

        self.assertTrue(creation.snapshot.receive_address.startswith("tb1q"))
        self.assertTrue((data_directory / "wallets_testnet4.json").exists())
        self.assertFalse((data_directory / "wallets.json").exists())

    def test_testnet4_import_uses_the_same_discovery_flow(self):
        data_directory = Path(self.temporary_directory.name)
        discovery_backend = FakeEsploraBackend(
            NETWORK_TESTNET4,
            funded_ordinals={1: 4_000, 23: 5_000},
        )
        testnet_service = BitcoinToolWalletService(
            data_directory / "restored_testnet4.json",
            data_directory / "restored_testnet4_cache.json",
            settings_store=self.settings_store,
            network=NETWORK_TESTNET4,
            backend_factory=lambda _network: discovery_backend,
        )
        words = self.service.create_wallet(
            "seed2", "seed-password"
        ).generated_mnemonic
        restored = testnet_service.import_wallet(
            "testnet_restore", "password", words
        )

        self.assertTrue(restored.snapshot.receive_address.startswith("tb1q"))
        self.assertIsNotNone(restored.discovery)
        self.assertEqual(restored.discovery.receive_scanned, 22)
        self.assertEqual(restored.discovery.change_scanned, 22)
        self.assertEqual(restored.snapshot.balance.sats, 9_000)
        self.assertEqual(len(restored.snapshot.transactions), 2)
        self.assertTrue(
            transaction_explorer_url(
                restored.snapshot.transactions[0].network,
                restored.snapshot.transactions[0].txid,
            ).startswith("https://mempool.space/testnet4/tx/")
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
            settings_store=self.settings_store,
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
        self.assertEqual(
            transaction_explorer_url(result.network, result.txid),
            f"https://mempool.space/tx/{review.txid}",
        )
        self.assertEqual(sum(item.transaction_query_count for item in backends), 0)
        snapshot = service.snapshot()
        self.assertEqual(snapshot.pending_txids, (review.txid,))
        self.assertEqual(snapshot.transactions[0].txid, review.txid)
        self.assertEqual(
            sum(item.txid == review.txid for item in snapshot.transactions),
            1,
        )
        self.assertFalse(snapshot.transactions[0].confirmed)
        self.assertEqual(snapshot.authoritative_balance.sats, 100_000)
        self.assertEqual(snapshot.pending_delta.sats, -review.fee.sats)
        self.assertEqual(snapshot.balance.sats, 100_000 - review.fee.sats)
        self.assertEqual(snapshot.available_balance.sats, 0)

    def test_withdrawal_utxo_sync_preserves_cached_transaction_history(self):
        backend = FakeEsploraBackend(funded_ordinals={0: 100_000})
        service = BitcoinToolWalletService(
            self.wallet_file,
            self.wallet_file.parent / "history-cache.json",
            settings_store=self.settings_store,
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
            settings_store=self.settings_store,
            backend_factory=lambda _network: backend,
        )
        service.create_wallet("SyncWallet", "password")

        snapshot = service.synchronize_wallet("SyncWallet")
        status = service.transaction_status("ab" * 32)

        self.assertEqual(snapshot.balance.sats, 12_345)
        self.assertEqual(snapshot.authoritative_balance.sats, 12_345)
        self.assertEqual(snapshot.confirmed_balance.sats, 12_345)
        self.assertEqual(snapshot.unconfirmed_chain_balance.sats, 0)
        self.assertEqual(snapshot.pending_delta.sats, 0)
        self.assertEqual(snapshot.available_balance.sats, 12_345)
        self.assertIsNotNone(snapshot.synced_at)
        self.assertEqual(backend.transaction_query_count, 1)
        self.assertTrue(status.confirmed)
        self.assertEqual(status.block_height, 101)

    def test_preflight_rejects_invalid_address_and_overspend_before_signing(self):
        service = BitcoinToolWalletService(
            self.wallet_file,
            self.wallet_file.parent / "preflight-cache.json",
            settings_store=self.settings_store,
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
            settings_store=self.settings_store,
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

    def test_change_reservation_survives_sync_and_cancel_reuses_index(self):
        cache_file = self.wallet_file.parent / "reservation-cache.json"
        service = BitcoinToolWalletService(
            self.wallet_file,
            cache_file,
            settings_store=self.settings_store,
            backend_factory=lambda network: FakeEsploraBackend(
                network, funded_ordinals={0: 100_000}
            ),
        )
        service.create_wallet("ReservationWallet", "password")
        destination = get_new_address(
            "ReservationWallet", wallet_file=self.wallet_file, network="mainnet"
        )["address"]

        first = service.prepare_withdrawal(
            destination,
            BitcoinAmount(25_000),
            2,
        )
        cache = json.loads(cache_file.read_text(encoding="utf-8"))
        wallet_cache = cache["wallets"]["ReservationWallet"]
        self.assertEqual(wallet_cache["reserved_change"]["index"], 0)
        self.assertEqual(
            wallet_cache["reserved_change"]["draft_id"],
            first.draft_id,
        )
        stored = json.loads(self.wallet_file.read_text(encoding="utf-8"))[
            "ReservationWallet"
        ]
        self.assertEqual(
            stored["accounts"]["bip84-account-0"]["next_change_index"],
            0,
        )

        service.synchronize_wallet("ReservationWallet")
        synchronized_cache = json.loads(cache_file.read_text(encoding="utf-8"))[
            "wallets"
        ]["ReservationWallet"]
        self.assertEqual(
            synchronized_cache["reserved_change"]["draft_id"],
            first.draft_id,
        )
        with self.assertRaisesRegex(ValueError, "active payment draft"):
            service.prepare_withdrawal(
                destination,
                BitcoinAmount(20_000),
                2,
            )

        service.cancel_withdrawal(first.draft_id)
        replacement = service.prepare_withdrawal(
            destination,
            BitcoinAmount(20_000),
            2,
        )
        cache = json.loads(cache_file.read_text(encoding="utf-8"))
        self.assertEqual(
            cache["wallets"]["ReservationWallet"]["reserved_change"]["index"],
            0,
        )
        service.cancel_withdrawal(replacement.draft_id)

    def test_sign_issues_change_and_cancel_never_reclaims_it(self):
        cache_file = self.wallet_file.parent / "issued-change-cache.json"
        service = BitcoinToolWalletService(
            self.wallet_file,
            cache_file,
            settings_store=self.settings_store,
            backend_factory=lambda network: FakeEsploraBackend(
                network, funded_ordinals={0: 100_000}
            ),
        )
        service.create_wallet("IssuedChangeWallet", "password")
        destination = get_new_address(
            "IssuedChangeWallet", wallet_file=self.wallet_file, network="mainnet"
        )["address"]
        draft = service.prepare_withdrawal(
            destination,
            BitcoinAmount(25_000),
            2,
        )
        review = service.sign_withdrawal(draft.draft_id, "password")

        stored = json.loads(self.wallet_file.read_text(encoding="utf-8"))[
            "IssuedChangeWallet"
        ]
        account = stored["accounts"]["bip84-account-0"]
        self.assertEqual(account["next_change_index"], 1)
        issued = next(
            entry
            for entry in account["issued_addresses"]
            if entry["branch"] == 1 and entry["index"] == 0
        )
        self.assertNotIn("lifecycle_state", issued)
        cache = json.loads(cache_file.read_text(encoding="utf-8"))["wallets"][
            "IssuedChangeWallet"
        ]
        self.assertNotIn("reserved_change", cache)
        self.assertTrue(cache["reserved_outpoints"])

        service.cancel_withdrawal(review.review_id)
        replacement = service.prepare_withdrawal(
            destination,
            BitcoinAmount(20_000),
            2,
        )
        cache = json.loads(cache_file.read_text(encoding="utf-8"))["wallets"][
            "IssuedChangeWallet"
        ]
        self.assertEqual(cache["reserved_change"]["index"], 1)
        service.cancel_withdrawal(replacement.draft_id)

    def test_failed_import_is_rolled_back_and_can_be_retried(self):
        class OfflineDiscoveryBackend(FakeEsploraBackend):
            def get_address(self, address):
                raise OSError("backend offline")

        words = self.service.create_wallet(
            "Seed", "seed-password"
        ).generated_mnemonic
        data_directory = self.wallet_file.parent
        imported_service = BitcoinToolWalletService(
            data_directory / "retry-wallets.json",
            data_directory / "retry-cache.json",
            settings_store=self.settings_store,
            backend_factory=lambda network: OfflineDiscoveryBackend(network),
        )

        with self.assertRaisesRegex(ValueError, "was rolled back"):
            imported_service.import_wallet(
                "RetryWallet",
                "password",
                words,
            )
        self.assertEqual(imported_service.list_wallets(), ())

        retry_service = BitcoinToolWalletService(
            data_directory / "retry-wallets.json",
            data_directory / "retry-cache.json",
            settings_store=self.settings_store,
            backend_factory=lambda network: FakeEsploraBackend(network),
        )
        retried = retry_service.import_wallet("RetryWallet", "password", words)
        self.assertIsNone(retried.generated_mnemonic)

    def test_testnet4_withdrawal_uses_isolated_wallet_and_explorer(self):
        data_directory = self.wallet_file.parent
        wallet_file = data_directory / "wallets_testnet4.json"
        service = BitcoinToolWalletService(
            wallet_file,
            data_directory / "send-cache-testnet4.json",
            settings_store=self.settings_store,
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
            transaction_explorer_url(result.network, result.txid),
            f"https://mempool.space/testnet4/tx/{review.txid}",
        )

    def test_wallet_lifecycle_preserves_secret_and_cache_until_removal(self):
        backend = FakeEsploraBackend(funded_ordinals={0: 12_345})
        service = BitcoinToolWalletService(
            self.wallet_file,
            self.wallet_file.parent / "lifecycle-cache.json",
            settings_store=self.settings_store,
            backend_factory=lambda _network: backend,
        )
        creation = service.create_wallet("Before", "old-password")
        service.synchronize_wallet("Before")

        renamed = service.rename_wallet("After", "old-password")
        self.assertEqual(renamed.name, "After")
        self.assertEqual(renamed.balance.sats, 12_345)
        self.assertEqual(
            service.get_mnemonic("old-password"),
            creation.generated_mnemonic,
        )
        self.assertEqual([wallet.name for wallet in service.list_wallets()], ["After"])

        service.change_password("old-password", "new-password")
        with self.assertRaises(ValueError):
            service.get_mnemonic("old-password")
        self.assertEqual(
            service.get_mnemonic("new-password"),
            creation.generated_mnemonic,
        )

        service.remove_wallet("new-password")
        self.assertFalse(service.snapshot().is_initialized)
        cache = json.loads(
            (self.wallet_file.parent / "lifecycle-cache.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertNotIn("After", cache["wallets"])

    def test_wallet_lifecycle_rejects_wrong_password_without_mutation(self):
        self.service.create_wallet("Protected", "correct-password")

        with self.assertRaises(ValueError):
            self.service.rename_wallet("Changed", "wrong-password")
        with self.assertRaises(ValueError):
            self.service.change_password("wrong-password", "new-password")
        with self.assertRaises(ValueError):
            self.service.remove_wallet("wrong-password")

        self.assertEqual([wallet.name for wallet in self.service.list_wallets()], ["Protected"])
