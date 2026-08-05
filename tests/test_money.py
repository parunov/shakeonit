import pytest

from sharebudget.money import format_money, parse_amount, split_amount


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("10", 1000), ("10,25", 1025), (" 1 250.5 ", 125050), ("0.009", 1)],
)
def test_parse_amount(raw, expected):
    assert parse_amount(raw) == expected


@pytest.mark.parametrize("raw", ["0", "-1", "text", "NaN", "Infinity"])
def test_parse_amount_rejects_invalid(raw):
    with pytest.raises(ValueError):
        parse_amount(raw)


def test_split_amount_preserves_every_cent():
    shares = split_amount(1000, [30, 10, 20])
    assert shares == {10: 334, 20: 333, 30: 333}
    assert sum(shares.values()) == 1000


def test_format_money():
    assert format_money(-123456, "EUR") == "−1 234.56 €"
