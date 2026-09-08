"""
chain_clients.py

This module is responsible for checking a wallet for NEW NFT buy/sell events
since the last time we checked, and returning them in a normalized format
that bot.py knows how to turn into an alert.

STATUS: stubbed out. Each function currently returns an empty list.
Next step is to implement:
  - _get_ethereum_events()   -> via Alchemy NFT API (also covers Robinhood Chain)
  - _get_solana_events()     -> via Helius API

Normalized event dict shape (what get_new_nft_events should return per event):
{
    "event_type": "buy" | "sell",
    "contract_address": str,
    "token_id": str,
    "collection_name": str,
    "token_name": str,
    "image_url": str,
    "price": float,
    "currency": str,
    "marketplace": str,
    "chain": str,
    "wallet": str,
    "listing_url": str,
    "timestamp": datetime,
}
"""

from datetime import datetime, timezone
import database as db


async def get_new_nft_events(wallet: dict) -> list[dict]:
    """
    wallet is a dict from database.py, e.g.:
    {"id": 1, "user_id": "123", "chain": "ethereum", "address": "0x...", "added_at": "..."}

    Returns a list of normalized event dicts (see module docstring).
    Empty list means "nothing new since last check."
    """
    chain = wallet["chain"]

    if chain in ("ethereum", "robinhood"):
        return await _get_evm_events(wallet)
    elif chain == "solana":
        return await _get_solana_events(wallet)
    else:
        return []


async def _get_evm_events(wallet: dict) -> list[dict]:
    """
    TODO: implement using Alchemy NFT API.
    Alchemy supports both Ethereum mainnet and Robinhood Chain, so this
    function will take an extra step of picking the right network endpoint
    based on wallet['chain'].
    """
    # Placeholder — no events yet until implemented.
    return []


async def _get_solana_events(wallet: dict) -> list[dict]:
    """
    TODO: implement using Helius API (enhanced transactions / webhooks).
    """
    # Placeholder — no events yet until implemented.
    return []
