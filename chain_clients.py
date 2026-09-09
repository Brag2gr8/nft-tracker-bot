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

import os
import logging
from datetime import datetime, timezone

import aiohttp

import database as db

log = logging.getLogger("nft-tracker-bot.chain_clients")

HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
HELIUS_TX_HISTORY_URL = "https://api.helius.xyz/v0/addresses/{address}/transactions"

ALCHEMY_API_KEY = os.getenv("ALCHEMY_API_KEY")
ALCHEMY_NETWORK_SLUG = {
    "ethereum": "eth-mainnet",
    "robinhood": "robinhood-mainnet",
}
EXPLORER_TX_URL = {
    "ethereum": "https://etherscan.io/tx/{hash}",
    "robinhood": "https://explorer.chain.robinhood.com/tx/{hash}",
}


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
    Routes to the right detection method depending on the EVM chain, since
    Alchemy's data APIs have different coverage per network:

    - Ethereum: uses `getNFTSales`, which returns already-parsed sale events
      (buyer, seller, price, marketplace) — no manual log decoding needed.
      This endpoint is NOT available on Robinhood Chain.

    - Robinhood Chain: falls back to the Transfers API (`alchemy_getAssetTransfers`),
      detecting raw NFT transfers in/out of the wallet, then best-effort
      matching a paired ETH payment in the same transaction to get a price.
      Since this chain is new, marketplace-specific sale parsing isn't
      available yet — this is the most reliable option today.
    """
    if not ALCHEMY_API_KEY:
        log.warning("ALCHEMY_API_KEY not set — skipping EVM wallet %s", wallet["address"])
        return []

    chain = wallet["chain"]
    subdomain = ALCHEMY_NETWORK_SLUG.get(chain)
    if not subdomain:
        log.error("No Alchemy network mapping for chain '%s'", chain)
        return []

    if chain == "ethereum":
        return await _get_ethereum_sales(wallet, subdomain)
    else:
        return await _get_evm_transfers(wallet, subdomain)


async def _get_ethereum_sales(wallet: dict, subdomain: str) -> list[dict]:
    """Ethereum path: Alchemy's getNFTSales endpoint (clean parsed sale data)."""
    address = wallet["address"]
    chain = wallet["chain"]
    base_url = f"https://{subdomain}.g.alchemy.com/nft/v3/{ALCHEMY_API_KEY}/getNFTSales"

    last_block_str = db.get_last_signature(wallet["id"])
    from_block = hex(int(last_block_str) + 1) if last_block_str else "0x0"

    all_sales = []
    async with aiohttp.ClientSession() as session:
        for role_param in ("buyerAddress", "sellerAddress"):
            params = {
                "fromBlock": from_block,
                "order": "asc",
                "limit": 100,
                role_param: address,
            }
            async with session.get(base_url, params=params) as resp:
                if resp.status == 429:
                    log.warning("Alchemy rate limit hit (429) on getNFTSales for %s — will retry next poll cycle", address)
                    continue
                if resp.status != 200:
                    body = await resp.text()
                    log.error("Alchemy error %s for %s (%s): %s", resp.status, address, role_param, body)
                    continue
                data = await resp.json()
                all_sales.extend(data.get("nftSales", []))

    if not all_sales:
        return []

    seen_keys = set()
    unique_sales = []
    for sale in all_sales:
        key = (sale.get("transactionHash"), sale.get("contractAddress"), sale.get("tokenId"))
        if key not in seen_keys:
            seen_keys.add(key)
            unique_sales.append(sale)

    unique_sales.sort(key=lambda s: int(s.get("blockNumber", 0)))

    events = []
    highest_block = int(last_block_str) if last_block_str else 0
    addr_lower = address.lower()

    for sale in unique_sales:
        block_num = int(sale.get("blockNumber", 0))
        highest_block = max(highest_block, block_num)

        buyer = (sale.get("buyerAddress") or "").lower()
        seller = (sale.get("sellerAddress") or "").lower()

        if buyer == addr_lower:
            event_type = "buy"
        elif seller == addr_lower:
            event_type = "sell"
        else:
            continue

        contract_address = sale.get("contractAddress")
        token_id = sale.get("tokenId")

        seller_fee = sale.get("sellerFee") or {}
        symbol = seller_fee.get("symbol", "ETH")
        decimals = seller_fee.get("decimals", 18)
        amount_raw = seller_fee.get("amount", "0")
        try:
            price = int(amount_raw) / (10 ** decimals)
        except (ValueError, TypeError):
            price = 0.0

        metadata = await _get_evm_nft_metadata(subdomain, contract_address, token_id)
        tx_hash = sale.get("transactionHash")

        events.append({
            "event_type": event_type,
            "contract_address": contract_address,
            "token_id": str(token_id),
            "collection_name": metadata.get("collection_name", "Unknown Collection"),
            "token_name": metadata.get("token_name", f"#{token_id}"),
            "image_url": metadata.get("image_url"),
            "price": price,
            "currency": symbol,
            "marketplace": sale.get("marketplace", "Unknown"),
            "chain": chain,
            "wallet": address,
            "tx_hash": tx_hash,
            "listing_url": EXPLORER_TX_URL[chain].format(hash=tx_hash),
            "timestamp": datetime.now(timezone.utc),
        })

    if highest_block and str(highest_block) != last_block_str:
        db.set_last_signature(wallet["id"], str(highest_block))

    return events


