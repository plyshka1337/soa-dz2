"""HTTP scenario against the running API; leaves rows available for SELECT."""

import json
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx

client = httpx.Client(base_url="http://127.0.0.1:8000", timeout=10)


def call(method, path, token=None, body=None, expected=200):
    response = client.request(method, path, headers={"Authorization": "Bearer " + token} if token else {},
                              **({"json": body} if body is not None else {}))
    print(f"{method} {path} -> {response.status_code}")
    assert response.status_code == expected, response.text
    return response.json() if response.content else None


def main():
    for attempt in range(20):
        try:
            client.get("/openapi.json").raise_for_status()
            break
        except httpx.HTTPError:
            if attempt == 19:
                raise
            time.sleep(.5)
    suffix = uuid4().hex[:10]
    tokens = {}
    for role in ("USER", "SELLER"):
        credentials = {"username": f"demo_{role}_{suffix}", "password": "demo-password123"}
        call("POST", "/auth/register", body={**credentials, "role": role}, expected=201)
        tokens[role] = call("POST", "/auth/login", body=credentials)["access_token"]
    product = call("POST", "/products", tokens["SELLER"], {"name": "Demo book", "price": 100, "stock": 5, "category": "demo_" + suffix, "status": "ACTIVE"}, 201)
    now = datetime.now(timezone.utc)
    promo = call("POST", "/promo-codes", tokens["SELLER"], {"code": "DEMO_" + suffix.upper(), "discount_type": "PERCENTAGE", "discount_value": 10,
                 "min_order_amount": 150, "max_uses": 1, "valid_from": (now - timedelta(days=1)).isoformat(), "valid_until": (now + timedelta(days=1)).isoformat()}, 201)
    items = [{"product_id": product["id"], "quantity": 2}]
    order = call("POST", "/orders", tokens["USER"], {"items": items, "promo_code": promo["code"]}, 201)
    assert order["total_amount"] == 180
    call("POST", "/orders", tokens["USER"], {"items": items}, 429)
    changed = call("PUT", "/orders/" + order["id"], tokens["USER"], {"items": [{"product_id": product["id"], "quantity": 1}]})
    assert changed["total_amount"] == 100 and changed["promo_code_id"] is None
    canceled = call("POST", "/orders/" + order["id"] + "/cancel", tokens["USER"])
    assert canceled["status"] == "CANCELED"
    restored = call("GET", "/products/" + product["id"], tokens["USER"])
    assert restored["stock"] == 5
    call("POST", "/orders", tokens["USER"], {"items": []}, 400)
    print(json.dumps({"product_id": product["id"], "order_id": order["id"], "promo_code": promo["code"], "stock_after_cancel": restored["stock"]}, indent=2))
    client.close()


if __name__ == "__main__":
    main()
