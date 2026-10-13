"""Kite trading web interface — Flask backend.

The API secret and access token never leave this process; the browser talks
only to /api/* on localhost, which proxies to Kite Connect.
"""
import csv
import io
import json
import os
import threading
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, redirect, request, send_from_directory

from kite import KiteClient, KiteError, MockKite
from net import force_ipv4
from protect import ProtectionManager
from stream import MockTickHub, TickHub

load_dotenv()
if os.getenv("KITE_FORCE_IPV4", "1") == "1":
    force_ipv4()  # orders must come from the whitelisted IPv4 address

BASE = Path(__file__).parent
DATA_DIR = BASE / "data"
DATA_DIR.mkdir(exist_ok=True)
SESSION_FILE = DATA_DIR / "session.json"

MOCK = os.getenv("KITE_MOCK", "0") == "1"
API_KEY = os.getenv("KITE_API_KEY", "")
API_SECRET = os.getenv("KITE_API_SECRET", "")
INSTRUMENT_EXCHANGES = [e.strip() for e in
                        os.getenv("KITE_EXCHANGES", "NSE,BSE,NFO,BFO,MCX").split(",") if e.strip()]
# The option chain needs the F&O segments for NIFTY/BANKNIFTY (NFO) and SENSEX (BFO).
INSTRUMENT_EXCHANGES += [e for e in ("NFO", "BFO") if e not in INSTRUMENT_EXCHANGES]

# Contract lot sizes to enforce for index derivatives, e.g. "NIFTY:65,SENSEX:20".
LOT_SIZES = {k.strip().upper(): int(v) for k, v in
             (pair.split(":") for pair in os.getenv("KITE_LOT_SIZES", "NIFTY:65,SENSEX:20").split(",") if ":" in pair)}
DERIV_SEGMENTS = {"NFO-OPT", "NFO-FUT", "BFO-OPT", "BFO-FUT"}
LOT_IN_UNITS_EXCHANGES = {"NFO", "BFO"}  # MCX/CDS quantities are already in lots

# Underlyings offered in the option chain: spot quote key and options exchange.
UNDERLYINGS = {
    "NIFTY": {"spot": "NSE:NIFTY 50", "exchange": "NFO", "step_hint": 50},
    "BANKNIFTY": {"spot": "NSE:NIFTY BANK", "exchange": "NFO", "step_hint": 100},
    "SENSEX": {"spot": "BSE:SENSEX", "exchange": "BFO", "step_hint": 100},
}

if not MOCK and not (API_KEY and API_SECRET):
    raise SystemExit("Set KITE_API_KEY and KITE_API_SECRET in .env (or KITE_MOCK=1 for demo mode).")

app = Flask(__name__, static_folder="static", static_url_path="/static")
kite = MockKite() if MOCK else KiteClient(API_KEY, API_SECRET)


# ---------------------------------------------------------------- session --
def load_session():
    if MOCK or not SESSION_FILE.exists():
        return
    try:
        saved = json.loads(SESSION_FILE.read_text())
        # Kite access tokens expire at ~6 AM the next day; drop older ones.
        if saved.get("date") == date.today().isoformat() or datetime.now().hour < 6:
            kite.access_token = saved.get("access_token")
    except (ValueError, OSError):
        pass


def save_session(token):
    if MOCK:
        return
    if token:
        SESSION_FILE.write_text(json.dumps({"access_token": token, "date": date.today().isoformat()}))
    elif SESSION_FILE.exists():
        SESSION_FILE.unlink()


load_session()