async def _get_evm_transfers(wallet: dict, subdomain: str) -> list[dict]:
    """
    Robinhood Chain (and any future EVM chain without getNFTSales support):
    detects NFT buy/sell activity via raw ERC721/ERC1155 transfers, then
    best-effort matches a paired native ETH transfer in the same
    transaction to determine the price paid.

    Price/marketplace detection here is less reliable than Ethereum's
    getNFTSales, since we're inferring from transfers rather than reading
    parsed marketplace events — expect "Unknown" price on some trades
    (e.g. trades routed through escrow contracts) until Alchemy adds
    dedicated sale-parsing support for this chain.
    """
    address = wallet["address"]
    chain = wallet["chain"]
    rpc_url = f"https://{subdomain}.g.alchemy.com/v2/{ALCHEMY_API_KEY}"

    last_block_str = db.get_last_signature(wallet["id"])
    from_block = hex(int(last_block_str) + 1) if last_block_str else "0x0"

    async def fetch_transfers(direction_key: str) -> list[dict]:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "alchemy_getAssetTransfers",
            "params": [{
                "fromBlock": from_block,
                "toBlock": "latest",
                direction_key: address,
                "category": ["erc721", "erc1155"],
                "withMetadata": True,
                "order": "asc",
                "maxCount": "0x64",
            }],
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(rpc_url, json=payload) as resp:
                if resp.status == 429:
                    log.warning("Alchemy rate limit hit (429) on getAssetTransfers for %s — will retry next poll cycle", address)
                    return []
                if resp.status != 200:
                    body = await resp.text()
                    log.error("Alchemy transfers error %s for %s: %s", resp.status, address, body)
                    return []
                data = await resp.json()
                return data.get("result", {}).get("transfers", [])

    incoming = await fetch_transfers("toAddress")   # candidate buys
    outgoing = await fetch_transfers("fromAddress")  # candidate sells

    combined = [(t, "buy") for t in incoming] + [(t, "sell") for t in outgoing]
    if not combined:
        return []

    combined.sort(key=lambda pair: int(pair[0].get("blockNum", "0x0"), 16))

    events = []
    highest_block = int(last_block_str) if last_block_str else 0

    for transfer, event_type in combined:
        block_num = int(transfer.get("blockNum", "0x0"), 16)
        highest_block = max(highest_block, block_num)

        tx_hash = transfer.get("hash")
        contract_address = transfer.get("rawContract", {}).get("address")
        token_id_hex = transfer.get("tokenId")
        try:
            token_id = str(int(token_id_hex, 16)) if token_id_hex else "0"
        except (ValueError, TypeError):
            token_id = str(token_id_hex)

        price, currency = await _find_paired_payment(rpc_url, tx_hash, address, event_type)
        metadata = await _get_evm_nft_metadata(subdomain, contract_address, token_id)

        block_timestamp = transfer.get("metadata", {}).get("blockTimestamp")
        try:
            timestamp = datetime.fromisoformat(block_timestamp.replace("Z", "+00:00")) if block_timestamp else datetime.now(timezone.utc)
        except ValueError:
            timestamp = datetime.now(timezone.utc)

        events.append({
            "event_type": event_type,
            "contract_address": contract_address,
            "token_id": token_id,
            "collection_name": metadata.get("collection_name", "Unknown Collection"),
            "token_name": metadata.get("token_name", f"#{token_id}"),
            "image_url": metadata.get("image_url"),
            "price": price if price is not None else "Unknown",
            "currency": currency,
            "marketplace": "Robinhood Chain",
            "chain": chain,
            "wallet": address,
            "tx_hash": tx_hash,
            "listing_url": EXPLORER_TX_URL[chain].format(hash=tx_hash),
            "timestamp": timestamp,
        })

    if highest_block and str(highest_block) != last_block_str:
        db.set_last_signature(wallet["id"], str(highest_block))

    return events


