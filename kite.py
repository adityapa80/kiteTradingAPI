"""Minimal Kite Connect v3 REST client, plus an offline mock for demo mode."""
import functools
import hashlib
import itertools
import json
import math
import threading
import random
import time
from datetime import date, datetime, timedelta

import requests

KITE_ROOT = "https://api.kite.trade"
LOGIN_URL = "https://kite.zerodha.com/connect/login"


class KiteError(Exception):
    def __init__(self, message, error_type="GeneralException", status=500):
        super().__init__(message)
        self.message = message
        self.error_type = error_type
        self.status = status


class KiteClient:
    def __init__(self, api_key, api_secret, access_token=None, timeout=10):
        self.api_key = api_key
        self.api_secret = api_secret
        self.access_token = access_token
        self.timeout = timeout
        self.http = requests.Session()
        # Browser polling, the TP/SL watcher and order postbacks call Kite from many threads at
        # once; a bigger pool keeps their connections warm instead of re-doing TLS over ~40 ms.
        self.http.mount("https://", requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=32))

    # ---- plumbing -------------------------------------------------------
    def login_url(self):
        return f"{LOGIN_URL}?v=3&api_key={self.api_key}"

    def _headers(self):
        headers = {"X-Kite-Version": "3"}
        if self.access_token:
            headers["Authorization"] = f"token {self.api_key}:{self.access_token}"
        return headers

    def _request(self, method, path, params=None, data=None, json_body=None, raw=False):
        for attempt in range(2):
            try:
                resp = self.http.request(
                    method, KITE_ROOT + path, params=params, data=data, json=json_body,
                    headers=self._headers(), timeout=self.timeout,
                )
            except requests.RequestException as exc:
                raise KiteError(f"Network error talking to Kite: {exc}", "NetworkException", 502)
            if resp.status_code != 429 or attempt:
                break
            time.sleep(1)  # rate limited; retry once

        if raw and resp.status_code == 200:
            return resp.text
        try:
            body = resp.json()
        except ValueError:
            raise KiteError(f"Unexpected response from Kite (HTTP {resp.status_code})",
                            "DataException", 502)
        if resp.status_code >= 400 or body.get("status") == "error":
            raise KiteError(body.get("message", "Unknown error"),
                            body.get("error_type", "GeneralException"), resp.status_code)
        return body.get("data")

    # ---- session --------------------------------------------------------
    def generate_session(self, request_token):
        checksum = hashlib.sha256(
            (self.api_key + request_token + self.api_secret).encode()).hexdigest()
        data = self._request("POST", "/session/token", data={
            "api_key": self.api_key, "request_token": request_token, "checksum": checksum})
        self.access_token = data["access_token"]
        return data

    def invalidate_session(self):
        if self.access_token:
            self._request("DELETE", "/session/token",
                          params={"api_key": self.api_key, "access_token": self.access_token})
        self.access_token = None

    # ---- user / portfolio ----------------------------------------------
    def profile(self):
        return self._request("GET", "/user/profile")

    def margins(self):
        return self._request("GET", "/user/margins")

    def holdings(self):
        return self._request("GET", "/portfolio/holdings")

    def positions(self):
        return self._request("GET", "/portfolio/positions")

    # ---- orders ---------------------------------------------------------
    def orders(self):
        return self._request("GET", "/orders")

    def trades(self):
        return self._request("GET", "/trades")

    def order_history(self, order_id):
        return self._request("GET", f"/orders/{order_id}")

    def place_order(self, variety, params):
        return self._request("POST", f"/orders/{variety}", data=params)

    def modify_order(self, variety, order_id, params):
        return self._request("PUT", f"/orders/{variety}/{order_id}", data=params)

    def cancel_order(self, variety, order_id):
        return self._request("DELETE", f"/orders/{variety}/{order_id}")

    def order_margins(self, orders):
        return self._request("POST", "/margins/orders", json_body=orders)

    # ---- market data ----------------------------------------------------
    def quote(self, instruments):
        return self._request("GET", "/quote", params=[("i", i) for i in instruments])

    def ltp(self, instruments):
        return self._request("GET", "/quote/ltp", params=[("i", i) for i in instruments])

    # ---- GTT ------------------------------------------------------------
    def gtts(self):
        return self._request("GET", "/gtt/triggers")

    def place_gtt(self, trigger_type, condition, orders):
        return self._request("POST", "/gtt/triggers", data={
            "type": trigger_type, "condition": json.dumps(condition), "orders": json.dumps(orders)})

    def modify_gtt(self, trigger_id, trigger_type, condition, orders):
        return self._request("PUT", f"/gtt/triggers/{trigger_id}", data={
            "type": trigger_type, "condition": json.dumps(condition), "orders": json.dumps(orders)})

    def delete_gtt(self, trigger_id):
        return self._request("DELETE", f"/gtt/triggers/{trigger_id}")

    def historical(self, token, interval, from_dt, to_dt):
        return self._request("GET", f"/instruments/historical/{token}/{interval}",
                             params={"from": from_dt, "to": to_dt})

    def instruments_csv(self, exchange=None):
        path = f"/instruments/{exchange}" if exchange else "/instruments"
        return self._request("GET", path, raw=True)


