"""Take-profit / stop-loss ("protection") for open trades, with scale-out exits.

A position can have several target (TP) legs and several stop (SL) legs, each
for a number of lots: e.g. TP 1 lot @ x and 2 lots @ y, with SL 3 lots @ z.
The legs are paired lot by lot into *tranches* - the nearest target with the
farthest stop - and every tranche is protected as one OCO unit:

* CNC / NRML / MTF: one Kite GTT per tranche (two-leg OCO when it has both a
  target and a stop, single otherwise). GTTs live at Zerodha, so they keep
  working when this app is closed.
* MIS: per tranche, a LIMIT target and/or an SL order; the watcher in this
  process links each pair (when one fills it cancels the other), so the app
  must keep running while the position is open.

When a tranche exits, its quantity leaves both sides together, so the remaining
stops always cover exactly the remaining lots. If the position shrinks some
other way (manual exit, partial booking) the legs farthest from the market are
trimmed first.
"""
import itertools
import json
import logging
import threading
import time
import uuid
from collections import deque

from kite import KiteError

log = logging.getLogger(__name__)

GTT_PRODUCTS = {"CNC", "NRML", "MTF"}
SL_LIMIT_BUFFER = 0.01   # SL legs use a limit 1% beyond the trigger so they fill on gaps
TP_LIMIT_BUFFER = 0.005  # GTT target legs: a limit at exactly the trigger can miss a spike that reverses
OPEN_STATUSES = {"OPEN", "TRIGGER PENDING", "AMO REQ RECEIVED", "OPEN PENDING", "VALIDATION PENDING",
                 "PUT ORDER REQ RECEIVED", "MODIFY PENDING", "MODIFY VALIDATION PENDING"}
LEG_NAMES = {"tp": "Target", "sl": "Stop-loss"}
LOT_IN_UNITS_EXCHANGES = {"NFO", "BFO"}  # quantities there are units; must be whole lots
MAX_UNITS_PER_SIDE = 10_000


def round_tick(price, tick):
    tick = tick or 0.05
    return round(round(price / tick) * tick, 4)


def new_id():
    return uuid.uuid4().hex[:8]


def legs_of(tranches, kind):
    """Collapse tranches back into the legs the user set (id, price, total qty)."""
    legs = {}
    for t in tranches:
        lid = t.get(f"{kind}_id")
        if lid and t.get(kind) is not None:
            leg = legs.setdefault(lid, {"id": lid, "price": t[kind], "qty": 0})
            leg["qty"] += t["qty"]
    return list(legs.values())


