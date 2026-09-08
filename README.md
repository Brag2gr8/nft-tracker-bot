# NFT Wallet Tracker Bot

Tracks NFT buy/sell activity on wallets you register, and DMs you an alert
when something happens.

## Current status

✅ Discord bot skeleton — slash commands, DM alerts, SQLite storage
⬜ Ethereum / Robinhood Chain tracking (via Alchemy) — not yet implemented
⬜ Solana tracking (via Helius) — not yet implemented

Right now, `/setwallet` will save wallets and the bot will poll on a loop,
but `chain_clients.py` returns no events yet since the blockchain API calls
haven't been wired in. `/testalert` works right now and lets you confirm
DMs are reaching you correctly.

## Setup

1. **Create a Discord bot application**
   - Go to https://discord.com/developers/applications → New Application
   - Go to the "Bot" tab → Reset Token → copy it
   - Under "Privileged Gateway Intents" you don't need any special intents for this bot
   - Under "Installation" → enable both "Guild Install" and "User Install" if you want either option

2. **Invite the bot to your account/server**
   - In the Developer Portal, use the generated OAuth2 URL with the `applications.commands` and `bot` scopes

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment**
   ```bash
   cp .env.example .env
   # then edit .env and paste in your DISCORD_TOKEN
   ```

5. **Run the bot**
   ```bash
   python bot.py
   ```

## Commands

| Command | Description |
|---|---|
| `/setwallet <chain> <address>` | Start tracking a wallet (chain: ethereum, solana, robinhood) |
| `/removewallet <chain> <address>` | Stop tracking a wallet |
| `/mywallets` | List your tracked wallets |
| `/holdings <chain> <address>` | Show NFTs currently held per our records |
| `/status` | Bot health check |
| `/testalert` | Send yourself a sample DM alert |

## Project structure

```
bot.py             - Discord bot, commands, alert formatting, polling loop
database.py        - SQLite storage (wallets, holdings, last-seen tx markers)
chain_clients.py   - Blockchain API integration (STUB - not yet implemented)
requirements.txt   - Python dependencies
.env.example       - Environment variable template
tracker.db         - SQLite database file (created automatically on first run)
```

## Next steps

- Implement `_get_evm_events()` in `chain_clients.py` using the Alchemy NFT API
  (covers both Ethereum and Robinhood Chain)
- Implement `_get_solana_events()` using the Helius API
- Test with real wallet addresses
