"""Real-time market data.

One upstream Kite WebSocket (wss://ws.kite.trade) is shared by every open
browser tab. Ticks are decoded here and pushed to browsers over Server-Sent
Events, coalesced to ~20 pushes/second per tab. Order postbacks arriving on
the same socket are forwarded too, so fills show up instantly.
"""
import json
import logging
import struct
import threading
import time

log = logging.getLogger(__name__)

WS_URL = "wss://ws.kite.trade"
SEGMENT_DIVISOR = {3: 10_000_000.0, 6: 10_000.0}  # CDS, BCD; everything else is paise
INDICES_SEGMENT = 9
PUSH_INTERVAL = 0.05


def parse_binary(data):
    """Decode a Kite binary market-data frame into quote dicts."""
    if len(data) < 4:  # 1-byte heartbeat
        return []
    count = struct.unpack(">H", data[:2])[0]
    off, out = 2, []
    for _ in range(count):
        size = struct.unpack(">H", data[off:off + 2])[0]
        off += 2
        quote = parse_packet(data[off:off + size])
        off += size
        if quote:
            out.append(quote)
    return out


def parse_packet(p):
    if len(p) < 8:
        return None
    token = struct.unpack(">I", p[:4])[0]
    segment = token & 0xFF
    div = SEGMENT_DIVISOR.get(segment, 100.0)

    def i32(o):
        return struct.unpack(">i", p[o:o + 4])[0]

    q = {"instrument_token": token, "last_price": i32(4) / div}
    if len(p) == 8:
        return q
    if segment == INDICES_SEGMENT:
        if len(p) >= 28:
            q["ohlc"] = {"high": i32(8) / div, "low": i32(12) / div, "open": i32(16) / div, "close": i32(20) / div}
            q["net_change"] = i32(24) / div
        return q
    if len(p) < 44:
        return q
    q.update(last_quantity=i32(8), average_price=i32(12) / div, volume=i32(16),
             buy_quantity=i32(20), sell_quantity=i32(24),
             ohlc={"open": i32(28) / div, "high": i32(32) / div, "low": i32(36) / div, "close": i32(40) / div})
    if len(p) >= 64:
        q.update(oi=i32(48), oi_day_high=i32(52), oi_day_low=i32(56))
    if len(p) >= 184:
        depth = {"buy": [], "sell": []}
        for i in range(10):
            o = 64 + i * 12
            level = {"quantity": i32(o), "price": i32(o + 4) / div, "orders": struct.unpack(">H", p[o + 8:o + 10])[0]}
            depth["buy" if i < 5 else "sell"].append(level)
        q["depth"] = depth
    return q


class _Client:
    def __init__(self):
        self.keys = set()
        self.depth_keys = set()  # only these get market depth (it is ~2/3 of a full quote)
        self.dirty = set()
        self.orders = False
        self.status = True  # send status on first loop
        self.event = threading.Event()


