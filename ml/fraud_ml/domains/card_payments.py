"""Card-payments fraud domain adapter.

This is the first (and currently only) implemented fraud domain. It encodes everything that is
specific to the Sparkov-style card transaction data: the merchant category vocabulary, how a
payment channel is derived, and which raw columns are retained vs. dropped.

Other domains (account takeover, insurance claims, ...) must provide their own adapter, schema,
features and separately trained/evaluated models. Nothing here should be assumed to transfer.
"""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "card_payments"

# Merchant categories present in the source dataset. The scoring API rejects anything else (422)
# instead of silently scoring an out-of-vocabulary category.
MERCHANT_CATEGORIES: Final[tuple[str, ...]] = (
    "entertainment",
    "food_dining",
    "gas_transport",
    "grocery_net",
    "grocery_pos",
    "health_fitness",
    "home",
    "kids_pets",
    "misc_net",
    "misc_pos",
    "personal_care",
    "shopping_net",
    "shopping_pos",
    "travel",
)

CHANNELS: Final[tuple[str, ...]] = ("card_not_present", "card_present", "unspecified")


def derive_channel(category: str) -> str:
    """Derive the payment channel from the category suffix.

    The source data has no explicit channel field. Categories ending in ``_net`` are online
    (card-not-present) and ``_pos`` are point-of-sale (card-present). All other categories do not
    say, so they are ``unspecified`` rather than guessed.
    """
    if category.endswith("_net"):
        return "card_not_present"
    if category.endswith("_pos"):
        return "card_present"
    return "unspecified"


# The dataset does not state a currency. Sparkov simulates US customers, so USD is an assumption
# recorded here and in docs/data/dataset-selection.md.
ASSUMED_CURRENCY: Final = "USD"