async def _find_paired_payment(rpc_url: str, tx_hash: str, wallet_address: str, event_type: str):
    """
    Best-effort: looks for a native ETH transfer in the same transaction as
    the NFT transfer, to infer the price paid. Returns (amount, "ETH") or
    (None, "ETH") if no matching payment is found (e.g. trade routed
    through an escrow/marketplace contract we can't trace here).
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "alchemy_getAssetTransfers",
        "params": [{
            "fromBlock": "0x0",
            "toBlock": "latest",
            "category": ["external", "internal"],
            "maxCount": "0x64",
            **({"fromAddress": wallet_address} if event_type == "buy" else {"toAddress": wallet_address}),
        }],
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(rpc_url, json=payload) as resp:
                if resp.status != 200:
                    return None, "ETH"
                data = await resp.json()
    except Exception:
        return None, "ETH"

    for t in data.get("result", {}).get("transfers", []):
        if t.get("hash") == tx_hash and t.get("value"):
            return t["value"], "ETH"

    return None, "ETH"


async def _get_evm_nft_metadata(subdomain: str, contract_address: str, token_id) -> dict:
    """
    Best-effort NFT metadata lookup (name, collection, image) via Alchemy's
    getNFTMetadata endpoint. Returns {} on any failure so the alert still
    goes out without enrichment rather than failing entirely.
    """
    if not ALCHEMY_API_KEY or not contract_address:
        return {}

    url = f"https://{subdomain}.g.alchemy.com/nft/v3/{ALCHEMY_API_KEY}/getNFTMetadata"
    params = {"contractAddress": contract_address, "tokenId": str(token_id)}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    return {}
                data = await resp.json()
    except Exception:
        log.exception("Failed to fetch Alchemy metadata for %s #%s", contract_address, token_id)
        return {}

    contract = data.get("contract", {})
    image = data.get("image", {})

    return {
        "token_name": data.get("name"),
        "collection_name": contract.get("name") or contract.get("openSeaMetadata", {}).get("collectionName"),
        "image_url": image.get("cachedUrl") or image.get("originalUrl"),
    }


async def _get_solana_events(wallet: dict) -> list[dict]:
    """
    Uses Helius's Enhanced Transactions API to fetch parsed transaction
    history for the wallet, filters for NFT_SALE events, and classifies
    each as a buy or sell depending on which side the wallet was on.

    Note: this endpoint is in "maintenance mode" per Helius's docs (still
    fully functional, just not receiving new parser types) but it's the
    simplest free-tier-compatible option since it returns already-parsed
    buyer/seller/amount data instead of raw instructions.
    """
    if not HELIUS_API_KEY:
        log.warning("HELIUS_API_KEY not set — skipping Solana wallet %s", wallet["address"])
        return []

    address = wallet["address"]
    last_signature = db.get_last_signature(wallet["id"])

    url = HELIUS_TX_HISTORY_URL.format(address=address)
    params = {"api-key": HELIUS_API_KEY, "limit": 100}
    if last_signature:
        # 'until' returns only transactions newer than this signature
        params["until"] = last_signature

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as resp:
            if resp.status == 429:
                log.warning("Helius rate limit hit (429) for %s — will retry next poll cycle", address)
                return []
            if resp.status != 200:
                body = await resp.text()
                log.error("Helius error %s for %s: %s", resp.status, address, body)
                return []
            transactions = await resp.json()

    if not transactions:
        return []

    # Helius returns newest-first; process oldest-first so alerts arrive in order
    transactions = list(reversed(transactions))

    events = []
    newest_signature = last_signature

    for tx in transactions:
        newest_signature = tx.get("signature", newest_signature)

        if tx.get("type") != "NFT_SALE":
            continue

        nft_event = tx.get("events", {}).get("nft")
        if not nft_event:
            continue

        buyer = nft_event.get("buyer")
        seller = nft_event.get("seller")

        if buyer == address:
            event_type = "buy"
        elif seller == address:
            event_type = "sell"
        else:
            # Involves this wallet's associated token account but not directly
            # buyer/seller (rare) — skip rather than mislabel.
            continue

        nfts = nft_event.get("nfts", [])
        mint = nfts[0]["mint"] if nfts else None
        if not mint:
            continue

        price_lamports = nft_event.get("amount", 0)
        price_sol = price_lamports / 1_000_000_000

        metadata = await _get_asset_metadata(mint)

        events.append({
            "event_type": event_type,
            "contract_address": mint,      # Solana NFTs are identified by mint address
            "token_id": mint,              # no separate token id concept on Solana
            "collection_name": metadata.get("collection_name", "Unknown Collection"),
            "token_name": metadata.get("token_name", "Unknown"),
            "image_url": metadata.get("image_url"),
            "price": price_sol,
            "currency": "SOL",
            "marketplace": tx.get("source", "Unknown"),
            "chain": "solana",
            "wallet": address,
            "tx_hash": tx.get("signature"),
            "listing_url": f"https://solscan.io/tx/{tx.get('signature')}",
            "timestamp": datetime.fromtimestamp(tx.get("timestamp", 0), tz=timezone.utc),
        })

    if newest_signature and newest_signature != last_signature:
        db.set_last_signature(wallet["id"], newest_signature)

    return events


async def _get_asset_metadata(mint: str) -> dict:
    """
    Fetches NFT name/collection/image via Helius's DAS API (getAsset).
    Best-effort — returns an empty dict on failure so a sale alert still
    goes out even if metadata enrichment fails.
    """
    if not HELIUS_API_KEY:
        return {}

    url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
    payload = {
        "jsonrpc": "2.0",
        "id": "das-lookup",
        "method": "getAsset",
        "params": {"id": mint},
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    return {}
                data = await resp.json()
    except Exception:
        log.exception("Failed to fetch DAS metadata for mint %s", mint)
        return {}

    result = data.get("result", {})
    content = result.get("content", {})
    metadata = content.get("metadata", {})
    files = content.get("files", [])
    grouping = result.get("grouping", [])

    collection_name = None
    for g in grouping:
        if g.get("group_key") == "collection":
            collection_name = g.get("collection_metadata", {}).get("name")

    return {
        "token_name": metadata.get("name"),
        "collection_name": collection_name,
        "image_url": files[0]["uri"] if files else content.get("links", {}).get("image"),
    }


# ---------------------------------------------------------------------------
# Live "current holdings" lookup — used by /holdings to show what a wallet
# actually owns right now, straight from the chain. This is independent of
# our own buy/sell event tracking, so it stays accurate even for NFTs
# acquired before the wallet was registered with the bot, or missed by a
# polling gap.
# ---------------------------------------------------------------------------

async def get_current_holdings(wallet: dict) -> list[dict]:
    """
    Returns a list of NFTs currently held by the wallet, normalized as:
    {
        "collection_name": str,
        "token_name": str,
        "contract_address": str,
        "token_id": str,
        "image_url": str,
    }
    """
    chain = wallet["chain"]
    if chain in ("ethereum", "robinhood"):
        return await _get_evm_holdings(wallet)
    elif chain == "solana":
        return await _get_solana_holdings(wallet)
    else:
        return []


async def _get_evm_holdings(wallet: dict) -> list[dict]:
    """Uses Alchemy's getNFTsForOwner endpoint — works the same on Ethereum and Robinhood Chain."""
    if not ALCHEMY_API_KEY:
        log.warning("ALCHEMY_API_KEY not set — cannot fetch holdings for %s", wallet["address"])
        return []

    chain = wallet["chain"]
    subdomain = ALCHEMY_NETWORK_SLUG.get(chain)
    if not subdomain:
        return []

    address = wallet["address"]
    url = f"https://{subdomain}.g.alchemy.com/nft/v3/{ALCHEMY_API_KEY}/getNFTsForOwner"

    holdings = []
    page_key = None

    async with aiohttp.ClientSession() as session:
        while True:
            params = {"owner": address, "withMetadata": "true", "pageSize": 100}
            if page_key:
                params["pageKey"] = page_key

            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log.error("Alchemy getNFTsForOwner error %s for %s: %s", resp.status, address, body)
                    break
                data = await resp.json()

            for nft in data.get("ownedNfts", []):
                contract = nft.get("contract", {})
                image = nft.get("image", {})
                holdings.append({
                    "collection_name": contract.get("name") or contract.get("openSeaMetadata", {}).get("collectionName") or "Unknown Collection",
                    "token_name": nft.get("name") or f"#{nft.get('tokenId')}",
                    "contract_address": contract.get("address"),
                    "token_id": nft.get("tokenId"),
                    "image_url": image.get("cachedUrl") or image.get("originalUrl"),
                })

            page_key = data.get("pageKey")
            if not page_key:
                break

    return holdings