class TickHub:
    def __init__(self, kite, api_key, key_to_token, token_to_key, on_order=None):
        self.kite = kite
        self.api_key = api_key
        self.key_to_token = key_to_token
        self.token_to_key = token_to_key
        self.on_order = on_order
        self.clients = {}
        self.latest = {}
        self.lock = threading.RLock()
        self.status = "connecting"
        self.ws = None
        self.subscribed = set()
        self._token_in_use = None

    # ----------------------------------------------------------- browser side
    def register(self, cid):
        with self.lock:
            self.clients[cid] = self.clients.get(cid) or _Client()
            return self.clients[cid]

    def unregister(self, cid, client):
        with self.lock:
            if self.clients.get(cid) is client:
                del self.clients[cid]
                self._resubscribe()

    def set_keys(self, cid, keys, depth_keys=()):
        with self.lock:
            c = self.clients.get(cid)
            if not c:
                return False
            c.keys = set(keys)
            c.depth_keys = set(depth_keys) & c.keys
            c.dirty |= {k for k in c.keys if k in self.latest}  # immediate snapshot
            c.event.set()
            self._resubscribe()
            return True

    def stream(self, cid):
        client = self.register(cid)
        try:
            yield "retry: 2000\n\n"
            while True:
                if not client.event.wait(15):
                    yield ": ping\n\n"
                    continue
                time.sleep(PUSH_INTERVAL)
                with self.lock:
                    client.event.clear()
                    ticks = {k: (self.latest[k] if k in client.depth_keys else
                                 {f: v for f, v in self.latest[k].items() if f != "depth"})
                             for k in client.dirty if k in self.latest}
                    client.dirty = set()
                    orders, client.orders = client.orders, False
                    status, client.status = client.status, False
                if status:
                    yield f"event: status\ndata: {json.dumps({'status': self.status})}\n\n"
                if ticks:
                    yield f"event: ticks\ndata: {json.dumps(ticks)}\n\n"
                if orders:
                    yield "event: order\ndata: {}\n\n"
        finally:
            self.unregister(cid, client)

    # ------------------------------------------------------------ fan-out
    def _publish(self, key, quote):
        prev = self.latest.get(key)
        if prev and len(quote) <= 2:  # ltp-only packet: keep the richer fields
            quote = {**prev, **quote}
        self.latest[key] = quote
        for c in self.clients.values():
            if key in c.keys:
                c.dirty.add(key)
                c.event.set()

    def _order_update(self, data=None):
        with self.lock:
            for c in self.clients.values():
                c.orders = True
                c.event.set()
        if self.on_order:
            threading.Thread(target=self.on_order, args=(data,), daemon=True).start()

    def _set_status(self, status):
        if status == self.status:
            return
        self.status = status
        with self.lock:
            for c in self.clients.values():
                c.status = True
                c.event.set()

    # ------------------------------------------------------------ upstream
    def _wanted_tokens(self):
        keys = set().union(*(c.keys for c in self.clients.values())) if self.clients else set()
        return {t for t in (self.key_to_token(k) for k in keys) if t}

    def _send(self, payload):
        try:
            self.ws.send(json.dumps(payload))
        except Exception as exc:  # socket closing; the reconnect will resubscribe
            log.info("ticker send failed: %s", exc)

    def _resubscribe(self):
        if not self.ws or self.status != "live":
            return
        want = self._wanted_tokens()
        new, gone = want - self.subscribed, self.subscribed - want
        if new:
            self._send({"a": "subscribe", "v": sorted(new)})
            self._send({"a": "mode", "v": ["full", sorted(new)]})
        if gone:
            self._send({"a": "unsubscribe", "v": sorted(gone)})
        self.subscribed = want

    def _on_open(self, ws):
        log.info("ticker connected")
        self.subscribed = set()
        self._set_status("live")
        with self.lock:
            self._resubscribe()

    def _on_message(self, ws, message):
        if isinstance(message, bytes):
            quotes = parse_binary(message)
            with self.lock:
                for q in quotes:
                    key = self.token_to_key(q["instrument_token"])
                    if key:
                        self._publish(key, q)
            return
        try:
            msg = json.loads(message)
        except ValueError:
            return
        if msg.get("type") == "order":
            self._order_update(msg.get("data"))
        elif msg.get("type") == "error":
            log.warning("ticker error: %s", msg.get("data"))

    def start(self):
        threading.Thread(target=self._run, name="kite-ticker", daemon=True).start()
        threading.Thread(target=self._watch_token, name="kite-ticker-token", daemon=True).start()

    def _watch_token(self):
        # Re-login issues a new access token: drop the old socket so _run reconnects.
        while True:
            time.sleep(2)
            if self.ws and self.kite.access_token != self._token_in_use:
                self.ws.close()

    def _run(self):
        from websocket import WebSocketApp  # websocket-client

        backoff = 1
        while True:
            token = self.kite.access_token
            if not token:
                self._set_status("offline")
                time.sleep(1)
                continue
            self._token_in_use = token
            self._set_status("connecting")
            self.ws = WebSocketApp(f"{WS_URL}?api_key={self.api_key}&access_token={token}",
                                   on_open=self._on_open, on_message=self._on_message,
                                   on_error=lambda ws, err: log.warning("ticker: %s", err))
            started = time.time()
            self.ws.run_forever(ping_interval=20, ping_timeout=10)
            self.ws = None
            self.subscribed = set()
            self._set_status("connecting" if self.kite.access_token else "offline")
            backoff = 1 if time.time() - started > 60 else min(backoff * 2, 30)
            time.sleep(backoff)


class MockTickHub(TickHub):
    """Demo mode: synthesise ticks from MockKite several times a second."""

    def __init__(self, kite, on_order=None, interval=0.25):
        super().__init__(kite, "", lambda k: k, lambda t: t, on_order)
        self.interval = interval

    def start(self):
        threading.Thread(target=self._run, name="mock-ticker", daemon=True).start()

    def _resubscribe(self):
        pass

    def _run(self):
        last_orders = None
        while True:
            time.sleep(self.interval)
            if not self.kite.access_token:
                self._set_status("offline")
                continue
            self._set_status("live")
            with self.lock:
                keys = set().union(*(c.keys for c in self.clients.values())) if self.clients else set()
            quotes = self.kite.quote(sorted(keys)) if keys else {}
            sig = [(o["order_id"], o["status"]) for o in self.kite.orders_list]
            with self.lock:
                for key, q in quotes.items():
                    self._publish(key, q)
            if last_orders is not None and sig != last_orders:
                self._order_update()
            last_orders = sig
