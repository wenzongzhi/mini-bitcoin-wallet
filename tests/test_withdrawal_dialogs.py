from types import SimpleNamespace
from unittest.mock import Mock

import withdrawal_dialogs


def test_declining_broadcast_retry_releases_the_active_payment(monkeypatch) -> None:
    """The no-TTL platform draft must not become unreachable from the UI."""

    state = SimpleNamespace(
        application=SimpleNamespace(broadcast_withdrawal=Mock()),
        cancel_withdrawal=Mock(),
    )
    review = SimpleNamespace(review_id="draft-123")

    def fail_immediately(_parent, **options):
        options["on_error"](RuntimeError("backend unavailable"))

    monkeypatch.setattr(withdrawal_dialogs, "_run_with_progress", fail_immediately)
    monkeypatch.setattr(
        withdrawal_dialogs.messagebox,
        "askretrycancel",
        lambda *args, **kwargs: False,
    )

    withdrawal_dialogs._broadcast_withdrawal(
        object(),
        object(),
        state,
        review,
    )

    state.cancel_withdrawal.assert_called_once_with("draft-123")
