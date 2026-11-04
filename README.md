# Telegram signal → paper trading bot

You post an option-buy call in the **signal group**. The bot takes a **1-lot paper trade on the nearest expiry** at the live Kite quote. You tag the call to manage it. No order is ever sent to the exchange.

The bot never posts in the signal group when that group is separate. Every message from it (bought, waiting, SL moved, booked, exited, refused, errors) goes to the **update group**. Each of those is one short line, so a shared group stays easy to scan and tag.

```
TELEGRAM_SIGNAL_CHAT_ID=   # calls and follow-ups are read here
TELEGRAM_UPDATE_CHAT_ID=   # the bot posts here
```

Only the messages below are acted on. **Everything else is ignored.**

## Buy call (its own message)

```
NIFTY 22800PE          NIFTY 22800PE          NIFTY 22800PE
Buy Above 100          Buy Near 90            Buy CMP
Sl 80                  Sl 70                  Sl 70
```

`Sl` is optional. A target line is ignored.

## Tag a watchlist line to buy it

The option can sit on its own message. Reply on that message with the entry:

```
Sensex 72800 PE | ADD TO WATCHLIST
```

```
Buy CMP
Buy Above 120
Buy Near 110
```

`Buy Above` and `Buy Near` need a price. `Sl` can go on the same reply. After it is taken, tag that same message to manage it (Exit, SL, Buy again). A second buy is refused while that trade is still open. While it is waiting, `Buy Near`, `Buy only`, `Buy Above` or `Buy CMP` replaces the entry.

| Entry | Buys when |
|---|---|
| `Buy Above 100` | the premium is at or above 100 **and** no more than 2.5% above it (≤ 102.50). If it has already run further, the bot waits for it to come back into 100–102.50 |
| `Buy Near 90` | the price is anything up to 2.5% above 90 (≤ 92.25). Any lower price is fine |
| `Buy only 100` | only when the ask is exactly 100 |
| `Buy CMP` | straight away, at the current price |

- **Lots:** the signal book takes 1 lot (`TRADE["lots"]` in `config.py`). "Book half" closes that lot. Each linked account is sized from its own cash on the portal Config page.
- **Waiting calls:** a call still waiting is dropped at that day's square-off.
- **No SL in the call:** the bot sets one 30% below the fill until you send yours.
- **SL already broken:** if the price is already at or below your SL when it would buy, the call is refused (or dropped, if it was waiting).

Works the same for SENSEX, CRUDEOIL, NATURALGAS, GOLD(M), SILVER(M), BANKNIFTY and NFO stocks (`Bajaj auto 10000 PE`). The name goes right before the strike.

## Follow-ups (tag the call, or the bot's message about it)

| Tag with | Does |
|---|---|
| `Buy Near 100` · `Buy only 100` · `Buy Above 100` · `Buy CMP`, while the call is waiting | replaces the entry. Buys now if the live price already fits; otherwise it keeps waiting on the new rule. An SL on that same tag replaces the waiting SL |
| `SL 70` | SL to 70. `SL 0` removes the stop |
| `Exit` · `Book` · `Book full` · `Book fully` · `Cancel` | sells the open lot now, in profit or loss. A call still waiting is dropped |
| `Book half` · `Book 50` | sells half the open lots. On a 1-lot book it sells that lot |
| `Buy again` · `Buy again Above 100` · `Buy again Near 100` · `Buy again CMP` | posts the same call again — same strike, same SL, 1 new lot. Plain `Buy again` keeps the original entry; the other three use that entry instead. Only once the previous trade on it is closed (or dropped); after that, tags on the original call act on the new trade |

An SL must be below the live premium, otherwise it is refused and the old SL stays.

`Exit all` as its own message closes every open trade and drops every waiting call.

## Commands

Send these in the update group. When that group is also the signal group, they work in that same chat.

| Send | Gets |
|---|---|
| `/op` | open positions and waiting calls, with entry, LTP, P&L, SL and targets |
| `/pnl` | today's P&L: each trade, booked, open and total (after costs) |
| `/trades` | `trades.csv` as a file, with every closed trade |

