"""
Thin OANDA REST v20 client.
Handles auth, pricing, orders, positions, and account summary. No third-party
SDK needed - just `requests`.
"""

import requests
from config import config


class OandaClient:
    def __init__(self, token: str, account_id: str, base_url: str):
        self.token = token
        self.account_id = account_id
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })
        self.timeout = 15

    # ---- helpers ----------------------------------------------------------
    def _url(self, path: str) -> str:
        return f"{self.base_url}/v3/accounts/{self.account_id}{path}"

    def _request(self, method: str, path: str, **kwargs):
        r = self.session.request(method, self._url(path), timeout=self.timeout, **kwargs)
        if r.status_code >= 400:
            raise RuntimeError(
                f"OANDA API {r.status_code} for {method} {path}: {r.text[:300]}"
            )
        return r.json()

    def _request_global(self, method: str, path: str, **kwargs):
        r = self.session.request(method, f"{self.base_url}/v3{path}", timeout=self.timeout, **kwargs)
        if r.status_code >= 400:
            raise RuntimeError(f"OANDA API {r.status_code} for {method} {path}: {r.text[:300]}")
        return r.json()

    # ---- account ----------------------------------------------------------
    def account_summary(self) -> dict:
        # The /summary endpoint wraps fields inside an "account" object:
        #   {"account": {"balance": ..., "unrealizedPL": ..., ...}}
        return self._request("GET", "/summary")["account"]

    # ---- pricing ----------------------------------------------------------
    def price(self, instrument: str) -> dict:
        data = self._request_global(
            "GET", f"/accounts/{self.account_id}/pricing?instruments={instrument}"
        )
        p = data["prices"][0]
        return {
            "instrument": p["instrument"],
            "bid": float(p["bids"][0]["price"]),
            "ask": float(p["asks"][0]["price"]),
            "mid": (float(p["bids"][0]["price"]) + float(p["asks"][0]["price"])) / 2.0,
        }

    # ---- orders -----------------------------------------------------------
    def market_order(self, instrument: str, units: int) -> dict:
        """units > 0 => BUY, units < 0 => SELL."""
        payload = {
            "order": {
                "type": "MARKET",
                "instrument": instrument,
                "units": str(units),
                "positionFill": "DEFAULT",
            }
        }
        return self._request("POST", "/orders", json=payload)

    def stop_order(self, instrument: str, units: int, price: str, order_type="STOP") -> dict:
        """Place a pending stop/limit order. units>0 BUY, units<0 SELL."""
        payload = {
            "order": {
                "type": order_type,
                "instrument": instrument,
                "units": str(units),
                "price": str(price),
                "timeInForce": "GTC",
                "positionFill": "DEFAULT",
            }
        }
        return self._request("POST", "/orders", json=payload)

    def cancel_order(self, order_id: str) -> dict:
        return self._request("PUT", f"/orders/{order_id}/cancel")

    # ---- positions --------------------------------------------------------
    def open_positions(self) -> list:
        data = self._request("GET", "/openPositions")
        return data.get("positions", [])

    def position(self, instrument: str) -> dict | None:
        for p in self.open_positions():
            if p["instrument"] == instrument:
                return p
        return None

    def close_position(self, instrument: str, side: str, units="ALL") -> dict:
        """side: 'long' (close BUY) or 'short' (close SELL)."""
        key = "longUnits" if side == "long" else "shortUnits"
        payload = {key: units}
        return self._request("PUT", f"/positions/{instrument}/close", json=payload)

    # ---- candle / historical (optional, for a context row) ---------------
    def candles(self, instrument: str, count: int = 20, granularity: str = "M1") -> list:
        data = self._request_global(
            "GET", f"/instruments/{instrument}/candles?count={count}&granularity={granularity}"
        )
        return data.get("candles", [])