async def _get_solana_holdings(wallet: dict) -> list[dict]:
    """Uses Helius's DAS API (getAssetsByOwner) to list current NFT holdings."""
    if not HELIUS_API_KEY:
        log.warning("HELIUS_API_KEY not set — cannot fetch holdings for %s", wallet["address"])
        return []

    address = wallet["address"]
    url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"

    holdings = []
    page = 1

    async with aiohttp.ClientSession() as session:
        while True:
            payload = {
                "jsonrpc": "2.0",
                "id": "holdings-lookup",
                "method": "getAssetsByOwner",
                "params": {
                    "ownerAddress": address,
                    "page": page,
                    "limit": 100,
                },
            }
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log.error("Helius getAssetsByOwner error %s for %s: %s", resp.status, address, body)
                    break
                data = await resp.json()

            result = data.get("result", {})
            items = result.get("items", [])

            for asset in items:
                content = asset.get("content", {})
                metadata = content.get("metadata", {})
                files = content.get("files", [])
                grouping = asset.get("grouping", [])

                collection_name = None
                for g in grouping:
                    if g.get("group_key") == "collection":
                        collection_name = g.get("collection_metadata", {}).get("name")

                holdings.append({
                    "collection_name": collection_name or "Unknown Collection",
                    "token_name": metadata.get("name") or "Unknown",
                    "contract_address": asset.get("id"),  # mint address
                    "token_id": asset.get("id"),
                    "image_url": files[0]["uri"] if files else content.get("links", {}).get("image"),
                })

            total = result.get("total", 0)
            if page * 100 >= total or not items:
                break
            page += 1

    return holdings


