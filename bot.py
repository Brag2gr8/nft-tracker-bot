"""
bot.py

Main entry point for the NFT purchase/sale tracker bot.

Commands:
  /setwallet chain address       - start tracking a wallet
  /removewallet chain address    - stop tracking a wallet
  /mywallets                     - list your tracked wallets
  /holdings chain address        - show NFTs currently held (live, on-chain)
  /floorprice chain identifier   - check a collection's current floor price
  /portfolio                     - summary across all your tracked wallets
  /summary                       - manual buy/sell digest for the last 7 days
  /status                        - bot health check
  /testalert                     - send yourself a sample alert to confirm DMs work
  /help                          - list all commands

Blockchain-specific logic lives in chain_clients.py
(Ethereum/Robinhood Chain via Alchemy, Solana via Helius).
"""

import os
import re
import asyncio
import logging
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
from aiohttp import web

import database as db
import chain_clients

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "180"))  # default 3 min
HEALTHCHECK_PORT = int(os.getenv("PORT", "8080"))  # Railway sets PORT automatically for web-exposed services
MAX_WALLETS_PER_USER = int(os.getenv("MAX_WALLETS_PER_USER", "10"))

SUPPORTED_CHAINS = {"ethereum", "solana", "robinhood"}
CHAIN_CHOICES = [
    app_commands.Choice(name="Ethereum", value="ethereum"),
    app_commands.Choice(name="Solana", value="solana"),
    app_commands.Choice(name="Robinhood Chain", value="robinhood"),
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("nft-tracker-bot")


class TrackerBot(commands.Bot):
    async def setup_hook(self):
        # Start a tiny HTTP server so external uptime monitors (e.g. UptimeRobot)
        # can ping the bot and alert you if it goes down. Note: on Railway this
        # only receives external traffic if the service has a public domain
        # generated and/or the Procfile process type is "web" rather than
        # "worker" — check Railway's networking settings if pings don't reach it.
        app = web.Application()
        app.router.add_get("/", self._health_handler)
        app.router.add_get("/ping", self._health_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", HEALTHCHECK_PORT)
        await site.start()
        log.info(f"Healthcheck server listening on port {HEALTHCHECK_PORT}")

    async def _health_handler(self, request):
        return web.json_response({
            "status": "ok",
            "poll_running": poll_wallets.is_running(),
            "bot_ready": self.is_ready(),
        })


intents = discord.Intents.default()
bot = TrackerBot(command_prefix="!", intents=intents)


def format_wallet(address: str) -> str:
    if len(address) <= 10:
        return address
    return f"{address[:6]}...{address[-4:]}"


def build_alert_embed(nft_data: dict, event_type: str) -> discord.Embed:
    """event_type is 'buy' or 'sell'."""
    if event_type == "buy":
        title = "🟢 NFT Purchased"
        color = discord.Color.green()
    else:
        title = "🔴 NFT Sold"
        color = discord.Color.red()

    embed = discord.Embed(
        title=title,
        description=f"**{nft_data.get('collection_name', 'Unknown Collection')}**",
        color=color,
        timestamp=nft_data.get("timestamp", datetime.now(timezone.utc)),
    )
    if nft_data.get("image_url"):
        embed.set_thumbnail(url=nft_data["image_url"])

    embed.add_field(name="NFT", value=nft_data.get("token_name", "Unknown"), inline=True)
    embed.add_field(
        name="Price",
        value=f"{nft_data.get('price', '?')} {nft_data.get('currency', '')}".strip(),
        inline=True,
    )
    embed.add_field(name="Marketplace", value=nft_data.get("marketplace", "Unknown"), inline=True)
    embed.add_field(name="Chain", value=nft_data.get("chain", "Unknown").capitalize(), inline=True)
    embed.add_field(name="Wallet", value=f"`{format_wallet(nft_data.get('wallet', ''))}`", inline=True)

    pnl = None
    if event_type == "sell" and nft_data.get("purchase_price") is not None and isinstance(nft_data.get("price"), (int, float)):
        pnl = nft_data["price"] - nft_data["purchase_price"]
        sign = "+" if pnl >= 0 else ""
        embed.add_field(
            name="P&L",
            value=f"{sign}{pnl:.4f} {nft_data.get('currency', '')}".strip(),
            inline=True,
        )

    if nft_data.get("gas_cost") is not None:
        embed.add_field(name="Gas Fee", value=f"{nft_data['gas_cost']:.5f} ETH", inline=True)

    links = []
    if nft_data.get("listing_url"):
        links.append(f"[Explorer]({nft_data['listing_url']})")
    if nft_data.get("opensea_url"):
        links.append(f"[OpenSea]({nft_data['opensea_url']})")
    if links:
        embed.add_field(name="Links", value=" • ".join(links), inline=False)

    return embed


async def send_alert_dm(user_id: str, nft_data: dict, event_type: str):
    try:
        user = await bot.fetch_user(int(user_id))
        embed = build_alert_embed(nft_data, event_type)
        await user.send(embed=embed)
        log.info(f"Sent {event_type} alert to user {user_id}")
    except discord.Forbidden:
        log.warning(f"Could not DM user {user_id} — DMs likely closed.")
    except Exception as e:
        log.exception(f"Failed to send alert to {user_id}: {e}")


async def send_digest_dm(user_id: str, events: list[dict], period_label: str):
    """Sends a summary DM covering the given events, used by both the
    weekly scheduled digest and the manual /summary command."""
    try:
        user = await bot.fetch_user(int(user_id))
    except Exception:
        log.exception(f"Could not fetch user {user_id} for digest")
        return

    if not events:
        try:
            await user.send(f"📊 **{period_label} summary**: no buy/sell activity.")
        except discord.Forbidden:
            pass
        return

    buys = [e for e in events if e["event_type"] == "buy"]
    sells = [e for e in events if e["event_type"] == "sell"]
    total_pnl = sum(e["pnl"] for e in sells if e.get("pnl") is not None)

    lines = [f"📊 **{period_label} summary**", f"Buys: {len(buys)} | Sells: {len(sells)}"]
    if any(e.get("pnl") is not None for e in sells):
        sign = "+" if total_pnl >= 0 else ""
        lines.append(f"Net P&L (tracked sells): {sign}{total_pnl:.4f}")
    lines.append("")

    for e in events[:20]:  # cap for message length
        icon = "🟢" if e["event_type"] == "buy" else "🔴"
        price_str = f"{e['price']} {e['currency']}" if e.get("price") is not None else "Unknown price"
        lines.append(f"{icon} {e.get('token_name', 'NFT')} ({e.get('collection_name', 'Unknown')}) — {price_str}")

    try:
        await user.send("\n".join(lines))
    except discord.Forbidden:
        log.warning(f"Could not DM digest to user {user_id} — DMs likely closed.")


# ---------------- Slash Commands ----------------

EVM_ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
SOLANA_ADDRESS_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")  # base58, roughly


def is_valid_address(chain: str, address: str) -> bool:
    if chain in ("ethereum", "robinhood"):
        return bool(EVM_ADDRESS_RE.match(address))
    elif chain == "solana":
        return bool(SOLANA_ADDRESS_RE.match(address)) and "..." not in address
    return True


@bot.tree.command(name="setwallet", description="Start tracking a wallet for NFT buy/sell activity")
@app_commands.describe(chain="Which blockchain this wallet is on", address="Wallet address to track")
@app_commands.choices(chain=CHAIN_CHOICES)
async def setwallet(interaction: discord.Interaction, chain: app_commands.Choice[str], address: str):
    chain = chain.value

    if not is_valid_address(chain, address):
        await interaction.response.send_message(
            "That doesn't look like a valid, full wallet address. "
            "Make sure you're pasting the complete address, not a shortened "
            "version like `0x1234...abcd`.",
            ephemeral=True,
        )
        return

    current_count = db.count_wallets_for_user(str(interaction.user.id))
    if current_count >= MAX_WALLETS_PER_USER:
        await interaction.response.send_message(
            f"You've hit the limit of {MAX_WALLETS_PER_USER} tracked wallets. "
            f"Remove one with `/removewallet` before adding another.",
            ephemeral=True,
        )
        return

    added = db.add_wallet(str(interaction.user.id), chain, address)
    if added:
        await interaction.response.send_message(
            f"✅ Now tracking `{format_wallet(address)}` on **{chain}**. You'll get a DM on any NFT buy/sell.",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            f"You're already tracking `{format_wallet(address)}` on **{chain}**.",
            ephemeral=True,
        )


@bot.tree.command(name="removewallet", description="Stop tracking a wallet")
@app_commands.describe(chain="Which blockchain this wallet is on", address="Select the wallet to remove")
@app_commands.choices(chain=CHAIN_CHOICES)
async def removewallet(interaction: discord.Interaction, chain: app_commands.Choice[str], address: str):
    chain = chain.value
    removed = db.remove_wallet(str(interaction.user.id), chain, address)
    if removed:
        await interaction.response.send_message(
            f"🗑️ Stopped tracking `{format_wallet(address)}` on **{chain}**.", ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "Couldn't find that wallet in your tracked list.", ephemeral=True
        )


@removewallet.autocomplete("address")
async def removewallet_address_autocomplete(interaction: discord.Interaction, current: str):
    """Populates the address field with the user's own tracked wallets,
    filtered to whichever chain they've already picked (if any), and
    matched against whatever they've typed so far."""
    wallets = db.get_wallets_for_user(str(interaction.user.id))

    selected_chain = None
    try:
        selected_chain = interaction.namespace.chain
    except AttributeError:
        pass
    if isinstance(selected_chain, app_commands.Choice):
        selected_chain = selected_chain.value

    if selected_chain:
        wallets = [w for w in wallets if w["chain"] == selected_chain]

    current_lower = current.lower()
    matches = [w for w in wallets if current_lower in w["address"].lower()]

    return [
        app_commands.Choice(name=f"{w['chain']} — {format_wallet(w['address'])}", value=w["address"])
        for w in matches[:25]  # Discord caps autocomplete results at 25
    ]


@bot.tree.command(name="mywallets", description="List your tracked wallets")
async def mywallets(interaction: discord.Interaction):
    wallets = db.get_wallets_for_user(str(interaction.user.id))
    if not wallets:
        await interaction.response.send_message("You aren't tracking any wallets yet. Use `/setwallet` to add one.", ephemeral=True)
        return

    lines = [f"• **{w['chain']}** — `{format_wallet(w['address'])}`" for w in wallets]
    await interaction.response.send_message(
        f"Your tracked wallets ({len(wallets)}/{MAX_WALLETS_PER_USER}):\n" + "\n".join(lines),
        ephemeral=True,
    )


@bot.tree.command(name="holdings", description="Show NFTs currently held in a tracked wallet (live, on-chain)")
@app_commands.describe(chain="Which blockchain this wallet is on", address="Select a tracked wallet")
@app_commands.choices(chain=CHAIN_CHOICES)
async def holdings(interaction: discord.Interaction, chain: app_commands.Choice[str], address: str):
    chain = chain.value
    wallets = db.get_wallets_for_user(str(interaction.user.id))
    match = next((w for w in wallets if w["chain"] == chain.lower() and w["address"] == address), None)
    if not match:
        await interaction.response.send_message("That wallet isn't in your tracked list.", ephemeral=True)
        return

    # Live lookups can take a couple seconds (pagination on larger wallets),
    # so defer to avoid Discord's 3-second interaction timeout.
    await interaction.response.defer(ephemeral=True)

    try:
        live_holdings = await chain_clients.get_current_holdings(match)
    except Exception:
        log.exception(f"Failed to fetch live holdings for wallet {match['id']}")
        await interaction.followup.send("Couldn't fetch holdings right now — try again in a bit.", ephemeral=True)
        return

    if not live_holdings:
        await interaction.followup.send("This wallet doesn't currently hold any NFTs (per on-chain data).", ephemeral=True)
        return

    # Cross-reference against our own purchase-price records where we have them
    recorded = {
        (h["contract_address"], str(h["token_id"])): h
        for h in db.get_holdings_for_wallet(match["id"])
    }

    lines = []
    for nft in live_holdings[:30]:  # cap to keep the message under Discord's limit
        key = (nft["contract_address"], str(nft["token_id"]))
        record = recorded.get(key)
        price_note = f" — bought at {record['purchase_price']} {record['purchase_currency'] or ''}" if record else ""
        lines.append(f"• {nft['token_name']} ({nft['collection_name']}){price_note}")

    header = f"Current holdings ({len(live_holdings)} total"
    header += ", showing first 30)" if len(live_holdings) > 30 else ")"

    await interaction.followup.send(header + ":\n" + "\n".join(lines), ephemeral=True)


@holdings.autocomplete("address")
async def holdings_address_autocomplete(interaction: discord.Interaction, current: str):
    """Same autocomplete pattern as /removewallet — shows the user's own
    tracked wallets instead of requiring exact manual retyping."""
    wallets = db.get_wallets_for_user(str(interaction.user.id))

    selected_chain = None
    try:
        selected_chain = interaction.namespace.chain
    except AttributeError:
        pass
    if isinstance(selected_chain, app_commands.Choice):
        selected_chain = selected_chain.value

    if selected_chain:
        wallets = [w for w in wallets if w["chain"] == selected_chain]

    current_lower = current.lower()
    matches = [w for w in wallets if current_lower in w["address"].lower()]

    return [
        app_commands.Choice(name=f"{w['chain']} — {format_wallet(w['address'])}", value=w["address"])
        for w in matches[:25]
    ]


@bot.tree.command(name="floorprice", description="Check a collection's current floor price")
@app_commands.describe(
    chain="Which blockchain the collection is on",
    identifier="Contract address (Ethereum/Robinhood Chain) or Magic Eden collection symbol (Solana)",
)
@app_commands.choices(chain=CHAIN_CHOICES)
async def floorprice(interaction: discord.Interaction, chain: app_commands.Choice[str], identifier: str):
    chain = chain.value
    await interaction.response.defer(ephemeral=True)

    try:
        price, currency, marketplace = await chain_clients.get_floor_price(chain, identifier)
    except Exception:
        log.exception(f"Failed to fetch floor price for {identifier} on {chain}")
        await interaction.followup.send("Couldn't fetch floor price right now — try again in a bit.", ephemeral=True)
        return

    if price is None:
        note = ""
        if chain == "robinhood":
            note = " (Robinhood Chain is new — floor price aggregation may not be available yet for this collection)"
        await interaction.followup.send(f"No floor price data found for that collection.{note}", ephemeral=True)
        return

    await interaction.followup.send(
        f"💎 Floor price: **{price} {currency}** (via {marketplace})", ephemeral=True
    )


@bot.tree.command(name="portfolio", description="Summary of NFT holdings across all your tracked wallets")
async def portfolio(interaction: discord.Interaction):
    wallets = db.get_wallets_for_user(str(interaction.user.id))
    if not wallets:
        await interaction.response.send_message("You aren't tracking any wallets yet.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    lines = [f"**Your portfolio** ({len(wallets)} wallet{'s' if len(wallets) != 1 else ''} tracked)", ""]
    total_nfts = 0

    for w in wallets:
        try:
            held = await chain_clients.get_current_holdings(w)
        except Exception:
            log.exception(f"Failed to fetch holdings for portfolio, wallet {w['id']}")
            held = []
        total_nfts += len(held)
        lines.append(f"• **{w['chain']}** `{format_wallet(w['address'])}` — {len(held)} NFT{'s' if len(held) != 1 else ''}")

    lines.append("")
    lines.append(f"Total: **{total_nfts}** NFTs across all tracked wallets")
    lines.append("_Note: floor-value estimates aren't included yet — use `/floorprice` per collection for pricing._")

    await interaction.followup.send("\n".join(lines), ephemeral=True)


@bot.tree.command(name="summary", description="Get a buy/sell digest for the last 7 days")
async def summary(interaction: discord.Interaction):
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    events = db.get_events_since(str(interaction.user.id), since)
    await interaction.response.send_message("Sending your summary as a DM...", ephemeral=True)
    await send_digest_dm(str(interaction.user.id), events, "Last 7 days")


@bot.tree.command(name="status", description="Check bot health")
async def status(interaction: discord.Interaction):
    running = "running ✅" if poll_wallets.is_running() else "stopped ⚠️"
    await interaction.response.send_message(
        f"Bot online. Poll loop: {running}. Interval: {POLL_INTERVAL_SECONDS}s.",
        ephemeral=True,
    )


@bot.tree.command(name="testalert", description="Send yourself a sample alert to confirm DMs work")
async def testalert(interaction: discord.Interaction):
    sample = {
        "collection_name": "Example Collection",
        "token_name": "Example #1234",
        "price": 0.5,
        "currency": "ETH",
        "marketplace": "OpenSea",
        "chain": "ethereum",
        "wallet": "0x1234567890abcdef1234567890abcdef12345678",
        "listing_url": "https://opensea.io",
        "timestamp": datetime.now(timezone.utc),
    }
    await send_alert_dm(str(interaction.user.id), sample, "buy")
    await interaction.response.send_message("Sent — check your DMs.", ephemeral=True)


@bot.tree.command(name="help", description="List all available commands")
async def help_command(interaction: discord.Interaction):
    lines = [
        "**NFT Tracker Bot — Commands**",
        "`/setwallet <chain> <address>` — start tracking a wallet",
        "`/removewallet <chain> <address>` — stop tracking a wallet",
        "`/mywallets` — list your tracked wallets",
        "`/holdings <chain> <address>` — live NFT holdings for a tracked wallet",
        "`/floorprice <chain> <identifier>` — check a collection's floor price",
        "`/portfolio` — NFT count summary across all your tracked wallets",
        "`/summary` — buy/sell digest for the last 7 days",
        "`/status` — bot health check",
        "`/testalert` — send yourself a sample alert to confirm DMs work",
        f"\nWallet limit: {MAX_WALLETS_PER_USER} per user.",
    ]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


# ---------------- Background polling ----------------

@tasks.loop(seconds=POLL_INTERVAL_SECONDS)
async def poll_wallets():
    wallets = db.get_all_wallets()
    for wallet in wallets:
        try:
            events = await chain_clients.get_new_nft_events(wallet)
        except Exception as e:
            log.exception(f"Error polling wallet {wallet['address']} on {wallet['chain']}: {e}")
            continue

        for event in events:
            try:
                # Best-effort gas cost enrichment for EVM chains only
                if wallet["chain"] in ("ethereum", "robinhood") and event.get("tx_hash"):
                    try:
                        event["gas_cost"] = await chain_clients.get_gas_cost(wallet["chain"], event["tx_hash"])
                    except Exception:
                        log.exception(f"Failed to fetch gas cost for tx {event.get('tx_hash')}")

                pnl = None
                if event["event_type"] == "buy":
                    db.add_holding(
                        wallet_id=wallet["id"],
                        contract_address=event["contract_address"],
                        token_id=event["token_id"],
                        collection_name=event.get("collection_name"),
                        token_name=event.get("token_name"),
                        purchase_price=event.get("price"),
                        purchase_currency=event.get("currency"),
                    )
                    await send_alert_dm(wallet["user_id"], event, "buy")

                elif event["event_type"] == "sell":
                    held = db.pop_holding(wallet["id"], event["contract_address"], event["token_id"])
                    if held and isinstance(event.get("price"), (int, float)) and held.get("purchase_price") is not None:
                        event["purchase_price"] = held["purchase_price"]
                        pnl = event["price"] - held["purchase_price"]
                    await send_alert_dm(wallet["user_id"], event, "sell")

                db.log_event(
                    wallet_id=wallet["id"],
                    user_id=wallet["user_id"],
                    event_type=event["event_type"],
                    chain=wallet["chain"],
                    collection_name=event.get("collection_name"),
                    token_name=event.get("token_name"),
                    price=event.get("price"),
                    currency=event.get("currency"),
                    pnl=pnl,
                )
            except Exception:
                log.exception(f"Failed to process event for wallet {wallet['address']}: {event}")
                continue


@poll_wallets.before_loop
async def before_poll():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    db.init_db()
    await bot.tree.sync()
    if not poll_wallets.is_running():
        poll_wallets.start()
    log.info(f"Logged in as {bot.user}. Slash commands synced. Polling every {POLL_INTERVAL_SECONDS}s.")


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("DISCORD_TOKEN not set. Copy .env.example to .env and fill it in.")
    bot.run(DISCORD_TOKEN)