# ==========================================================================
# Mock client: same interface, fake data. Enabled with KITE_MOCK=1 so the UI
# can be explored without credentials or market hours. Never touches Kite.
# ==========================================================================
MOCK_INSTRUMENTS = [
    # token, symbol, name, exchange, segment, type, lot, tick, base price
    (256265, "NIFTY 50", "NIFTY 50", "NSE", "INDICES", "EQ", 0, 0, 24850.0),
    (260105, "NIFTY BANK", "NIFTY BANK", "NSE", "INDICES", "EQ", 0, 0, 54200.0),
    (265, "SENSEX", "SENSEX", "BSE", "INDICES", "EQ", 0, 0, 81500.0),
    (738561, "RELIANCE", "RELIANCE INDUSTRIES", "NSE", "NSE", "EQ", 1, 0.1, 1385.0),
    (408065, "INFY", "INFOSYS", "NSE", "NSE", "EQ", 1, 0.1, 1520.0),
    (2953217, "TCS", "TATA CONSULTANCY SERV", "NSE", "NSE", "EQ", 1, 0.1, 3450.0),
    (341249, "HDFCBANK", "HDFC BANK", "NSE", "NSE", "EQ", 1, 0.1, 1960.0),
    (1270529, "ICICIBANK", "ICICI BANK", "NSE", "NSE", "EQ", 1, 0.1, 1410.0),
    (779521, "SBIN", "STATE BANK OF INDIA", "NSE", "NSE", "EQ", 1, 0.05, 815.0),
    (884737, "TATAMOTORS", "TATA MOTORS", "NSE", "NSE", "EQ", 1, 0.05, 690.0),
    (128083204, "RELIANCE", "RELIANCE INDUSTRIES", "BSE", "BSE", "EQ", 1, 0.05, 1385.5),
    (12345602, "NIFTY26OCTFUT", "NIFTY", "NFO", "NFO-FUT", "FUT", 65, 0.1, 24910.0),
]

# Weekly index option chains generated around spot:
# name, spot key, exchange, segment, lot, strike step, expiry weekday (Mon=0)
MOCK_OPTION_CHAINS = [
    ("NIFTY", "NSE:NIFTY 50", "NFO", "NFO-OPT", 65, 50, 1),
    ("SENSEX", "BSE:SENSEX", "BFO", "BFO-OPT", 20, 100, 3),
]
MONTH_CODES = "123456789OND"


def _norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _bs_price(spot, strike, days, typ, vol=0.13):
    t = max(days, 0.05) / 365
    d1 = (math.log(spot / strike) + 0.5 * vol * vol * t) / (vol * math.sqrt(t))
    d2 = d1 - vol * math.sqrt(t)
    if typ == "CE":
        return spot * _norm_cdf(d1) - strike * _norm_cdf(d2)
    return strike * _norm_cdf(-d2) - spot * _norm_cdf(-d1)