# ---------------------------------------------------------------------------
# Floor price lookup — used by /floorprice
# ---------------------------------------------------------------------------

async def get_floor_price(chain: str, contract_address: str):
    """
    Returns (floor_price: float, currency: str, marketplace: str) or
    (None, None, None) if unavailable.
    """
    if chain in ("ethereum", "robinhood"):
        return await _get_evm_floor_price(chain, contract_address)
    elif chain == "solana":
        return await _get_solana_floor_price(contract_address)
    return None, None, None


async def _get_evm_floor_price(chain: str, contract_address: str):
    """
    Uses Alchemy's getFloorPrice endpoint. Note: like getNFTSales, marketplace
    floor aggregation may have limited coverage on newer chains like Robinhood
    Chain — treat a missing result as "not available yet" rather than an error.
    """
    if not ALCHEMY_API_KEY:
        return None, None, None

    subdomain = ALCHEMY_NETWORK_SLUG.get(chain)
    if not subdomain:
        return None, None, None

    url = f"https://{subdomain}.g.alchemy.com/nft/v3/{ALCHEMY_API_KEY}/getFloorPrice"
    params = {"contractAddress": contract_address}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as resp:
                if resp.status == 429:
                    log.warning("Alchemy rate limit hit on getFloorPrice for %s", contract_address)
                    return None, None, None
                if resp.status != 200:
                    return None, None, None
                data = await resp.json()
    except Exception:
        log.exception("Failed to fetch floor price for %s", contract_address)
        return None, None, None

    # Response has one sub-object per marketplace (e.g. openSea, looksRare) —
    # pick the first one that didn't error out.
    for marketplace, info in data.items():
        if isinstance(info, dict) and "error" not in info and info.get("floorPrice") is not None:
            return info["floorPrice"], info.get("priceCurrency", "ETH"), marketplace

    return None, None, None


