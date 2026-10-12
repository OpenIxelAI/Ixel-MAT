"""Sonnet 5.5 and Haiku 5.5 are priced, so a run on an API key shows what it cost."""
from ixel_mat.usage import price_for


def test_sonnet_5_5_has_its_own_price_not_sonnet_5s():
    price = price_for("claude-sonnet-5-5")
    assert (price.input, price.output, price.read) == (2.0, 10.0, 0.20)


def test_haiku_5_5_is_priced():
    price = price_for("claude-haiku-5-5")
    assert (price.input, price.output) == (0.10, 0.50)