# ------------------------------------------------------------ instruments --
class InstrumentStore:
    """Daily cache of the Kite instrument master, searchable in memory."""

    def __init__(self):
        self.items = []
        self.by_key = {}
        self.options = {}  # underlying -> expiry -> strike -> {"CE": inst, "PE": inst}
        self.by_token = {}
        self.loaded_for = None
        self.lock = threading.Lock()

    def ensure_loaded(self):
        today = date.today().isoformat()
        if self.loaded_for == today:
            return
        with self.lock:
            if self.loaded_for == today:
                return
            items = []
            options = {}
            lot_warnings = set()
            for exch in INSTRUMENT_EXCHANGES:
                cache = DATA_DIR / f"instruments-{exch}-{today}.csv"
                if MOCK:
                    text = kite.instruments_csv(exch)
                elif cache.exists():
                    text = cache.read_text(encoding="utf-8")
                else:
                    text = kite.instruments_csv(exch)
                    for old in DATA_DIR.glob(f"instruments-{exch}-*.csv"):
                        old.unlink()
                    cache.write_text(text, encoding="utf-8")
                for row in csv.DictReader(io.StringIO(text)):
                    if MOCK and row["exchange"] != exch:
                        continue
                    inst = {
                        "instrument_token": int(row["instrument_token"]),
                        "tradingsymbol": row["tradingsymbol"],
                        "name": row["name"],
                        "exchange": row["exchange"],
                        "segment": row["segment"],
                        "instrument_type": row["instrument_type"],
                        "expiry": row["expiry"],
                        "strike": float(row["strike"] or 0),
                        "lot_size": int(float(row["lot_size"] or 1)),
                        "tick_size": float(row["tick_size"] or 0.05),
                    }
                    name = (inst["name"] or "").upper()
                    if inst["segment"] in DERIV_SEGMENTS and name in LOT_SIZES:
                        if inst["lot_size"] != LOT_SIZES[name] and name not in lot_warnings:
                            lot_warnings.add(name)
                            print(f"  ! {name}: instrument master lot size {inst['lot_size']} "
                                  f"overridden by KITE_LOT_SIZES={LOT_SIZES[name]}")
                        inst["lot_size"] = LOT_SIZES[name]
                    if inst["segment"] in ("NFO-OPT", "BFO-OPT") and name in UNDERLYINGS \
                            and inst["expiry"] >= today:
                        (options.setdefault(name, {}).setdefault(inst["expiry"], {})
                         .setdefault(inst["strike"], {})[inst["instrument_type"]]) = inst
                    items.append(inst)
            self.items = items
            self.by_key = {f'{i["exchange"]}:{i["tradingsymbol"]}': i for i in items}
            self.options = options
            self.by_token = {i["instrument_token"]: f'{i["exchange"]}:{i["tradingsymbol"]}' for i in items}
            self.loaded_for = today

    def search(self, query, limit=25):
        self.ensure_loaded()
        q = query.upper().strip()
        if not q:
            return []
        terms = q.split()
        scored = []
        for inst in self.items:
            sym, name = inst["tradingsymbol"], (inst["name"] or "").upper()
            hay = f"{sym} {name}"
            if not all(t in hay for t in terms):
                continue
            score = 0
            if sym == q:
                score += 100
            elif sym.startswith(terms[0]):
                score += 50
            elif name.startswith(terms[0]):
                score += 30
            if inst["segment"] in ("NSE", "INDICES"):
                score += 15
            elif inst["segment"] == "BSE":
                score += 10
            score -= len(sym) * 0.1
            scored.append((score, inst))
            if len(scored) > 2000:
                break
        scored.sort(key=lambda s: -s[0])
        return [i for _, i in scored[:limit]]

    def get(self, key):
        self.ensure_loaded()
        return self.by_key.get(key)

    def chain(self, underlying, expiry=None):
        self.ensure_loaded()
        expiries = sorted(self.options.get(underlying, {}))
        if not expiries:
            return {"expiries": [], "expiry": None, "strikes": []}
        expiry = expiry if expiry in expiries else expiries[0]
        rows = self.options[underlying][expiry]
        return {"expiries": expiries, "expiry": expiry,
                "strikes": [{"strike": k, "CE": rows[k].get("CE"), "PE": rows[k].get("PE")} for k in sorted(rows)]}


def check_lot_multiple(exchange, symbol, quantity):
    """Reject F&O quantities that aren't a whole number of lots."""
    inst = instruments.get(f"{exchange}:{symbol}")
    if not inst or inst["exchange"] not in LOT_IN_UNITS_EXCHANGES or inst["lot_size"] <= 1:
        return
    lot, qty = inst["lot_size"], int(quantity)
    if qty <= 0 or qty % lot:
        lower = qty // lot * lot
        hint = f" Try {lower} or {lower + lot}." if lower else f" Minimum is {lot}."
        raise KiteError(f"{symbol}: quantity {qty} is not a multiple of the lot size {lot}.{hint}",
                        "InputException", 400)


instruments = InstrumentStore()
protections = ProtectionManager(kite, instruments, None if MOCK else DATA_DIR / "protections.json")
protections.start()


def _sync_protections(_order=None):
    try:
        protections.sync()  # react to fills immediately instead of on the next 3 s sweep
    except KiteError as exc:
        print("protection sync:", exc.message)


def _key_to_token(key):
    inst = instruments.get(key)
    return inst["instrument_token"] if inst else None


hub = (MockTickHub(kite, on_order=_sync_protections) if MOCK else
       TickHub(kite, API_KEY, _key_to_token, lambda t: instruments.by_token.get(t), on_order=_sync_protections))
hub.start()


# ---------------------------------------------------------------- helpers --
@app.errorhandler(KiteError)
def handle_kite_error(err):
    if err.error_type == "TokenException":
        kite.access_token = None
        save_session(None)
    status = err.status if 400 <= err.status < 600 else 500
    return jsonify(error=err.message, error_type=err.error_type), status


