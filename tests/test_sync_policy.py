from sync_coordinator import SyncPolicy


def test_transaction_polling_slows_after_fast_window() -> None:
    policy = SyncPolicy()

    assert policy.status_interval(0) == 30
    assert policy.status_interval(599) == 30
    assert policy.status_interval(600) == 120


def test_sync_backoff_is_exponential_and_capped() -> None:
    policy = SyncPolicy(maximum_backoff=900)

    assert [policy.retry_delay(attempt) for attempt in range(1, 7)] == [
        30,
        60,
        120,
        240,
        480,
        900,
    ]