## What the bot does on its own

- **SL:** sells every open lot at the bid when the LTP trades at or below the SL.
- **Targets:** posts an alert when one is reached. It does not move the SL or book profit — you tag the call for that.
- **Square-off:** NSE/BSE at 15:20, MCX 10 minutes before close.
- **Restarts:** trades are kept in `state/positions.json`, so tags still work after a restart.

## Setup

Add the bot to both groups. In the signal group it must read normal messages (privacy mode off in @BotFather, or make it a group admin).

```bash
pip3 install -r requirements.txt     # first time only
python3 login_zerodha.py             # market-data token, every morning
python3 main.py --check              # Kite, contracts, Telegram access
python3 main.py                      # signal book
python3 -m portal                    # accounts, daily login, risk limits
```

The `.env` Zerodha key is only the quote login. Orders go to accounts linked in the portal.

## Copying into broker accounts

A signal is decided once, from the live quote. That decision is then copied, in parallel, to every enabled account. One account's rejection or dead token does not stop the others.

Each copy writes an order intent before calling the broker. The intent key is unique, and a Zerodha order is tagged with it. If the process dies after the send, recovery looks the order up by that tag instead of sending it again. Buys are limit + IOC at the admin slippage cap. Exits are market orders. A kill switch stops new buys immediately; exits still go out.

Lot size is the account's cash divided by the rupees configured for that symbol, rounded down. NIFTY, SENSEX and BANKNIFTY start at ₹50,000 for one lot. Every other symbol starts at ₹100,000. An open position does not reduce the cash used for the next trade; the check only chooses the lot size. The broker can still reject an order if the exchange margin is not there.

| Control | Where |
|---|---|
| Rupees per lot, slippage | admin Config page, shared by every account. `*` is every symbol without its own row. A short name such as `CRUDE` is the same row as `CRUDEOIL`. Slippage is percent or points, added to the copy buy limit. `Buy only` uses the price with no slippage |
| Paper balance | on the account, used only while that account is on paper. Live reads the broker's cash |
| Max loss per trade | cuts the lot count. Below 1 lot, the buy is skipped |
| Max loss per day | same, and the auto kill switch can stop the account |
| Kill switch / flatten | portal buttons. Flatten sells whatever that account still holds |
| Quote login | admin, portal page **Quote login**, every morning. Renews the `.env` price token. The running bot reads it without a restart |
| Daily login | each trading account, from the portal, every morning. Zerodha pastes the redirect URL. Kotak Neo enters the authenticator code |
| Live orders | account mode **and** `LIVE_TRADING=1` |

Kotak Neo uses the Trade API token as the API key, the MPIN as the secret, the UCC as the client id, and the registered mobile number. A Zerodha contract name is translated to Kotak's name before the order is sent. Dhan and Groww can be saved; they do not place orders yet.

On the droplet, `deploy/telegram-bot.service` and `deploy/portal.service` run the two processes. To stop the signal bot outside market hours, disable that service and enable `deploy/market-hours.timer` instead. It starts the bot at 08:00 IST on trading days and stops it at 00:10, after the MCX close. Weekends and dates in `HOLIDAYS` stay stopped. The portal keeps running so the morning quote login still works. Leave `PORTAL_HOST=127.0.0.1` and open it with `ssh -L 8080:127.0.0.1:8080 bot@YOUR_DROPLET`. The first registered user is the admin. Set `PORTAL_INVITE_CODE` before anyone else can register.

`python3 main.py --parse "your message"` shows how a message is read (add `--reply` for a follow-up, and `--tagged "Sensex 72800 PE"` for a buy on a watchlist line).

## Files

| Path | What |
|---|---|
| `config.py` | instruments, sessions, signal-book lots, bands, default SL, costs |
| `signal_parser.py` | message → buy call / follow-up / exit all |
| `paper_trader.py` | entries, SL, targets, booking, square-off, journal |
| `bot.py` | Telegram loop and message routing |
| `state/trades.csv` | every closed trade |
| `tests/` | `python3 -m unittest discover tests` |