def require_login(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not kite.access_token:
            return jsonify(error="Not logged in", error_type="TokenException"), 403
        # Simple CSRF guard: browsers can't send custom headers cross-site without CORS.
        if request.method != "GET" and request.headers.get("X-Requested-With") != "kite-ui":
            return jsonify(error="Missing X-Requested-With header", error_type="InputException"), 400
        return fn(*args, **kwargs)
    return wrapper


ORDER_FIELDS = ("exchange", "tradingsymbol", "transaction_type", "order_type", "quantity",
                "product", "price", "trigger_price", "validity", "validity_ttl",
                "disclosed_quantity", "market_protection", "tag")
VARIETIES = {"regular", "amo", "co", "iceberg", "auction"}


def clean_order_params(body, fields=ORDER_FIELDS):
    params = {k: body[k] for k in fields if body.get(k) not in (None, "")}
    if params.get("order_type") in ("MARKET", "SL-M"):
        params.pop("price", None)
        params.setdefault("market_protection", -1)  # auto protection, required by Kite for market orders
    if params.get("order_type") in ("MARKET", "LIMIT"):
        params.pop("trigger_price", None)
    return params


# ----------------------------------------------------------------- routes --
@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/login")
def login():
    return redirect(kite.login_url())


@app.get("/callback")
def callback():
    if request.args.get("status") != "success" or not request.args.get("request_token"):
        return redirect("/?login_error=1")
    try:
        data = kite.generate_session(request.args["request_token"])
    except KiteError as err:
        return redirect(f"/?login_error={err.message}")
    save_session(data["access_token"])
    return redirect("/")


@app.post("/api/logout")
@require_login
def logout():
    try:
        kite.invalidate_session()
    finally:
        kite.access_token = None
        save_session(None)
    return jsonify(ok=True)


@app.get("/api/session")
def session_info():
    return jsonify(logged_in=bool(kite.access_token), mock=MOCK)


@app.get("/api/profile")
@require_login
def profile():
    return jsonify(kite.profile())


@app.get("/api/margins")
@require_login
def margins():
    return jsonify(kite.margins())


@app.get("/api/holdings")
@require_login
def holdings():
    return jsonify(kite.holdings())


@app.get("/api/positions")
@require_login
def positions():
    return jsonify(kite.positions())


@app.get("/api/orders")
@require_login
def orders():
    return jsonify(kite.orders())


@app.get("/api/orders/<order_id>")
@require_login
def order_history(order_id):
    return jsonify(kite.order_history(order_id))


@app.get("/api/trades")
@require_login
def trades():
    return jsonify(kite.trades())


@app.post("/api/orders/<variety>")
@require_login
def place_order(variety):
    if variety not in VARIETIES:
        return jsonify(error="Invalid variety", error_type="InputException"), 400
    params = clean_order_params(request.get_json(force=True))
    check_lot_multiple(params.get("exchange"), params.get("tradingsymbol"), params.get("quantity", 0))
    return jsonify(kite.place_order(variety, params))


@app.put("/api/orders/<variety>/<order_id>")
@require_login
def modify_order(variety, order_id):
    body = request.get_json(force=True)
    params = clean_order_params(body, ("order_type", "quantity", "price", "trigger_price",
                                       "validity", "disclosed_quantity"))
    if "quantity" in params:
        check_lot_multiple(body.get("exchange"), body.get("tradingsymbol"), params["quantity"])
    return jsonify(kite.modify_order(variety, order_id, params))


@app.delete("/api/orders/<variety>/<order_id>")
@require_login
def cancel_order(variety, order_id):
    return jsonify(kite.cancel_order(variety, order_id))


@app.post("/api/margins/orders")
@require_login
def order_margins():
    body = request.get_json(force=True)
    orders = [{**clean_order_params(o), "variety": o.get("variety", "regular")} for o in body]
    for o in orders:
        o.pop("market_protection", None)
        o["quantity"] = int(o.get("quantity", 0))
        o["price"] = float(o.get("price", 0))
        o["trigger_price"] = float(o.get("trigger_price", 0))
    return jsonify(kite.order_margins(orders))


@app.get("/api/quote")
@require_login
def quote():
    keys = [k for k in request.args.getlist("i") if k][:500]
    return jsonify(kite.quote(keys) if keys else {})


@app.get("/api/stream")
@require_login
def stream():
    cid = request.args.get("cid", "")[:40]
    if not cid:
        return jsonify(error="cid required", error_type="InputException"), 400
    return Response(hub.stream(cid), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/stream/subscribe")
@require_login
def stream_subscribe():
    b = request.get_json(force=True)
    keys = [k for k in b.get("keys", []) if isinstance(k, str)][:2900]
    depth = [k for k in b.get("depth", []) if isinstance(k, str)][:50]
    if not hub.set_keys(b.get("cid", ""), keys, depth):
        return jsonify(error="Unknown stream; reconnect", error_type="InputException"), 404
    return jsonify(ok=True, status=hub.status)


@app.get("/api/instruments/search")
@require_login
def search_instruments():
    return jsonify(instruments.search(request.args.get("q", "")))


@app.get("/api/instruments/lookup")
@require_login
def lookup_instruments():
    keys = request.args.getlist("i")[:200]
    return jsonify({k: instruments.get(k) for k in keys if instruments.get(k)})


def _price(value):
    if value in (None, ""):
        return None
    value = float(value)
    if value <= 0:
        raise KiteError("Prices must be positive", "InputException", 400)
    return value


# ---------------------------------------------------------------- drawings --
DRAWINGS_FILE = DATA_DIR / "drawings.json"
_drawings_lock = threading.Lock()


def _load_drawings():
    try:
        return json.loads(DRAWINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


@app.get("/api/drawings")
@require_login
def get_drawings():
    key = request.args.get("key", "")
    with _drawings_lock:
        return jsonify(items=_load_drawings().get(key, []))


@app.put("/api/drawings")
@require_login
def put_drawings():
    b = request.get_json(force=True)
    key, items = b.get("key", ""), b.get("items", [])
    if not key or not isinstance(items, list) or len(items) > 500 or len(json.dumps(items)) > 200_000:
        return jsonify(error="Invalid drawings payload", error_type="InputException"), 400
    with _drawings_lock:
        data = _load_drawings()
        if items:
            data[key] = items
        else:
            data.pop(key, None)
        tmp = DRAWINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(DRAWINGS_FILE)  # atomic: a crash never leaves a half-written file
    return jsonify(ok=True)


@app.get("/api/protections")
@require_login
def list_protections():
    since = request.args.get("since", default=-1, type=int)
    return jsonify(items=protections.list(), events=protections.events_since(since),
                   last_event=protections.last_event)


@app.post("/api/protections")
@require_login
def set_protection():
    b = request.get_json(force=True)
    if "tps" in b or "sls" in b:  # scale-out legs: [{id, price, qty}], qty in order units
        legs = {k: [{"id": leg.get("id"), "price": _price(leg.get("price")), "qty": leg.get("qty")}
                    for leg in (b.get(k) or []) if isinstance(leg, dict)][:50] for k in ("tps", "sls")}
        tps, sls = legs["tps"], legs["sls"]
    else:  # single TP / SL for the whole position
        tps = [{"price": _price(b.get("tp"))}] if b.get("tp") not in (None, "") else []
        sls = [{"price": _price(b.get("sl"))}] if b.get("sl") not in (None, "") else []
    rec = protections.set(b["exchange"], b["tradingsymbol"], b["product"], tps, sls)
    return jsonify(rec)


@app.post("/api/protections/remove")
@require_login
def remove_protection():
    b = request.get_json(force=True)
    protections.remove(b["exchange"], b["tradingsymbol"], b["product"])
    return jsonify(ok=True)


@app.get("/api/options/chain")
@require_login
def option_chain():
    name = request.args.get("underlying", "NIFTY").upper()
    if name not in UNDERLYINGS:
        return jsonify(error="Unknown underlying", error_type="InputException"), 400
    data = instruments.chain(name, request.args.get("expiry"))
    any_inst = next((s["CE"] or s["PE"] for s in data["strikes"]), None)
    return jsonify(underlying=name, spot=UNDERLYINGS[name]["spot"], exchange=UNDERLYINGS[name]["exchange"],
                   lot_size=any_inst["lot_size"] if any_inst else LOT_SIZES.get(name),
                   underlyings=list(UNDERLYINGS), **data)


INTERVAL_DAYS = {"minute": 7, "3minute": 20, "5minute": 30, "10minute": 60, "15minute": 90,
                 "30minute": 120, "60minute": 180, "day": 1500}


@app.get("/api/historical/<int:token>/<interval>")
@require_login
def historical(token, interval):
    if interval not in INTERVAL_DAYS:
        return jsonify(error="Invalid interval", error_type="InputException"), 400
    to_dt = datetime.now()
    from_dt = to_dt - timedelta(days=INTERVAL_DAYS[interval])
    fmt = "%Y-%m-%d %H:%M:%S"
    return jsonify(kite.historical(token, interval, from_dt.strftime(fmt), to_dt.strftime(fmt)))


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    host = os.getenv("HOST", "127.0.0.1")  # keep private; reach it through an SSH tunnel
    print(f"\n  Kite Terminal running at http://127.0.0.1:{port}  ({'DEMO / mock' if MOCK else 'LIVE'} mode)\n")
    app.run(host=host, port=port, debug=False, threaded=True)