async def _get_solana_floor_price(mint_or_collection: str):
    """
    Uses Magic Eden's public API (no key required) to look up a collection's
    floor price. Expects a collection symbol, not a mint address — see the
    /floorprice command's help text for how the user should supply this.
    """
    url = f"https://api-mainnet.magiceden.dev/v2/collections/{mint_or_collection}/stats"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None, None, None
                data = await resp.json()
    except Exception:
        log.exception("Failed to fetch Magic Eden floor price for %s", mint_or_collection)
        return None, None, None

    floor_lamports = data.get("floorPrice")
    if floor_lamports is None:
        return None, None, None

    return floor_lamports / 1_000_000_000, "SOL", "Magic Eden"


# ---------------------------------------------------------------------------
# Gas cost lookup — best-effort enrichment for EVM buy/sell alerts
# ---------------------------------------------------------------------------

async def get_gas_cost(chain: str, tx_hash: str):
    """
    Returns gas cost in ETH (float) for a given transaction, or None if
    unavailable. Uses the standard eth_getTransactionReceipt RPC method,
    which is part of Alchemy's core node access (distinct from their
    higher-level "Transaction Receipts" data API, which has narrower
    per-chain coverage) — so this works on Ethereum and should also work
    on Robinhood Chain as a basic RPC call.
    """
    if not ALCHEMY_API_KEY:
        return None

    subdomain = ALCHEMY_NETWORK_SLUG.get(chain)
    if not subdomain:
        return None

    rpc_url = f"https://{subdomain}.g.alchemy.com/v2/{ALCHEMY_API_KEY}"
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_getTransactionReceipt",
        "params": [tx_hash],
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(rpc_url, json=payload) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
    except Exception:
        log.exception("Failed to fetch gas receipt for %s", tx_hash)
        return None

    result = data.get("result")
    if not result:
        return None

    try:
        gas_used = int(result["gasUsed"], 16)
        effective_gas_price = int(result["effectiveGasPrice"], 16)
        return (gas_used * effective_gas_price) / 1e18
    except (KeyError, ValueError, TypeError):
        return None
