# NFT Wallet Tracker Bot

Tracks NFT buy/sell activity on wallets you register, and DMs you an alert
when something happens.

## Current status

✅ Discord bot — slash commands, DM alerts, SQLite storage with persistent volume support
✅ Ethereum tracking (via Alchemy `getNFTSales`)
✅ Robinhood Chain tracking (via Alchemy Transfers API — `getNFTSales` isn't available on this chain yet, so this uses raw transfer detection + best-effort price matching instead)
✅ Solana tracking (via Helius Enhanced Transactions API)
✅ Live on-chain holdings lookup (`/holdings`)
✅ Floor price lookup (`/floorprice`)
✅ Portfolio summary across wallets (`/portfolio`)
✅ Weekly + on-demand buy/sell digest (`/summary`, auto weekly DM)
✅ Gas cost shown on EVM buy/sell alerts (best-effort)
✅ Wallet-count rate limit per user (default 10, via `MAX_WALLETS_PER_USER`)
✅ Basic HTTP healthcheck endpoint for uptime monitoring (`/ping`) — see note below
✅ `/help` command

### Known limitations
- **Robinhood Chain price detection is best-effort.** Alchemy doesn't have
  dedicated marketplace-sale parsing for this chain yet (it's brand new),
  so we detect the NFT transfer reliably but the price may show "Unknown"
  if the trade routed through an escrow/marketplace contract we can't trace
  with a simple paired-payment lookup.
- **The healthcheck server may not be reachable externally on Railway**
  unless the service has a public domain generated and/or the Procfile
  process type is `web` rather than `worker`. Check Railway's networking
  settings if an external uptime monitor (e.g. UptimeRobot) can't reach it —
  the server itself is running either way, just may only be reachable
  internally.
- `/portfolio` shows NFT counts per wallet but does not yet total floor
  value across collections — use `/floorprice` per collection for now.

### Getting an Alchemy API key (free tier)
1. Go to https://dashboard.alchemy.com/signup and sign up
2. Create an app — for Ethereum, pick network "Ethereum Mainnet"; for
   Robinhood Chain, pick "Robinhood Chain Mainnet" (same API key generally
   works across networks, actual per-network access may depend on your plan)
3. Paste it into `.env` as `ALCHEMY_API_KEY=your_key_here`

### Getting a Helius API key (free tier)
1. Go to https://dashboard.helius.dev and sign up
2. Create an API key — free tier includes 1M credits/month, no card required
3. Paste it into `.env` as `HELIUS_API_KEY=your_key_here`

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
| `/holdings <chain> <address>` | Show NFTs currently held, live from the chain |
| `/floorprice <chain> <identifier>` | Check a collection's current floor price (contract address for EVM, Magic Eden symbol for Solana) |
| `/portfolio` | NFT count summary across all your tracked wallets |
| `/summary` | Buy/sell digest for the last 7 days (also sent automatically every week) |
| `/status` | Bot health check |
| `/testalert` | Send yourself a sample DM alert |
| `/help` | List all commands |

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `DISCORD_TOKEN` | Yes | Your bot's token from the Discord Developer Portal |
| `POLL_INTERVAL_SECONDS` | No (default 180) | How often to check tracked wallets |
| `ALCHEMY_API_KEY` | For Ethereum/Robinhood Chain | From dashboard.alchemy.com |
| `HELIUS_API_KEY` | For Solana | From dashboard.helius.dev |
| `DB_PATH` | On Railway | Should point at your mounted volume, e.g. `/data/tracker.db`, so data survives redeploys |
| `MAX_WALLETS_PER_USER` | No (default 10) | Per-user wallet tracking limit |
| `PORT` | No (default 8080) | Port for the healthcheck HTTP server; Railway sets this automatically for web-exposed services |

## Project structure

```
bot.py             - Discord bot, commands, alert formatting, polling loop, digests, healthcheck server
database.py        - SQLite storage (wallets, holdings, event log, last-seen tx markers)
chain_clients.py   - Blockchain API integration (Alchemy for EVM chains, Helius for Solana)
requirements.txt   - Python dependencies
.env.example       - Environment variable template
tracker.db         - SQLite database file (created automatically; use DB_PATH to point elsewhere)
```

## Next steps

- Add floor-value totals to `/portfolio` (sum of floor prices across held collections)
- Improve Robinhood Chain price detection once Alchemy adds marketplace-sale parsing for it
- Consider webhooks instead of polling once wallet count grows beyond a handful