def build_tranches(tps, sls, long, lot):
    """Pair TP and SL legs lot by lot: nearest target with the farthest stop."""
    def ordered(legs):  # long: ascending = nearest TP first / farthest SL first; short: the mirror
        return sorted(legs, key=lambda leg: leg["price"], reverse=not long)

    def expand(legs):
        return [leg for leg in ordered(legs) for _ in range(leg["qty"] // lot)]

    tp_seq, sl_seq = expand(tps), expand(sls)
    out = []
    for i in range(max(len(tp_seq), len(sl_seq))):
        tp = tp_seq[i] if i < len(tp_seq) else None
        sl = sl_seq[i] if i < len(sl_seq) else None
        ids = (tp["id"] if tp else None, sl["id"] if sl else None)
        if out and (out[-1]["tp_id"], out[-1]["sl_id"]) == ids:
            out[-1]["qty"] += lot
        else:
            out.append({"tp_id": ids[0], "sl_id": ids[1], "tp": tp["price"] if tp else None,
                        "sl": sl["price"] if sl else None, "qty": lot})
    return out


def trim(legs, qty, long, kind):
    """Cut legs down to `qty` in total, removing quantity farthest from the market first."""
    excess = sum(leg["qty"] for leg in legs) - qty
    if excess <= 0:
        return legs
    out = {leg["id"]: dict(leg) for leg in legs}
    farthest_first = sorted(legs, key=lambda leg: leg["price"], reverse=(kind == "tp") == long)
    for leg in farthest_first:
        if excess <= 0:
            break
        cut = min(excess, leg["qty"])
        out[leg["id"]]["qty"] -= cut
        excess -= cut
    return [leg for leg in out.values() if leg["qty"] > 0]


class ProtectionManager:
    def __init__(self, kite, instruments, path=None):
        self.kite = kite
        self.instruments = instruments
        self.path = path
        self.items = {}
        self.events = deque(maxlen=100)
        self.event_ids = itertools.count(1)
        self.last_event = 0
        self.lock = threading.RLock()
        self._load()

    # ------------------------------------------------------------ persistence
    def _load(self):
        if self.path and self.path.exists():
            try:
                self.items = {k: self._migrate(v) for k, v in json.loads(self.path.read_text()).items()}
            except (ValueError, OSError, KeyError):
                self.items = {}

    @staticmethod
    def _migrate(rec):
        """Records written before scale-out support had one tp/sl pair for the whole position."""
        if "tranches" in rec:
            return rec
        t = {"tp_id": "tp" if rec.get("tp") is not None else None, "sl_id": "sl" if rec.get("sl") is not None else None,
             "tp": rec.get("tp"), "sl": rec.get("sl"), "qty": rec["quantity"], "gtt_id": rec.get("gtt_id"),
             "tp_order_id": rec.get("tp_order_id"), "sl_order_id": rec.get("sl_order_id")}
        new = {k: rec[k] for k in ("key", "exchange", "tradingsymbol", "product", "mode", "side", "quantity")}
        new["tranches"] = [t]
        return new

    def _save(self):
        if self.path:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.items, indent=1))
            tmp.replace(self.path)

    def _event(self, kind, title, text=""):
        self.last_event = next(self.event_ids)
        self.events.append({"id": self.last_event, "kind": kind, "title": title, "text": text})

    def events_since(self, since):
        return [e for e in self.events if e["id"] > since] if since >= 0 else []

    @staticmethod
    def view(rec):
        out = {k: v for k, v in rec.items() if k != "tranches"}
        out["tps"] = legs_of(rec["tranches"], "tp")
        out["sls"] = legs_of(rec["tranches"], "sl")
        out["tranches"] = [{k: t.get(k) for k in ("tp", "sl", "qty")} for t in rec["tranches"]]
        return out

    def list(self):
        with self.lock:
            return [self.view(r) for r in self.items.values()]

    # ---------------------------------------------------------------- lookups
    @staticmethod
    def key(exchange, symbol, product):
        return f"{exchange}:{symbol}:{product}"

    def trade_quantity(self, exchange, symbol, product, positions=None, holdings=None):
        """Signed open quantity for a trade, using the same rules as the UI."""
        positions = self.kite.positions()["net"] if positions is None else positions
        qty = sum(p["quantity"] for p in positions
                  if p["exchange"] == exchange and p["tradingsymbol"] == symbol and p["product"] == product)
        if product == "CNC" and exchange in ("NSE", "BSE"):
            holdings = self.kite.holdings() if holdings is None else holdings
            for h in holdings:
                if h["tradingsymbol"] == symbol:
                    held = h["quantity"] + h.get("t1_quantity", 0) - h.get("used_quantity", 0)
                    return held + max(qty, 0)
        return qty

    def _lot(self, exchange, symbol):
        if exchange not in LOT_IN_UNITS_EXCHANGES:
            return 1
        inst = self.instruments.get(f"{exchange}:{symbol}")
        return inst["lot_size"] if inst else 1

    def _tick(self, exchange, symbol):
        inst = self.instruments.get(f"{exchange}:{symbol}")
        return inst["tick_size"] if inst else 0.05

    def _ltp(self, exchange, symbol):
        key = f"{exchange}:{symbol}"
        data = self.kite.ltp([key])
        if key not in data:
            raise KiteError(f"No price available for {key}", "InputException", 400)
        return data[key]["last_price"]

    # ------------------------------------------------------------------- set
    @staticmethod
    def _clean_legs(legs, full_qty):
        out = {}
        for leg in legs or []:
            if not leg or leg.get("price") in (None, ""):
                continue
            price = float(leg["price"])
            qty = full_qty if leg.get("qty") in (None, "") else int(leg["qty"])
            if price <= 0 or qty <= 0:
                raise KiteError("Prices and quantities must be positive", "InputException", 400)
            if price in out:  # two legs at the same price are one leg
                out[price]["qty"] += qty
            else:
                out[price] = {"id": str(leg.get("id") or new_id())[:16], "price": price, "qty": qty}
        return list(out.values())

    def set(self, exchange, symbol, product, tps, sls):
        """Replace the TP/SL legs of a trade. Each leg: {id?, price, qty?} (qty in order units; None = all)."""
        with self.lock:
            qty = self.trade_quantity(exchange, symbol, product)
            if qty == 0:
                raise KiteError(f"No open {product} quantity in {symbol} to protect", "InputException", 400)
            long, full = qty > 0, abs(qty)
            tps, sls = self._clean_legs(tps, full), self._clean_legs(sls, full)
            if not tps and not sls:
                self.remove(exchange, symbol, product)
                return None
            lot = self._lot(exchange, symbol)
            ltp = self._ltp(exchange, symbol)
            side = "long" if long else "short"
            for leg in tps:
                if (long and leg["price"] <= ltp) or (not long and leg["price"] >= ltp):
                    raise KiteError(f"Target {leg['price']} must be {'above' if long else 'below'} the current price ({ltp}) "
                                    f"for a {side} trade", "InputException", 400)
            for leg in sls:
                if (long and leg["price"] >= ltp) or (not long and leg["price"] <= ltp):
                    raise KiteError(f"Stop-loss {leg['price']} must be {'below' if long else 'above'} the current price ({ltp}) "
                                    f"for a {side} trade", "InputException", 400)
            for kind, legs in (("tp", tps), ("sl", sls)):
                total = sum(leg["qty"] for leg in legs)
                if total > full:
                    raise KiteError(f"{LEG_NAMES[kind]} quantity ({total}) is more than the open position ({full})",
                                    "InputException", 400)
                if total > MAX_UNITS_PER_SIDE:
                    raise KiteError("Too many units in one TP/SL set", "InputException", 400)
                for leg in legs:
                    if leg["qty"] % lot:
                        raise KiteError(f"{LEG_NAMES[kind]} quantity {leg['qty']} is not a whole number of lots ({lot})",
                                        "InputException", 400)

            key = self.key(exchange, symbol, product)
            old = self.items.get(key)
            rec = {"key": key, "exchange": exchange, "tradingsymbol": symbol, "product": product,
                   "mode": "gtt" if product in GTT_PRODUCTS else "oco", "side": "LONG" if long else "SHORT",
                   "quantity": full, "updated_at": int(time.time()), "tranches": []}
            try:
                self._reconcile(old["tranches"] if old else [], build_tranches(tps, sls, long, lot), rec, ltp)
            finally:  # record whatever is actually live at Kite, even after a partial failure
                if rec["tranches"]:
                    self.items[key] = rec
                else:
                    self.items.pop(key, None)
                self._save()
            return self.view(rec)

    def _reconcile(self, old, desired, rec, ltp):
        """Turn the live tranches `old` into `desired` with as few broker calls as possible."""
        live = rec["tranches"]
        pool = list(old)
        todo = []
        for d in desired:  # unchanged tranches are kept as they are
            same = next((o for o in pool if o.get("tp") == d["tp"] and o.get("sl") == d["sl"] and o["qty"] == d["qty"]), None)
            if same:
                pool.remove(same)
                live.append({**same, "tp_id": d["tp_id"], "sl_id": d["sl_id"]})
            else:
                todo.append(d)
        orders = {o["order_id"]: o for o in self.kite.orders()} if rec["mode"] == "oco" and pool else {}
        try:
            for d in todo:  # reuse an existing GTT / order pair where possible (modify), else create
                o = pool.pop(0) if pool else None
                try:
                    live.append(self._apply_tranche(o, d, rec, ltp, orders))
                except Exception:
                    if o:
                        pool.insert(0, o)
                    raise
            while pool:
                self._drop_tranche(pool[0], rec, orders)
                pool.pop(0)
        except Exception:
            live.extend(pool)  # not dropped yet: still live at the broker
            raise

    def _gtt_payload(self, rec, t, ltp):
        exit_side = "SELL" if rec["side"] == "LONG" else "BUY"
        tick = self._tick(rec["exchange"], rec["tradingsymbol"])

        def order(price):
            return {"exchange": rec["exchange"], "tradingsymbol": rec["tradingsymbol"], "transaction_type": exit_side,
                    "quantity": t["qty"], "order_type": "LIMIT", "product": rec["product"], "price": price}
        legs = []
        if t.get("sl") is not None:
            buf = -SL_LIMIT_BUFFER if exit_side == "SELL" else SL_LIMIT_BUFFER
            legs.append((t["sl"], order(round_tick(t["sl"] * (1 + buf), tick))))
        if t.get("tp") is not None:
            buf = -TP_LIMIT_BUFFER if exit_side == "SELL" else TP_LIMIT_BUFFER
            legs.append((t["tp"], order(round_tick(t["tp"] * (1 + buf), tick))))
        legs.sort(key=lambda leg: leg[0])  # Kite expects trigger_values ascending
        condition = {"exchange": rec["exchange"], "tradingsymbol": rec["tradingsymbol"],
                     "trigger_values": [leg[0] for leg in legs], "last_price": ltp}
        return ("two-leg" if len(legs) == 2 else "single"), condition, [leg[1] for leg in legs]

    def _gtt_active(self, gtt_id):
        try:
            return any(str(g["id"]) == str(gtt_id) and g["status"] == "active" for g in self.kite.gtts())
        except KiteError:
            return True  # unknown: assume active rather than risk a duplicate

    def _apply_tranche(self, o, d, rec, ltp, orders):
        t = {k: d[k] for k in ("tp_id", "sl_id", "tp", "sl", "qty")}
        if rec["mode"] == "gtt":
            payload = self._gtt_payload(rec, t, ltp)
            if o and o.get("gtt_id"):
                try:
                    self.kite.modify_gtt(o["gtt_id"], *payload)
                    t["gtt_id"] = o["gtt_id"]
                    return t
                except KiteError:
                    if self._gtt_active(o["gtt_id"]):
                        raise  # a real rejection: keep the old GTT rather than replace it
            t["gtt_id"] = self.kite.place_gtt(*payload)["trigger_id"]
            return t

        exit_side = "SELL" if rec["side"] == "LONG" else "BUY"
        tick = self._tick(rec["exchange"], rec["tradingsymbol"])
        for leg in ("tp", "sl"):
            oid = o.get(f"{leg}_order_id") if o else None
            is_open = bool(oid) and orders.get(oid, {}).get("status") in OPEN_STATUSES
            if t[leg] is None:
                if is_open:
                    self._cancel_quietly(oid)
                t[f"{leg}_order_id"] = None
                continue
            if leg == "tp":
                params = {"order_type": "LIMIT", "price": t["tp"]}
            else:
                buf = -SL_LIMIT_BUFFER if exit_side == "SELL" else SL_LIMIT_BUFFER
                params = {"order_type": "SL", "trigger_price": t["sl"], "price": round_tick(t["sl"] * (1 + buf), tick)}
            params["quantity"] = t["qty"]
            if is_open:
                self.kite.modify_order("regular", oid, params)
                t[f"{leg}_order_id"] = oid
            else:
                res = self.kite.place_order("regular", {
                    **params, "exchange": rec["exchange"], "tradingsymbol": rec["tradingsymbol"],
                    "transaction_type": exit_side, "product": rec["product"], "validity": "DAY",
                    "tag": "ktTP" if leg == "tp" else "ktSL"})
                t[f"{leg}_order_id"] = res["order_id"]
        return t

    def _drop_tranche(self, t, rec, orders=None):
        if rec["mode"] == "gtt":
            if t.get("gtt_id"):
                try:
                    self.kite.delete_gtt(t["gtt_id"])
                except KiteError as exc:
                    log.info("delete gtt failed: %s", exc.message)
            return
        if orders is None:
            orders = {o["order_id"]: o for o in self.kite.orders()}
        for leg in ("tp", "sl"):
            oid = t.get(f"{leg}_order_id")
            if oid and orders.get(oid, {}).get("status") in OPEN_STATUSES:
                self._cancel_quietly(oid)

    def _cancel_quietly(self, order_id):
        try:
            self.kite.cancel_order("regular", order_id)
        except KiteError as exc:
            log.info("cancel %s failed: %s", order_id, exc.message)

    # ---------------------------------------------------------------- remove
    def remove(self, exchange, symbol, product):
        with self.lock:
            rec = self.items.pop(self.key(exchange, symbol, product), None)
            self._save()
            if not rec:
                return
            orders = {o["order_id"]: o for o in self.kite.orders()} if rec["mode"] == "oco" else None
            for t in rec["tranches"]:
                self._drop_tranche(t, rec, orders)

    # --------------------------------------------------------------- watcher
    def sync(self):
        with self.lock:
            if not self.items or not self.kite.access_token:
                return
            changed = False
            orders = {o["order_id"]: o for o in self.kite.orders()}
            gtts = ({str(g["id"]): g for g in self.kite.gtts()}
                    if any(r["mode"] == "gtt" for r in self.items.values()) else {})
            positions = self.kite.positions()["net"]
            holdings = (self.kite.holdings() if any(r["product"] == "CNC" for r in self.items.values()) else [])
            for key, rec in list(self.items.items()):
                sym = rec["tradingsymbol"]
                keep = []
                for t in rec["tranches"]:
                    if rec["mode"] == "gtt":
                        g = gtts.get(str(t.get("gtt_id")))
                        status = g["status"] if g else "deleted"
                        if status == "active":
                            keep.append(t)
                            continue
                        changed = True
                        if status == "triggered":
                            fired = next((i for i, o in enumerate(g["orders"]) if o.get("result")), None)
                            trig = g["condition"]["trigger_values"][fired] if fired is not None else None
                            leg = "tp" if trig is not None and t.get("tp") is not None and abs(trig - t["tp"]) < 1e-6 else "sl"
                            self._event("success", f"{LEG_NAMES[leg]} triggered on {sym}",
                                        f"{t['qty']} qty at {t[leg]}: exit order sent.")
                        else:
                            self._event("info", f"A TP/SL on {sym} is no longer active", f"GTT {status}.")
                        continue

                    legs = {leg: orders.get(t.get(f"{leg}_order_id")) for leg in ("tp", "sl")}
                    done = next((leg for leg, o in legs.items() if o and o["status"] == "COMPLETE"), None)
                    if done:
                        other = "sl" if done == "tp" else "tp"
                        if legs[other] and legs[other]["status"] in OPEN_STATUSES:
                            self._cancel_quietly(legs[other]["order_id"])
                        changed = True
                        self._event("success", f"{LEG_NAMES[done]} hit on {sym}",
                                    f"Exited {t['qty']} @ {legs[done]['average_price']}"
                                    + (f"; matching {LEG_NAMES[other].lower()} cancelled." if t.get(other) is not None else "."))
                        continue
                    for leg, o in legs.items():
                        if o and o["status"] in ("REJECTED", "CANCELLED"):
                            changed = True
                            t[leg] = t[f"{leg}_id"] = t[f"{leg}_order_id"] = None
                            if o["status"] == "REJECTED":
                                self._event("error", f"{LEG_NAMES[leg]} order for {sym} rejected", o.get("status_message") or "")
                            else:
                                self._event("info", f"{LEG_NAMES[leg]} order for {sym} was cancelled")
                    if t.get("tp") is not None or t.get("sl") is not None:
                        keep.append(t)
                rec["tranches"] = keep

                qty = self.trade_quantity(rec["exchange"], sym, rec["product"], positions, holdings)
                long = rec["side"] == "LONG"
                if not keep or qty == 0 or (qty > 0) != long:
                    open_orders = orders if rec["mode"] == "oco" else None
                    for t in keep:
                        self._drop_tranche(t, rec, open_orders)
                    del self.items[key]
                    changed = True
                    if keep:
                        self._event("info", f"{sym} position closed", "Its remaining TP/SL orders were cancelled.")
                    continue
                full = abs(qty)
                if full != rec["quantity"]:
                    rec["quantity"] = full
                    changed = True
                tps, sls = legs_of(keep, "tp"), legs_of(keep, "sl")
                if sum(leg["qty"] for leg in tps) > full or sum(leg["qty"] for leg in sls) > full:
                    lot = self._lot(rec["exchange"], sym)
                    desired = build_tranches(trim(tps, full, long, "tp"), trim(sls, full, long, "sl"), long, lot)
                    rec["tranches"] = []
                    changed = True
                    try:
                        self._reconcile(keep, desired, rec, self._ltp(rec["exchange"], sym))
                        self._event("info", f"TP/SL on {sym} resized", f"Now covers {full} qty.")
                    except KiteError as exc:
                        self._event("error", f"Couldn't resize TP/SL on {sym}", exc.message)
                    if not rec["tranches"]:
                        del self.items[key]
            if changed:
                self._save()

    def start(self, interval=3):
        def run():
            while True:
                time.sleep(interval)
                try:
                    self.sync()
                except KiteError as exc:
                    log.info("protection sync: %s", exc.message)
                except Exception:  # keep the watcher alive no matter what
                    log.exception("protection sync crashed")
        threading.Thread(target=run, name="tp-sl-watcher", daemon=True).start()
