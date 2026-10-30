# Telegram signal → paper trading bot

You post an option-buy call in the **signal group**. The bot takes a **2-lot paper trade on the nearest expiry** at the live Kite quote. You tag the call to manage it. No order is ever sent to the exchange.

The bot never posts in the signal group. Every message from it (bought, waiting, SL moved, booked, exited, refused, errors) goes to the **update group**:

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
Tgt 120 140 160++      Tgt 110 130 150++      Tgt 110 130 150++
```

`Sl` and `Tgt` lines are optional.

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

`Buy Above` and `Buy Near` need a price. `Sl` and `Tgt` can go on the same reply. After it is taken, tag that same message to manage it (Exit, SL, targets, Buy again). A second buy on it is refused while that trade is still open or waiting.

| Entry | Buys when |
|---|---|
| `Buy Above 100` | the premium is at or above 100 **and** no more than 2.5% above it (≤ 102.50). If it has already run further, the bot waits for it to come back into 100–102.50 |
| `Buy Near 90` | the price is anything up to 2.5% above 90 (≤ 92.25). Any lower price is fine |
| `Buy CMP` | straight away, at the current price |

- **Lots:** every trade is 2 lots (`TRADE["lots"]` in `config.py`).
- **Waiting calls:** a call still waiting is dropped at that day's square-off.
- **No SL in the call:** the bot sets one 30% below the fill until you send yours.
- **SL already broken:** if the price is already at or below your SL when it would buy, the call is refused (or dropped, if it was waiting).

Works the same for SENSEX, CRUDEOIL, NATURALGAS, GOLD(M), SILVER(M), BANKNIFTY and NFO stocks (`Bajaj auto 10000 PE`). The name goes right before the strike.

## Follow-ups (tag the call, or the bot's message about it)

| Tag with | Does |
|---|---|
| `SL 70` | SL to 70 |
| `SL to cost` · `SL at cost` · `SL at CTC` · `SL CTC` | SL to your entry price |
| `SL to t1` · `SL to t2` · `SL to t3` | SL to that target |
| `No sl` · `Hero zero` · `Hero to Zero` | no SL |
| `Exit` · `Book` · `Book full` · `Book fully` · `Ignore` · `Avoid` · `Cancel` · `Close` · `Sell` · `SL hit` | sells **both lots** now, in profit or loss. A call still waiting is dropped |
| `Book half` · `Book 50` · `Book 50%` | sells **1 of the 2 lots** now. On the last lot it sells that one |
| `Tgt 110` · `Tgt 110 120 130` · `Target 110` · `Target 110 120 130` | replaces the targets |
| `Buy again` · `Buy again Above 100` · `Buy again Near 100` · `Buy again CMP` | posts the same call again — same strike, same SL and targets, 2 new lots. Plain `Buy again` keeps the original entry; the other three use that entry instead. Only once the previous trade on it is closed (or dropped); after that, tags on the original call act on the new trade |

An SL must be below the live premium, otherwise it is refused and the old SL stays.

`Exit all` as its own message closes every open trade and drops every waiting call.

## Commands (update group only)

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

Each copy writes an order intent before calling the broker. The intent key is unique, and a Zerodha order is tagged with it. If the process dies after the send, recovery looks the order up by that tag instead of sending it again. Buys are limit + IOC at the account's slippage cap. Exits are market orders. A kill switch stops new buys immediately; exits still go out.

| Control | Where |
|---|---|
| Lots, min, max, slippage | per symbol, in the portal. `*` is the default |
| Max loss per trade | cuts the lot count. Below the minimum, the buy is skipped |
| Max loss per day | same, and the auto kill switch can stop the account |
| Kill switch / flatten | portal buttons. Flatten sells whatever that account still holds |
| Daily Zerodha token | each user, from the portal, every morning |
| Live orders | account mode **and** `LIVE_TRADING=1` |

Dhan, Kotak and Groww can be saved in the portal. Only Zerodha places orders today; the others use the same adapter slot.

On the droplet, `deploy/telegram-bot.service` and `deploy/portal.service` run the two processes as the `bot` user. Leave `PORTAL_HOST=127.0.0.1` and open it with `ssh -L 8080:127.0.0.1:8080 bot@YOUR_DROPLET`. The first registered user is the admin. Set `PORTAL_INVITE_CODE` before anyone else can register.

`python3 main.py --parse "your message"` shows how a message is read (add `--reply` for a follow-up, and `--tagged "Sensex 72800 PE"` for a buy on a watchlist line).

## Files

| Path | What |
|---|---|
| `config.py` | instruments, sessions, lots, bands, default SL, costs |
| `signal_parser.py` | message → buy call / follow-up / exit all |
| `paper_trader.py` | entries, SL, targets, booking, square-off, journal |
| `bot.py` | Telegram loop and message routing |
| `state/trades.csv` | every closed trade |
| `tests/` | `python3 -m unittest discover tests` |
