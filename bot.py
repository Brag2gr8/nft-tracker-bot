"""
bot.py

Main entry point for the NFT purchase/sale tracker bot.

Commands:
  /setwallet chain address     - start tracking a wallet
  /removewallet chain address  - stop tracking a wallet
  /mywallets                   - list your tracked wallets
  /holdings chain address      - show NFTs currently held (per our records)
  /status                      - bot health check
  /testalert                   - send yourself a sample alert to confirm DMs work

This file intentionally does NOT contain any blockchain-specific logic yet.
That lives in chain_clients.py (Ethereum/Robinhood via Alchemy, Solana via Helius),
which is stubbed out for now with placeholder functions we'll fill in next.
"""

import os
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

import database as db
import chain_clients

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "180"))  # default 3 min

SUPPORTED_CHAINS = {"ethereum", "solana", "robinhood"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("nft-tracker-bot")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


def is_valid_chain(chain: str) -> bool:
    return chain.lower() in SUPPORTED_CHAINS


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

    if event_type == "sell" and nft_data.get("purchase_price") is not None:
        pnl = nft_data["price"] - nft_data["purchase_price"]
        sign = "+" if pnl >= 0 else ""
        embed.add_field(
            name="P&L",
            value=f"{sign}{pnl:.4f} {nft_data.get('currency', '')}".strip(),
            inline=True,
        )

    if nft_data.get("listing_url"):
        embed.add_field(name="Link", value=f"[View]({nft_data['listing_url']})", inline=False)

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


# ---------------- Slash Commands ----------------

@bot.tree.command(name="setwallet", description="Start tracking a wallet for NFT buy/sell activity")
@app_commands.describe(chain="ethereum, solana, or robinhood", address="Wallet address to track")
async def setwallet(interaction: discord.Interaction, chain: str, address: str):
    if not is_valid_chain(chain):
        await interaction.response.send_message(
            f"Unsupported chain `{chain}`. Choose from: {', '.join(SUPPORTED_CHAINS)}",
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
@app_commands.describe(chain="ethereum, solana, or robinhood", address="Wallet address to remove")
async def removewallet(interaction: discord.Interaction, chain: str, address: str):
    removed = db.remove_wallet(str(interaction.user.id), chain, address)
    if removed:
        await interaction.response.send_message(
            f"🗑️ Stopped tracking `{format_wallet(address)}` on **{chain}**.", ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "Couldn't find that wallet in your tracked list.", ephemeral=True
        )


@bot.tree.command(name="mywallets", description="List your tracked wallets")
async def mywallets(interaction: discord.Interaction):
    wallets = db.get_wallets_for_user(str(interaction.user.id))
    if not wallets:
        await interaction.response.send_message("You aren't tracking any wallets yet. Use `/setwallet` to add one.", ephemeral=True)
        return

    lines = [f"• **{w['chain']}** — `{format_wallet(w['address'])}`" for w in wallets]
    await interaction.response.send_message("Your tracked wallets:\n" + "\n".join(lines), ephemeral=True)


@bot.tree.command(name="holdings", description="Show NFTs currently held in a tracked wallet (per our records)")
@app_commands.describe(chain="ethereum, solana, or robinhood", address="Wallet address")
async def holdings(interaction: discord.Interaction, chain: str, address: str):
    wallets = db.get_wallets_for_user(str(interaction.user.id))
    match = next((w for w in wallets if w["chain"] == chain.lower() and w["address"] == address), None)
    if not match:
        await interaction.response.send_message("That wallet isn't in your tracked list.", ephemeral=True)
        return

    held = db.get_holdings_for_wallet(match["id"])
    if not held:
        await interaction.response.send_message("No holdings recorded yet for this wallet.", ephemeral=True)
        return

    lines = [
        f"• {h['token_name'] or h['token_id']} ({h['collection_name'] or 'Unknown'}) — "
        f"bought at {h['purchase_price']} {h['purchase_currency'] or ''}"
        for h in held
    ]
    await interaction.response.send_message("Current holdings:\n" + "\n".join(lines), ephemeral=True)


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
                if held:
                    event["purchase_price"] = held["purchase_price"]
                await send_alert_dm(wallet["user_id"], event, "sell")


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
