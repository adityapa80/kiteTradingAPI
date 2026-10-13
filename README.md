# Kite Terminal

A modern trading web interface for Zerodha, built on the [Kite Connect v3 API](https://kite.trade/docs/connect/v3/).

- **Real-time prices**: a Kite WebSocket feed is streamed to the browser (watchlist, option chain, depth, P&L, candles). Fills arrive instantly through order postbacks. A **LIVE** badge shows the feed status
- **Watchlists**: 5 lists, tick-by-tick prices with flashes, inline market depth, instrument search across NSE/BSE/NFO/MCX
- **Order window**: Buy/Sell toggle, Regular/AMO, MIS/CNC/NRML, Market/Limit/SL/SL-M, lots for F&O, live margin requirement, click-to-fill depth, optional confirmation step
- **Orders**: pending, executed, and trades, with modify, cancel, and repeat; toasts when orders fill or get rejected
- **Positions and holdings**: live P&L, one-click exit, allocation breakdown, sortable columns
- **Charts**: candlesticks and volume (TradingView Lightweight Charts) from 1m to 1D, updating live
- **Drawing tools**: trend lines (with extend-right) and horizontal support/resistance lines, from the chart toolbar or with **Alt+T** / **Alt+H**. Click a line to select it, then drag it or its endpoints to adjust, change its colour, or press **Del** to delete it (with Undo). Points snap to nearby candle highs and lows. Drawings are saved per instrument on the server, in absolute time and price, so they show on every interval and device
- **One or two charts**: one chart by default. The layout toggle shows two charts side by side, each with its own instrument and interval. Click a chart to focus it, and the watchlist's chart button (or **C**) loads into the focused one. TP/SL lines live on the left chart, and **Swap** exchanges the two
- **Trades on the chart**: an entry line at your average price with live P&L, plus buy/sell markers on the candles where your orders filled
- **TP / SL on the chart, live**: clicking **+ TP** / **+ SL** on the entry line places the order at once, with no confirmation. The default level is 1% away for stocks and 5% for options, and a target always starts beyond your entry. Dragging a line modifies the order when you release it. ✕ cancels a leg instantly, and the notification has an **Undo** button. If a level is on the wrong side of the market, it is refused and the line snaps back
- **Multi-lot TP / SL (F&O, 2+ lots)**: give different lots different exits. Use **− / +** on a TP or SL line to change how many lots it covers. **+ TP 1L** on the entry line then adds a line for the uncovered lots, and **Split** breaks a line into as many lines as you choose. Lots are shared out evenly (7 into 3 gives 3 + 2 + 2), and prices are staggered and shown in a preview first. This works for any number of lots, for example TP 1 lot @ x and 2 lots @ y, 1 lot each at x, y and z, or 20 lots as 4 × 5. Each line can be dragged or cancelled on its own. Single-lot trades and stocks keep the single TP/SL line
- **Option chain**: NIFTY, BANK NIFTY and SENSEX, with an expiry picker and days to expiry. It shows the ATM strike, a live spot divider, ITM shading, OI bars, PCR, and one-click **B / S** for 1 lot on any strike
- **Lot-based F&O trading**: quantities are entered in lots, using a −/+ stepper and quick 1/2/3/5/10 chips. The server rejects any NFO/BFO quantity that isn't a whole number of lots (NIFTY = 65, SENSEX = 20, configurable)
- **Quick partial profit booking**: **1 lot / 25% / 50% / All** buttons appear in the F&O positions panel, the Positions table, and on the chart (**−1L**, **½**). They send market orders in whole lots. Any TP/SL is then resized to the remaining quantity
- **Exit all F&O**: one confirmed click buys back shorts first, then sells longs
- **Dashboard**: funds, P&L, market overview, and top movers
- Dark and light themes, plus full keyboard control (press `?` in the app)

## Architecture

```
browser (static/*)  ──►  Flask (app.py) on 127.0.0.1  ──►  api.kite.trade        (REST: orders, portfolio)
        ▲                       │
        └──── SSE (stream.py) ◄─┴──────────────────  wss://ws.kite.trade   (ticks + order postbacks)
```

Market data comes from a single server-side Kite WebSocket in `full` mode, which carries LTP, OHLC, OI and 5-level depth.
That one socket is shared by every open tab. Each tab tells the server which instruments are on screen, and the
server subscribes to exactly those. Ticks reach the browser as Server-Sent Events, in batches of up to about 20
per second. If the feed drops, the badge shows **POLLING** and the app falls back to REST quotes every 2 s until
the feed reconnects.

The API secret and the access token stay in the Python process and never reach the browser. The server
binds to localhost only. State-changing requests must carry an `X-Requested-With` header, which blocks
simple cross-site request forgery.

## Setup

1. Create an app at <https://developers.kite.trade> and set its **Redirect URL** to
   `http://127.0.0.1:5000/callback`.
2. Install dependencies and configure:
   ```bash
   pip install -r requirements.txt
   copy .env.example .env      # then fill in KITE_API_KEY and KITE_API_SECRET
   ```
3. Run it and open <http://127.0.0.1:5000>:
   ```bash
   python app.py
   ```
4. Click **Login with Kite**. Kite access tokens expire every morning at about 6 AM, so you'll log in once per day.
   The token is cached in `data/session.json` so the app survives restarts.

### Demo mode (no account needed)

```bash
set KITE_MOCK=1 && python app.py
```

Demo mode uses simulated prices, orders, positions and holdings. Nothing is sent to Zerodha.

## Lot sizes

`KITE_LOT_SIZES` (default `NIFTY:65,SENSEX:20`) overrides the lot size from the instrument master for those
underlyings' futures and options. On startup, the app prints a warning if Zerodha's master lists a different
lot size. If that happens, update the setting when the exchange revises the contract size. Each order quantity
is checked against the lot size, both in the browser and on the server, before it reaches Kite.

## How TP / SL is placed

| Product | Mechanism | Keeps working if the app is closed? |
| --- | --- | --- |
| CNC, NRML, MTF | Kite **GTT**: two-leg OCO if both TP and SL are set, single otherwise | Yes, it lives at Zerodha |
| MIS (intraday) | Two regular exit orders (a target LIMIT plus an SL stop) that this app links as an OCO pair | **No**, the app must stay running |

- **Multiple lots**: TP and SL lines are paired lot by lot into *tranches*, nearest target with farthest stop, and each tranche is its own OCO (one GTT, or one order pair for MIS). With TP 1L @ x, TP 2L @ y and SL 3L @ z, hitting x exits 1 lot and leaves an OCO covering exactly the remaining 2 lots at y and z. The stop never covers more than you hold. If the position shrinks some other way, the lines farthest from the market are trimmed first.
- **Limit buffers**: SL legs use a limit price 1% beyond the trigger (`SL_LIMIT_BUFFER`). GTT target legs use a limit 0.5% inside the target (`TP_LIMIT_BUFFER`). These are floors, not fixed prices, so orders still fill on spikes that reverse instantly. Both settings are in `protect.py`.
- **MIS watcher**: every 3 seconds, a background watcher (`protect.py`) checks the MIS legs. When one leg fills, it cancels the other. If you partly exit, it resizes the legs to the remaining quantity. If you close the position some other way, it cancels both legs.
- **Records**: TP/SL records are stored in `data/protections.json`, and chart drawings in `data/drawings.json`.
- **Validation**: before placing, the server checks that the TP and SL are on the correct side of the current price.

## Notes

- **Charts** use the Historical Data API, which is a paid add-on on Kite Connect.
- **Market and SL-M orders** are sent with `market_protection=-1` (automatic protection), as the current Kite API expects.
- **Instrument search** downloads the instrument master once per day for each exchange in `KITE_EXCHANGES`
  and caches it in `data/`. The first search of the day takes a few seconds.
- **Static IP**: Under SEBI's retail algo framework, Zerodha may require a registered static IP for API order placement.
  If orders are rejected for that reason, register your IP in the Kite developer console.
- **WebSocket limits**: Kite allows 3 WebSocket connections per API key and 3000 instruments per connection.
  The app uses one connection.

## Files

| File | Purpose |
| --- | --- |
| `app.py` | Flask server: OAuth login, REST proxy, instrument search, session persistence |
| `kite.py` | Lightweight Kite Connect client and the demo-mode mock |
| `static/index.html`, `style.css`, `app.js` | The UI (vanilla JS, no build step) |