OPEN_STATUSES = {"OPEN", "TRIGGER PENDING", "AMO REQ RECEIVED"}


class MockKite:
    def __init__(self):
        self.access_token = None
        self.by_key = {}
        for tok, sym, name, exch, seg, typ, lot, tick, px in MOCK_INSTRUMENTS:
            self.by_key[f"{exch}:{sym}"] = {
                "instrument_token": tok, "tradingsymbol": sym, "name": name,
                "exchange": exch, "segment": seg, "instrument_type": typ,
                "lot_size": lot, "tick_size": tick, "close": px, "last": px,
            }
        self.by_key["NFO:NIFTY26OCTFUT"]["expiry"] = "2026-10-27"
        self._gen_options()
        self.orders_list = []
        self.trades_list = []
        self.net = {}  # (exchange, symbol, product) -> {qty, buy_value, sell_value, ...}
        self.cash = 500000.0
        self.gtt_list = {}
        self.gtt_seq = itertools.count(1001)
        self._lock = threading.RLock()

    def login_url(self):
        return "/callback?request_token=mock&status=success"

    def generate_session(self, request_token):
        self.access_token = "mock-token"
        return {"access_token": self.access_token, "user_id": "DEMO01", "user_name": "Demo Trader"}

    def invalidate_session(self):
        self.access_token = None

    def _gen_options(self):
        token = itertools.count(30000000)
        today = date.today()
        for name, spot_key, exch, seg, lot, step, weekday in MOCK_OPTION_CHAINS:
            spot = self.by_key[spot_key]["close"]
            atm = round(spot / step) * step
            first = today + timedelta(days=(weekday - today.weekday()) % 7)
            for week in range(3):
                exp = first + timedelta(days=7 * week)
                code = f"{name}{exp:%y}{MONTH_CODES[exp.month - 1]}{exp.day:02d}"
                for k in range(-30, 31):
                    strike = atm + k * step
                    for typ in ("CE", "PE"):
                        sym = f"{code}{int(strike)}{typ}"
                        inst = {"instrument_token": next(token), "tradingsymbol": sym, "name": name,
                                "exchange": exch, "segment": seg, "instrument_type": typ, "lot_size": lot,
                                "tick_size": 0.05, "expiry": exp.isoformat(), "strike": float(strike),
                                "underlying": spot_key,
                                "oi_base": int(random.uniform(2e5, 6e6) / (1 + abs(k) / 6)) // lot * lot}
                        inst["close"] = inst["last"] = self._option_price(inst, spot)
                        self.by_key[f"{exch}:{sym}"] = inst

    @staticmethod
    def _option_price(inst, spot):
        exp = datetime.fromisoformat(inst["expiry"]).replace(hour=15, minute=30)
        days = (exp - datetime.now()).total_seconds() / 86400
        px = _bs_price(spot, inst["strike"], days + 0.3, inst["instrument_type"])
        return round(max(0.05, round(px / 0.05) * 0.05), 2)

    def _tick(self, inst):
        self._move(inst)
        self._on_price(inst)
        return inst["last"]

    def _move(self, inst):
        if "underlying" in inst:  # options follow the simulated index
            inst["last"] = self._option_price(inst, self.by_key[inst["underlying"]]["last"])
            return
        vol = 0.00005 if inst["segment"] == "INDICES" else 0.0003
        step = inst["close"] * vol * random.gauss(0, 1)
        inst["last"] = round(max(inst["close"] * 0.9, min(inst["close"] * 1.1, inst["last"] + step)), 2)

    def _on_price(self, inst):
        """React to every price change the way an exchange would, so a brief touch of a
        trigger or limit price is never missed between polls."""
        ex, sym, ltp = inst["exchange"], inst["tradingsymbol"], inst["last"]
        for g in list(self.gtt_list.values()):
            cond = g["condition"]
            if g["status"] == "active" and cond["exchange"] == ex and cond["tradingsymbol"] == sym:
                self._check_gtt(g, ltp)
        for o in self.orders_list:
            if o["exchange"] == ex and o["tradingsymbol"] == sym and o["status"] in ("OPEN", "TRIGGER PENDING"):
                self._match(o, ltp)
        if inst["segment"] == "INDICES":  # re-price options that have working orders or GTTs
            key = f"{ex}:{sym}"
            for dep in self._working_instruments():
                if dep.get("underlying") == key:
                    self._move(dep)
                    self._on_price(dep)

    def _working_instruments(self):
        keys = {(o["exchange"], o["tradingsymbol"]) for o in self.orders_list if o["status"] in ("OPEN", "TRIGGER PENDING")}
        keys |= {(g["condition"]["exchange"], g["condition"]["tradingsymbol"])
                 for g in self.gtt_list.values() if g["status"] == "active"}
        return [self.by_key[f"{e}:{t}"] for e, t in keys if f"{e}:{t}" in self.by_key]

    def _inst(self, exchange, symbol):
        inst = self.by_key.get(f"{exchange}:{symbol}")
        if not inst:
            raise KiteError(f"Invalid instrument {exchange}:{symbol}", "InputException", 400)
        return inst

    def profile(self):
        return {"user_id": "DEMO01", "user_name": "Demo Trader", "user_shortname": "Demo",
                "email": "demo@example.com", "broker": "ZERODHA",
                "exchanges": ["NSE", "BSE", "NFO"], "products": ["CNC", "MIS", "NRML"]}

    def margins(self):
        used = sum(abs(p["quantity"]) * p["average_price"] * (0.2 if p["product"] == "MIS" else 1)
                   for p in self._positions())
        seg = {"enabled": True, "net": round(self.cash - used, 2),
               "available": {"cash": self.cash, "live_balance": round(self.cash - used, 2),
                             "opening_balance": self.cash, "collateral": 0, "intraday_payin": 0},
               "utilised": {"debits": round(used, 2), "span": 0, "exposure": 0, "option_premium": 0}}
        return {"equity": seg, "commodity": {**seg, "net": 0,
                "available": {"cash": 0, "live_balance": 0, "opening_balance": 0, "collateral": 0,
                              "intraday_payin": 0}, "utilised": {"debits": 0}}}

    def holdings(self):
        out = []
        for sym, qty, avg in [("INFY", 40, 1410.25), ("HDFCBANK", 25, 1655.8), ("SBIN", 100, 842.1)]:
            inst = self._inst("NSE", sym)
            ltp = self._tick(inst)
            p = self.net.get(("NSE", sym, "CNC"))
            used = max(0, p["sell_qty"] - p["buy_qty"]) if p else 0
            out.append({"tradingsymbol": sym, "exchange": "NSE", "instrument_token": inst["instrument_token"],
                        "product": "CNC", "quantity": qty, "t1_quantity": 0, "used_quantity": used,
                        "average_price": avg,
                        "last_price": ltp, "close_price": inst["close"],
                        "pnl": round((ltp - avg) * qty, 2),
                        "day_change": round(ltp - inst["close"], 2),
                        "day_change_percentage": round((ltp - inst["close"]) / inst["close"] * 100, 2)})
        return out

    def _positions(self):
        out = []
        for (exch, sym, prod), p in self.net.items():
            inst = self._inst(exch, sym)
            ltp = self._tick(inst)
            qty = p["buy_qty"] - p["sell_qty"]
            avg = (p["buy_value"] / p["buy_qty"] if qty > 0 else
                   p["sell_value"] / p["sell_qty"] if qty < 0 else 0)
            pnl = p["sell_value"] - p["buy_value"] + qty * ltp
            out.append({"tradingsymbol": sym, "exchange": exch, "product": prod, "quantity": qty,
                        "instrument_token": inst["instrument_token"],
                        "average_price": round(avg, 2), "last_price": ltp, "close_price": inst["close"],
                        "pnl": round(pnl, 2), "m2m": round(pnl, 2), "multiplier": 1,
                        "buy_value": round(p["buy_value"], 2), "sell_value": round(p["sell_value"], 2),
                        "buy_quantity": p["buy_qty"], "sell_quantity": p["sell_qty"]})
        return out

    def positions(self):
        net = self._positions()
        return {"net": net, "day": net}

    def orders(self):
        self._match_open_orders()
        return list(self.orders_list)

    def trades(self):
        return list(self.trades_list)

    def order_history(self, order_id):
        return [o for o in self.orders_list if o["order_id"] == order_id]

    def _fill(self, order, price):
        order.update(status="COMPLETE", filled_quantity=order["quantity"], pending_quantity=0,
                     average_price=price, status_message=None)
        key = (order["exchange"], order["tradingsymbol"], order["product"])
        p = self.net.setdefault(key, {"buy_qty": 0, "sell_qty": 0, "buy_value": 0.0, "sell_value": 0.0})
        side = "buy" if order["transaction_type"] == "BUY" else "sell"
        p[f"{side}_qty"] += order["quantity"]
        p[f"{side}_value"] += order["quantity"] * price
        self.trades_list.append({
            "trade_id": str(random.randint(10**7, 10**8)), "order_id": order["order_id"],
            "exchange": order["exchange"], "tradingsymbol": order["tradingsymbol"],
            "transaction_type": order["transaction_type"], "product": order["product"],
            "quantity": order["quantity"], "average_price": price,
            "fill_timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})

    def _check_gtt(self, g, ltp):
        cond = g["condition"]
        tv = cond["trigger_values"]
        if len(tv) == 2:
            leg = 0 if ltp <= tv[0] else 1 if ltp >= tv[1] else None
        else:
            above = cond["last_price"] < tv[0]
            leg = 0 if (above and ltp >= tv[0]) or (not above and ltp <= tv[0]) else None
        if leg is not None:
            order = g["orders"][leg]
            params = {k: order[k] for k in ("exchange", "tradingsymbol", "transaction_type", "quantity",
                                            "order_type", "product", "price")}
            g["status"] = "triggered"
            res = self.place_order("regular", params)
            order["result"] = {"triggered_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                               "order_result": {"order_id": res["order_id"], "status": "success"}}

    def _match(self, o, ltp):
        buy = o["transaction_type"] == "BUY"
        if o["status"] == "TRIGGER PENDING":
            trig = o["trigger_price"]
            if not ((buy and ltp >= trig) or (not buy and ltp <= trig)):
                return
            if o["order_type"] == "SL-M":
                self._fill(o, ltp)
                return
            o["status"] = "OPEN"
        if (buy and ltp <= o["price"]) or (not buy and ltp >= o["price"]):
            self._fill(o, ltp)

    def _match_open_orders(self):
        for inst in self._working_instruments():
            self._tick(inst)

    def place_order(self, variety, params):
        inst = self._inst(params["exchange"], params["tradingsymbol"])
        if inst["segment"] == "INDICES":
            raise KiteError("Indices are not tradable", "InputException", 400)
        qty = int(params["quantity"])
        if qty <= 0 or (inst["lot_size"] > 1 and qty % inst["lot_size"]):
            raise KiteError(f"Quantity should be a multiple of lot size {inst['lot_size']}",
                            "InputException", 400)
        order_id = str(int(time.time() * 1000)) + str(random.randint(10, 99))
        order = {"order_id": order_id, "variety": variety, "status": "OPEN",
                 "exchange": params["exchange"], "tradingsymbol": params["tradingsymbol"],
                 "instrument_token": inst["instrument_token"],
                 "transaction_type": params["transaction_type"], "order_type": params["order_type"],
                 "product": params["product"], "validity": params.get("validity", "DAY"),
                 "quantity": qty, "filled_quantity": 0, "pending_quantity": qty,
                 "price": float(params.get("price") or 0),
                 "trigger_price": float(params.get("trigger_price") or 0),
                 "average_price": 0, "tag": params.get("tag"), "status_message": None,
                 "order_timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        self.orders_list.append(order)
        if variety == "amo":
            order["status"] = "AMO REQ RECEIVED"
        elif order["order_type"] == "MARKET":
            self._move(inst)  # not _tick: the matcher must not see this order before it is filled
            self._fill(order, inst["last"])
        elif order["order_type"] in ("SL", "SL-M"):
            order["status"] = "TRIGGER PENDING"
        else:
            self._match(order, inst["last"])
        return {"order_id": order_id}

    def modify_order(self, variety, order_id, params):
        for o in self.orders_list:
            if o["order_id"] == order_id:
                if o["status"] not in OPEN_STATUSES:
                    raise KiteError("Order cannot be modified as it is not open", "OrderException", 400)
                for k in ("quantity", "price", "trigger_price"):
                    if params.get(k) not in (None, ""):
                        o[k] = int(params[k]) if k == "quantity" else float(params[k])
                if params.get("order_type"):
                    o["order_type"] = params["order_type"]
                o["pending_quantity"] = o["quantity"]
                return {"order_id": order_id}
        raise KiteError("Order not found", "InputException", 400)

    def cancel_order(self, variety, order_id):
        for o in self.orders_list:
            if o["order_id"] == order_id:
                if o["status"] not in OPEN_STATUSES:
                    raise KiteError("Order cannot be cancelled as it is not open", "OrderException", 400)
                o["status"] = "CANCELLED"
                return {"order_id": order_id}
        raise KiteError("Order not found", "InputException", 400)

    def order_margins(self, orders):
        out = []
        for o in orders:
            inst = self._inst(o["exchange"], o["tradingsymbol"])
            price = float(o.get("price") or 0) or inst["last"]
            factor = 0.2 if o.get("product") == "MIS" else (0.12 if inst["segment"] == "NFO-FUT" else 1)
            out.append({"total": round(price * int(o["quantity"]) * factor, 2)})
        return out

    @staticmethod
    def _volume(inst):
        # Cumulative day volume, like Kite: grows a little on every tick.
        lot = max(inst.get("lot_size") or 1, 1)
        inst["day_volume"] = inst.get("day_volume", random.randint(10**5, 10**6) * lot) + random.randint(0, 40) * lot
        return inst["day_volume"]

    def ltp(self, instruments):
        return {k: {"instrument_token": self.by_key[k]["instrument_token"], "last_price": self._tick(self.by_key[k])}
                for k in instruments if k in self.by_key}

    def gtts(self):
        return [dict(g) for g in self.gtt_list.values()]

    def place_gtt(self, trigger_type, condition, orders):
        gid = next(self.gtt_seq)
        self.gtt_list[gid] = {"id": gid, "type": trigger_type, "status": "active",
                              "condition": dict(condition), "orders": [{**o, "result": None} for o in orders],
                              "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        return {"trigger_id": gid}

    def modify_gtt(self, trigger_id, trigger_type, condition, orders):
        g = self.gtt_list.get(int(trigger_id))
        if not g or g["status"] != "active":
            raise KiteError("GTT is not active", "InputException", 400)
        if trigger_type != g["type"]:  # like Kite: a GTT's type can't be changed by modifying it
            raise KiteError("GTT type cannot be modified", "InputException", 400)
        g.update(type=trigger_type, condition=dict(condition), orders=[{**o, "result": None} for o in orders])
        return {"trigger_id": g["id"]}

    def delete_gtt(self, trigger_id):
        g = self.gtt_list.get(int(trigger_id))
        if not g or g["status"] != "active":
            raise KiteError("GTT is not active", "InputException", 400)
        g["status"] = "deleted"
        return {"trigger_id": g["id"]}

    def quote(self, instruments):
        out = {}
        for key in instruments:
            inst = self.by_key.get(key)
            if not inst:
                continue
            ltp = self._tick(inst)
            spread = inst["tick_size"] or 0.05
            out[key] = {
                "instrument_token": inst["instrument_token"], "last_price": ltp,
                "volume": self._volume(inst),
                "ohlc": {"open": inst["close"], "high": max(ltp, inst["close"] * 1.01),
                         "low": min(ltp, inst["close"] * 0.99), "close": inst["close"]},
                "net_change": 0,
                **({"oi": int(inst["oi_base"] * random.uniform(0.98, 1.02))} if "oi_base" in inst else {}),
                "depth": None if inst["segment"] == "INDICES" else {
                    "buy": [{"price": round(ltp - spread * (i + 1), 2), "quantity": random.randint(1, 900),
                             "orders": random.randint(1, 20)} for i in range(5)],
                    "sell": [{"price": round(ltp + spread * (i + 1), 2), "quantity": random.randint(1, 900),
                              "orders": random.randint(1, 20)} for i in range(5)]},
            }
        return out

    def historical(self, token, interval, from_dt, to_dt):
        inst = next((i for i in self.by_key.values() if str(i["instrument_token"]) == str(token)), None)
        if not inst:
            raise KiteError("Invalid token", "InputException", 400)
        step = {"minute": 1, "3minute": 3, "5minute": 5, "10minute": 10, "15minute": 15,
                "30minute": 30, "60minute": 60}.get(interval)
        start = datetime.strptime(from_dt, "%Y-%m-%d %H:%M:%S")
        # Align like Kite: intraday candles step from the 09:15 open, daily ones at midnight.
        start = start.replace(hour=9, minute=15, second=0) if step else start.replace(hour=0, minute=0, second=0)
        end = datetime.strptime(to_dt, "%Y-%m-%d %H:%M:%S")
        rng = random.Random(token)
        price = inst["close"] * 0.9
        candles, t = [], start
        while t <= end and len(candles) < 3000:
            if t.weekday() < 5 and (step is None or (9 * 60 + 15) <= t.hour * 60 + t.minute < 15 * 60 + 30):
                o = price
                c = o * (1 + rng.gauss(0.0002, 0.004 if step else 0.012))
                h, lo = max(o, c) * (1 + abs(rng.gauss(0, 0.002))), min(o, c) * (1 - abs(rng.gauss(0, 0.002)))
                candles.append([t.strftime("%Y-%m-%dT%H:%M:%S+0530"), round(o, 2), round(h, 2),
                                round(lo, 2), round(c, 2), rng.randint(10**4, 10**6)])
                price = c
            t += timedelta(minutes=step) if step else timedelta(days=1)
            if step and t.hour * 60 + t.minute >= 15 * 60 + 30:
                t = (t + timedelta(days=1)).replace(hour=9, minute=15)
        if candles:  # anchor the series so it ends at the current simulated price
            f = inst["last"] / candles[-1][4]
            candles = [[c[0], *(round(v * f, 2) for v in c[1:5]), c[5]] for c in candles]
        return {"candles": candles}

    def instruments_csv(self, exchange=None):
        rows = ["instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,"
                "tick_size,lot_size,instrument_type,segment,exchange"]
        for i in self.by_key.values():
            rows.append(f'{i["instrument_token"]},0,{i["tradingsymbol"]},"{i["name"]}",0,{i.get("expiry", "")},'
                        f'{i.get("strike", 0)},{i["tick_size"]},{i["lot_size"]},{i["instrument_type"]},'
                        f'{i["segment"]},{i["exchange"]}')
        return "\n".join(rows)


def _locked(fn):
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)
    return wrapper


# The mock is shared by Flask request threads and the TP/SL watcher thread.
for _name, _fn in list(vars(MockKite).items()):
    if callable(_fn) and not _name.startswith("_"):
        setattr(MockKite, _name, _locked(_fn))
