import pytest

from app.runtime.display import account_display


def test_account_display_is_stable_and_separate():
    assert account_display(":99", 1) == ":99"
    assert account_display(":99", 2) == ":100"
    assert account_display(":99.0", 2) == ":100.0"


@pytest.mark.parametrize("base,account", [(":99", 0), (":9999", 2), ("remote:0", 1)])
def test_account_display_rejects_invalid_identity(base, account):
    with pytest.raises(ValueError):
        account_display(base, account)
