import io
import json
import logging
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import jwt
from fastapi.testclient import TestClient

from app import auth, service
from app.db import connect
from app.main import app, logger


class MarketplaceTests(unittest.TestCase):
    def setUp(self):
        self.logs = io.StringIO()
        self.previous_handlers = logger.handlers
        logger.handlers = [logging.StreamHandler(self.logs)]
        self.client = TestClient(app)
        self.user, self.user_id = self.account("USER")
        self.seller, self.seller_id = self.account("SELLER")
        response = self.client.post("/auth/login", json={"username": "admin", "password": "admin-demo-password"})
        self.assertEqual(200, response.status_code, response.text)
        self.admin = {"Authorization": "Bearer " + response.json()["access_token"]}
        self.category = str(uuid4())

    def tearDown(self):
        self.client.close()
        logger.handlers = self.previous_handlers

    def account(self, role):
        name = "test_" + uuid4().hex
        response = self.client.post("/auth/register", json={"username": name, "password": "password123", "role": role})
        self.assertEqual(201, response.status_code, response.text)
        user_id = response.json()["id"]
        tokens = self.client.post("/auth/login", json={"username": name, "password": "password123"}).json()
        return {"Authorization": "Bearer " + tokens["access_token"]}, user_id

    def product(self, **changes):
        payload = {"name": "Book", "description": None, "price": 100, "stock": 10, "category": self.category, "status": "ACTIVE", **changes}
        response = self.client.post("/products", headers=self.seller, json=payload)
        self.assertEqual(201, response.status_code, response.text)
        return response.json()

    def promo(self, **changes):
        now = datetime.now(timezone.utc)
        payload = {"code": "P_" + uuid4().hex[:10].upper(), "discount_type": "PERCENTAGE", "discount_value": 10,
                   "min_order_amount": 100, "max_uses": 10, "valid_from": (now - timedelta(days=1)).isoformat(),
                   "valid_until": (now + timedelta(days=1)).isoformat(), "active": True, **changes}
        response = self.client.post("/promo-codes", headers=self.seller, json=payload)
        self.assertEqual(201, response.status_code, response.text)
        return response.json()

    def create(self, product, quantity=1, headers=None, promo=None):
        body = {"items": [{"product_id": product["id"], "quantity": quantity}]}
        if promo:
            body["promo_code"] = promo["code"]
        return self.client.post("/orders", headers=headers or self.user, json=body)

    def age_operations(self):
        with connect() as conn:
            conn.execute("UPDATE user_operations SET created_at=now()-(%s * interval '1 minute') WHERE user_id=%s",
                         (service.LIMIT_MINUTES + 1, self.user_id))

    def stock(self, product):
        with connect() as conn:
            return conn.execute("SELECT stock FROM products WHERE id=%s", (product["id"],)).fetchone()["stock"]

    def error(self, response, status, code):
        self.assertEqual(status, response.status_code, response.text)
        self.assertEqual(code, response.json()["error_code"], response.text)
        UUID(response.headers["X-Request-Id"])

    def test_product_crud_filter_pagination_and_soft_delete(self):
        first = self.product()
        self.product(status="INACTIVE")
        page = self.client.get("/products", headers=self.user, params={"category": self.category, "status": "ACTIVE", "page": 0, "size": 1})
        self.assertEqual(200, page.status_code, page.text)
        self.assertEqual(1, page.json()["totalElements"])
        self.assertEqual(first["id"], page.json()["items"][0]["id"])
        self.assertEqual(0, page.json()["page"])
        changed = {key: first[key] for key in ("name", "description", "price", "stock", "category", "status")}
        changed["price"] = 150
        response = self.client.put("/products/" + first["id"], headers=self.seller, json=changed)
        self.assertEqual(200, response.status_code, response.text)
        self.assertNotEqual(first["updated_at"], response.json()["updated_at"])
        self.assertEqual(204, self.client.delete("/products/" + first["id"], headers=self.seller).status_code)
        with connect() as conn:
            saved = conn.execute("SELECT status, created_at, updated_at FROM products WHERE id=%s", (first["id"],)).fetchone()
        self.assertEqual("ARCHIVED", saved["status"])
        self.assertIsNotNone(saved["created_at"])

    def test_roles_and_product_ownership(self):
        product = self.product()
        other_seller, _ = self.account("SELLER")
        self.error(self.client.delete("/products/" + product["id"], headers=other_seller), 403, "ACCESS_DENIED")
        self.error(self.client.post("/products", headers=self.user, json={"name": "a", "price": 1, "stock": 0, "category": "a", "status": "ACTIVE"}), 403, "ACCESS_DENIED")
        self.error(self.create(product, headers=self.seller), 403, "ACCESS_DENIED")
        response = self.client.post("/products", headers=self.admin, json={"name": "admin item", "price": 1, "stock": 1, "category": "a", "status": "ACTIVE", "seller_id": self.seller_id})
        self.assertEqual(201, response.status_code, response.text)
        self.assertEqual(self.seller_id, response.json()["seller_id"])
        self.error(self.client.get("/products"), 401, "TOKEN_INVALID")
        self.error(self.client.post("/auth/register", json={"username": "admin2", "password": "password123", "role": "ADMIN"}), 400, "VALIDATION_ERROR")

    def test_contract_validation_types_bounds_and_fields(self):
        base = {"name": "a", "price": 1, "stock": 1, "category": "a", "status": "ACTIVE"}
        bad = [{"name": ""}, {"name": "a" * 256}, {"description": "a" * 4001}, {"price": 0}, {"price": .001},
               {"price": "1.00"}, {"price": True}, {"stock": -1}, {"stock": "1"}, {"stock": True}, {"category": "a" * 101}]
        for changes in bad:
            with self.subTest(changes=list(changes)):
                response = self.client.post("/products", headers=self.seller, json={**base, **changes})
                self.error(response, 400, "VALIDATION_ERROR")
                self.assertTrue(response.json()["details"]["fields"])
        valid = {"name": "a" * 255, "description": "a" * 4000, "price": .01, "stock": 0, "category": "a" * 100, "status": "ACTIVE"}
        response = self.client.post("/products", headers=self.seller, json=valid)
        self.assertEqual(201, response.status_code, response.text)
        product = self.product()
        for quantity in (0, 1000, True, "1"):
            self.error(self.create(product, quantity), 400, "VALIDATION_ERROR")
        for body in ({"items": []}, {"items": [{"product_id": product["id"], "quantity": 1}] * 51}, {"items": [{"product_id": product["id"], "quantity": 1}], "promo_code": "bad"}):
            self.error(self.client.post("/orders", headers=self.user, json=body), 400, "VALIDATION_ERROR")
        self.error(self.client.get("/products", headers=self.user, params={"page": -1}), 400, "VALIDATION_ERROR")
        self.error(self.client.get("/products/not-a-uuid", headers=self.user), 400, "VALIDATION_ERROR")
        self.assertEqual(10, self.stock(product))

    def test_order_reservation_snapshot_and_operation_are_persisted(self):
        product = self.product(price=10)
        response = self.create(product, 2)
        self.assertEqual(201, response.status_code, response.text)
        order = response.json()
        self.assertEqual(Decimal("20"), Decimal(str(order["total_amount"])))
        self.assertEqual(8, self.stock(product))
        with connect() as conn:
            conn.execute("UPDATE products SET price=99 WHERE id=%s", (product["id"],))
            operation = conn.execute("SELECT operation_type FROM user_operations WHERE user_id=%s", (self.user_id,)).fetchone()
        loaded = self.client.get("/orders/" + order["id"], headers=self.user).json()
        self.assertEqual(Decimal("10"), Decimal(str(loaded["items"][0]["price_at_order"])))
        self.assertEqual(Decimal("20"), Decimal(str(loaded["total_amount"])))
        self.assertEqual("CREATE_ORDER", operation["operation_type"])

    def test_create_checks_rate_before_active_order(self):
        product = self.product()
        self.assertEqual(201, self.create(product).status_code)
        self.error(self.create(product), 429, "ORDER_LIMIT_EXCEEDED")
        self.age_operations()
        self.error(self.create(product), 409, "ORDER_HAS_ACTIVE")

    def test_missing_inactive_and_shortage_roll_back(self):
        product = self.product(stock=1)
        missing = {"id": str(uuid4())}
        self.error(self.create(missing), 404, "PRODUCT_NOT_FOUND")
        inactive = self.product(status="INACTIVE")
        self.error(self.create(inactive), 409, "PRODUCT_INACTIVE")
        response = self.create(product, 2)
        self.error(response, 409, "INSUFFICIENT_STOCK")
        self.assertEqual(1, response.json()["details"]["products"][0]["available"])
        self.assertEqual(1, self.stock(product))
        with connect() as conn:
            self.assertEqual(0, conn.execute("SELECT count(*) AS n FROM orders WHERE user_id=%s", (self.user_id,)).fetchone()["n"])

    def test_invalid_promo_rolls_back_already_reserved_stock(self):
        product = self.product()
        for promo in ({"code": "MISSING"}, self.promo(active=False), self.promo(valid_until=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()), self.promo(min_order_amount=500)):
            response = self.create(product, promo=promo)
            self.assertEqual(422, response.status_code, response.text)
            self.assertEqual(10, self.stock(product))
        with connect() as conn:
            self.assertEqual(0, conn.execute("SELECT count(*) AS n FROM user_operations WHERE user_id=%s", (self.user_id,)).fetchone()["n"])

    def test_percentage_fixed_and_percentage_over_70(self):
        product = self.product()
        for changes, expected in (({"discount_value": 10}, "90"), ({"discount_type": "FIXED_AMOUNT", "discount_value": 200}, "0"), ({"discount_value": 80}, "100")):
            headers, _ = self.account("USER")
            response = self.create(product, headers=headers, promo=self.promo(**changes))
            self.assertEqual(201, response.status_code, response.text)
            self.assertEqual(Decimal(expected), Decimal(str(response.json()["total_amount"])))

    def test_update_rollback_then_releases_inapplicable_promo(self):
        product = self.product(stock=10)
        promo = self.promo(min_order_amount=150, max_uses=1)
        original = self.create(product, 2, promo=promo).json()
        bad = {"items": [{"product_id": product["id"], "quantity": 20}]}
        self.error(self.client.put("/orders/" + original["id"], headers=self.user, json=bad), 409, "INSUFFICIENT_STOCK")
        self.assertEqual(8, self.stock(product))
        valid = {"items": [{"product_id": product["id"], "quantity": 2}]}
        kept = self.client.put("/orders/" + original["id"], headers=self.user, json=valid)
        self.assertEqual(200, kept.status_code, kept.text)
        self.assertEqual(promo["id"], kept.json()["promo_code_id"])
        self.age_operations()
        valid["items"][0]["quantity"] = 1
        response = self.client.put("/orders/" + original["id"], headers=self.user, json=valid)
        self.assertEqual(200, response.status_code, response.text)
        self.assertIsNone(response.json()["promo_code_id"])
        self.assertEqual(9, self.stock(product))
        with connect() as conn:
            self.assertEqual(0, conn.execute("SELECT current_uses FROM promo_codes WHERE id=%s", (promo["id"],)).fetchone()["current_uses"])
        self.error(self.client.put("/orders/" + original["id"], headers=self.user, json=valid), 429, "ORDER_LIMIT_EXCEEDED")

    def test_cancel_releases_stock_and_promo_once(self):
        product, promo = self.product(), self.promo()
        order = self.create(product, 2, promo=promo).json()
        response = self.client.post("/orders/" + order["id"] + "/cancel", headers=self.user)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("CANCELED", response.json()["status"])
        self.assertEqual(10, self.stock(product))
        self.error(self.client.post("/orders/" + order["id"] + "/cancel", headers=self.user), 409, "INVALID_STATE_TRANSITION")
        with connect() as conn:
            self.assertEqual(0, conn.execute("SELECT current_uses FROM promo_codes WHERE id=%s", (promo["id"],)).fetchone()["current_uses"])

    def test_expired_and_disabled_promos_are_removed_on_update(self):
        product = self.product()
        for expired in (False, True):
            headers, _ = self.account("USER")
            promo = self.promo(max_uses=1)
            order = self.create(product, 2, headers=headers, promo=promo).json()
            with connect() as conn:
                if expired:
                    conn.execute("UPDATE promo_codes SET valid_until=now()-interval '1 second' WHERE id=%s", (promo["id"],))
                else:
                    conn.execute("UPDATE promo_codes SET active=false WHERE id=%s", (promo["id"],))
            response = self.client.put("/orders/" + order["id"], headers=headers, json={"items": [{"product_id": product["id"], "quantity": 1}]})
            self.assertEqual(200, response.status_code, response.text)
            self.assertIsNone(response.json()["promo_code_id"])
            self.assertEqual(0, response.json()["discount_amount"])
            with connect() as conn:
                self.assertEqual(0, conn.execute("SELECT current_uses FROM promo_codes WHERE id=%s", (promo["id"],)).fetchone()["current_uses"])

    def test_states_and_order_ownership(self):
        product = self.product()
        order = self.create(product).json()
        other_user, _ = self.account("USER")
        path = "/orders/" + order["id"]
        self.error(self.client.get(path, headers=other_user), 403, "ORDER_OWNERSHIP_VIOLATION")
        self.error(self.client.get(path, headers=self.seller), 403, "ACCESS_DENIED")
        self.error(self.client.get("/orders/" + str(uuid4()), headers=self.user), 404, "ORDER_NOT_FOUND")
        self.error(self.client.post(path + "/status", headers=self.admin, json={"status": "PAID"}), 409, "INVALID_STATE_TRANSITION")
        for status in ("PAYMENT_PENDING", "PAID", "SHIPPED", "COMPLETED"):
            response = self.client.post(path + "/status", headers=self.admin, json={"status": status})
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(status, response.json()["status"])
        self.error(self.client.put(path, headers=self.user, json={"items": [{"product_id": product["id"], "quantity": 1}]}), 409, "INVALID_STATE_TRANSITION")
        self.error(self.client.post(path + "/cancel", headers=self.user), 409, "INVALID_STATE_TRANSITION")
        self.assertEqual(200, self.client.get(path, headers=self.admin).status_code)

    def test_jwt_expiration_refresh_rotation_and_token_types(self):
        tokens = self.client.post("/auth/login", json={"username": "admin", "password": "admin-demo-password"}).json()
        expired = jwt.encode({"sub": "00000000-0000-0000-0000-000000000001", "role": "ADMIN", "type": "access", "exp": datetime.now(timezone.utc) - timedelta(seconds=1)}, auth.SECRET, algorithm="HS256")
        self.error(self.client.get("/products", headers={"Authorization": "Bearer " + expired}), 401, "TOKEN_EXPIRED")
        self.error(self.client.get("/products", headers={"Authorization": "Bearer " + tokens["refresh_token"]}), 401, "TOKEN_INVALID")
        refreshed = self.client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
        self.assertEqual(200, refreshed.status_code, refreshed.text)
        self.error(self.client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]}), 401, "REFRESH_TOKEN_INVALID")
        self.error(self.client.post("/auth/refresh", json={"refresh_token": "bad"}), 401, "REFRESH_TOKEN_INVALID")

    def test_json_logging_contains_request_id_and_masks_password(self):
        response = self.client.post("/auth/login", json={"username": "admin", "password": "admin-demo-password"})
        entries = [json.loads(line) for line in self.logs.getvalue().splitlines()]
        log = entries[-1]
        self.assertEqual(response.headers["X-Request-Id"], log["request_id"])
        self.assertTrue({"request_id", "method", "endpoint", "status_code", "duration_ms", "user_id", "timestamp"}.issubset(log))
        self.assertEqual("***", log["body"]["password"])
        self.assertNotIn("admin-demo-password", self.logs.getvalue())

    def test_concurrent_orders_cannot_oversell_or_exceed_promo_uses(self):
        for use_promo in (False, True):
            product = self.product(stock=2 if use_promo else 1)
            promo = self.promo(max_uses=1) if use_promo else None
            clients = [self.account("USER")[0] for _ in range(2)]
            with ThreadPoolExecutor(max_workers=2) as executor:
                responses = list(executor.map(lambda headers: self.create(product, headers=headers, promo=promo), clients))
            statuses = sorted(response.status_code for response in responses)
            self.assertEqual([201, 422 if use_promo else 409], statuses, [r.text for r in responses])
            self.assertEqual(1 if use_promo else 0, self.stock(product))


if __name__ == "__main__":
    unittest.main()
