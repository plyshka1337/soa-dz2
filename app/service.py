import hashlib
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from uuid import uuid4

from fastapi import Response
from psycopg.errors import UniqueViolation

from app.auth import decode_token, password_hash, password_matches, token_pair
from app.db import connect
from app.errors import ApiError

LIMIT_MINUTES = float(os.getenv("ORDER_LIMIT_MINUTES", "1"))
NEXT_STATUS = {"CREATED": "PAYMENT_PENDING", "PAYMENT_PENDING": "PAID", "PAID": "SHIPPED", "SHIPPED": "COMPLETED"}


def register(body):
    data = body.model_dump(mode="json")
    try:
        with connect() as conn:
            return conn.execute("INSERT INTO users (id, username, password_hash, role) VALUES (%s, %s, %s, %s) RETURNING id, username, role",
                                (uuid4(), data["username"], password_hash(body.password.get_secret_value()), data["role"])).fetchone()
    except UniqueViolation:
        raise ApiError(409, "USERNAME_TAKEN", "Username already exists") from None


def login(body):
    with connect() as conn:
        user = conn.execute("SELECT * FROM users WHERE username = %s", (body.username,)).fetchone()
        if user is None or not password_matches(body.password.get_secret_value(), user["password_hash"]):
            raise ApiError(401, "TOKEN_INVALID", "Invalid username or password")
        return token_pair(conn, user)


def refresh(body):
    data = decode_token(body.refresh_token, "refresh")
    digest = hashlib.sha256(body.refresh_token.encode()).hexdigest()
    with connect() as conn:
        saved = conn.execute("DELETE FROM refresh_tokens WHERE token_hash = %s AND expires_at > now() RETURNING user_id", (digest,)).fetchone()
        if saved is None or str(saved["user_id"]) != data["sub"]:
            raise ApiError(401, "REFRESH_TOKEN_INVALID", "Unknown or already used refresh token")
        user = conn.execute("SELECT id, role FROM users WHERE id = %s", (saved["user_id"],)).fetchone()
        return token_pair(conn, user)


def product(conn, id, user=None):
    row = conn.execute("SELECT * FROM products WHERE id = %s" + (" FOR UPDATE" if user else ""), (id,)).fetchone()
    if row is None:
        raise ApiError(404, "PRODUCT_NOT_FOUND", "Product not found")
    if user and user["role"] != "ADMIN" and row["seller_id"] != user["id"]:
        raise ApiError(403, "ACCESS_DENIED", "Product belongs to another seller")
    return row


def seller(conn, requested, user):
    owner = requested or user["id"]
    if user["role"] != "ADMIN" and str(owner) != str(user["id"]):
        raise ApiError(403, "ACCESS_DENIED", "Seller can manage only own products")
    if not conn.execute("SELECT 1 FROM users WHERE id = %s AND role IN ('SELLER', 'ADMIN')", (owner,)).fetchone():
        raise ApiError(400, "VALIDATION_ERROR", "Seller does not exist", {"fields": [{"field": "seller_id", "message": "Expected an existing seller"}]})
    return owner


def create_product(body, user):
    data = body.model_dump(mode="json")
    with connect() as conn:
        owner = seller(conn, data.get("seller_id"), user)
        return conn.execute("INSERT INTO products (id, name, description, price, stock, category, status, seller_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                            (uuid4(), data["name"], data["description"], Decimal(str(data["price"])), data["stock"], data["category"], data["status"], owner)).fetchone()


def get_product(id, user):
    with connect() as conn:
        return product(conn, id)


def list_products(page, size, status, category, user):
    clauses, args = [], []
    if status is not None:
        clauses.append("status = %s")
        args.append(status.value)
    if category is not None:
        clauses.append("category = %s")
        args.append(category)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with connect() as conn:
        count = conn.execute("SELECT count(*) AS n FROM products" + where, args).fetchone()["n"]
        rows = conn.execute("SELECT * FROM products" + where + " ORDER BY created_at, id LIMIT %s OFFSET %s", args + [size, page * size]).fetchall()
        return {"items": rows, "totalElements": count, "page": page, "size": size}


def update_product(id, body, user):
    data = body.model_dump(mode="json")
    with connect() as conn:
        current = product(conn, id, user)
        owner = seller(conn, data.get("seller_id") or current["seller_id"], user)
        return conn.execute("UPDATE products SET name=%s, description=%s, price=%s, stock=%s, category=%s, status=%s, seller_id=%s WHERE id=%s RETURNING *",
                            (data["name"], data["description"], Decimal(str(data["price"])), data["stock"], data["category"], data["status"], owner, id)).fetchone()


def delete_product(id, user):
    with connect() as conn:
        product(conn, id, user)
        conn.execute("UPDATE products SET status='ARCHIVED' WHERE id=%s", (id,))
    return Response(status_code=204)


def create_promo(body, user):
    data = body.model_dump(mode="json")
    if body.valid_until < body.valid_from:
        raise ApiError(400, "VALIDATION_ERROR", "valid_until must not precede valid_from", {"fields": [{"field": "valid_until", "message": "Must be >= valid_from"}]})
    try:
        with connect() as conn:
            return conn.execute("INSERT INTO promo_codes (id, code, discount_type, discount_value, min_order_amount, max_uses, valid_from, valid_until, active) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                                (uuid4(), data["code"], data["discount_type"], Decimal(str(data["discount_value"])), Decimal(str(data["min_order_amount"])), data["max_uses"], data["valid_from"], data["valid_until"], data["active"])).fetchone()
    except UniqueViolation:
        raise ApiError(409, "PROMO_CODE_EXISTS", "Promo code already exists") from None


def lock_user(conn, user):
    conn.execute("SELECT id FROM users WHERE id = %s FOR UPDATE", (user["id"],))


def check_limit(conn, user, operation):
    last = conn.execute("SELECT created_at FROM user_operations WHERE user_id = %s AND operation_type = %s ORDER BY created_at DESC LIMIT 1", (user["id"], operation)).fetchone()
    if last and datetime.now(timezone.utc) - last["created_at"] < timedelta(minutes=LIMIT_MINUTES):
        raise ApiError(429, "ORDER_LIMIT_EXCEEDED", "Order operation rate limit exceeded")


def record_operation(conn, user, operation):
    conn.execute("INSERT INTO user_operations (id, user_id, operation_type) VALUES (%s,%s,%s)", (uuid4(), user["id"], operation))


def order(conn, id, user, lock=False):
    row = conn.execute("SELECT * FROM orders WHERE id = %s" + (" FOR UPDATE" if lock else ""), (id,)).fetchone()
    if row is None:
        raise ApiError(404, "ORDER_NOT_FOUND", "Order not found")
    if user["role"] != "ADMIN" and row["user_id"] != user["id"]:
        raise ApiError(403, "ORDER_OWNERSHIP_VIOLATION", "Order belongs to another user")
    return row


def order_response(conn, id):
    row = conn.execute("SELECT * FROM orders WHERE id = %s", (id,)).fetchone()
    row["items"] = conn.execute("SELECT id, product_id, quantity, price_at_order FROM order_items WHERE order_id = %s ORDER BY product_id", (id,)).fetchall()
    return row


def reserve(conn, items, old=()):
    quantities = {}
    for item in items:
        key = item["product_id"]
        quantities[key] = quantities.get(key, 0) + item["quantity"]
    ids = list(quantities) + [str(item["product_id"]) for item in old]
    rows = conn.execute("SELECT * FROM products WHERE id = ANY(%s::uuid[]) ORDER BY id FOR UPDATE", (ids,)).fetchall()
    products = {str(row["id"]): row for row in rows}
    for item in old:
        key = str(item["product_id"])
        conn.execute("UPDATE products SET stock = stock + %s WHERE id=%s", (item["quantity"], key))
        products[key]["stock"] += item["quantity"]
    for key in quantities:
        if key not in products:
            raise ApiError(404, "PRODUCT_NOT_FOUND", "Product not found", {"product_id": key})
        if products[key]["status"] != "ACTIVE":
            raise ApiError(409, "PRODUCT_INACTIVE", "Product is not active", {"product_id": key})
    shortages = [{"product_id": key, "requested": quantity, "available": products[key]["stock"]}
                 for key, quantity in quantities.items() if quantity > products[key]["stock"]]
    if shortages:
        raise ApiError(409, "INSUFFICIENT_STOCK", "Insufficient stock", {"products": shortages})
    result = []
    for key, quantity in quantities.items():
        conn.execute("UPDATE products SET stock = stock - %s WHERE id=%s", (quantity, key))
        result.append({"product_id": key, "quantity": quantity, "price_at_order": products[key]["price"]})
    return result


def price_order(conn, items, code=None, promo_id=None):
    total = sum((item["price_at_order"] * item["quantity"] for item in items), Decimal("0"))
    if total > Decimal("9999999999.99"):
        raise ApiError(400, "VALIDATION_ERROR", "Order amount exceeds numeric capacity")
    promo = None
    if code:
        promo = conn.execute("SELECT * FROM promo_codes WHERE code=%s FOR UPDATE", (code,)).fetchone()
    elif promo_id:
        promo = conn.execute("SELECT * FROM promo_codes WHERE id=%s FOR UPDATE", (promo_id,)).fetchone()
    if not code and not promo_id:
        return total, Decimal("0"), None
    now = datetime.now(timezone.utc)
    valid = promo and promo["active"] and promo["valid_from"] <= now <= promo["valid_until"] and promo["current_uses"] < promo["max_uses"] + bool(promo_id)
    enough = promo and total >= promo["min_order_amount"]
    if not valid or not enough:
        if promo_id:
            conn.execute("UPDATE promo_codes SET current_uses=current_uses-1 WHERE id=%s", (promo_id,))
            return total, Decimal("0"), None
        code = "PROMO_CODE_INVALID" if not valid else "PROMO_CODE_MIN_AMOUNT"
        raise ApiError(422, code, "Promo code is not applicable")
    value = promo["discount_value"]
    discount = (total * value / 100).quantize(Decimal("0.01"), rounding=ROUND_DOWN) if promo["discount_type"] == "PERCENTAGE" and value <= 70 else Decimal("0")
    if promo["discount_type"] == "FIXED_AMOUNT":
        discount = min(value, total)
    if not promo_id:
        conn.execute("UPDATE promo_codes SET current_uses=current_uses+1 WHERE id=%s", (promo["id"],))
    return total - discount, discount, promo["id"]


def save_items(conn, order_id, items):
    for item in items:
        conn.execute("INSERT INTO order_items (id, order_id, product_id, quantity, price_at_order) VALUES (%s,%s,%s,%s,%s)",
                     (uuid4(), order_id, item["product_id"], item["quantity"], item["price_at_order"]))


def create_order(body, user):
    data = body.model_dump(mode="json")
    with connect() as conn:
        lock_user(conn, user)
        check_limit(conn, user, "CREATE_ORDER")
        if conn.execute("SELECT 1 FROM orders WHERE user_id=%s AND status IN ('CREATED','PAYMENT_PENDING')", (user["id"],)).fetchone():
            raise ApiError(409, "ORDER_HAS_ACTIVE", "User already has an active order")
        items = reserve(conn, data["items"])
        total, discount, promo_id = price_order(conn, items, code=data["promo_code"])
        id = uuid4()
        conn.execute("INSERT INTO orders (id, user_id, promo_code_id, total_amount, discount_amount) VALUES (%s,%s,%s,%s,%s)", (id, user["id"], promo_id, total, discount))
        save_items(conn, id, items)
        record_operation(conn, user, "CREATE_ORDER")
        return order_response(conn, id)


def get_order(id, user):
    with connect() as conn:
        order(conn, id, user)
        return order_response(conn, id)


def update_order(id, body, user):
    data = body.model_dump(mode="json")
    with connect() as conn:
        lock_user(conn, user)
        current = order(conn, id, user, lock=True)
        if current["status"] != "CREATED":
            raise ApiError(409, "INVALID_STATE_TRANSITION", "Only CREATED orders can be edited")
        check_limit(conn, user, "UPDATE_ORDER")
        old = conn.execute("SELECT * FROM order_items WHERE order_id=%s", (id,)).fetchall()
        items = reserve(conn, data["items"], old)
        total, discount, promo_id = price_order(conn, items, promo_id=current["promo_code_id"])
        conn.execute("UPDATE orders SET total_amount=%s, discount_amount=%s, promo_code_id=%s WHERE id=%s", (total, discount, promo_id, id))
        conn.execute("DELETE FROM order_items WHERE order_id=%s", (id,))
        save_items(conn, id, items)
        record_operation(conn, user, "UPDATE_ORDER")
        return order_response(conn, id)


def cancel_order(id, user):
    with connect() as conn:
        lock_user(conn, user)
        current = order(conn, id, user, lock=True)
        if current["status"] not in ("CREATED", "PAYMENT_PENDING"):
            raise ApiError(409, "INVALID_STATE_TRANSITION", "Order cannot be canceled")
        items = conn.execute("SELECT * FROM order_items WHERE order_id=%s ORDER BY product_id", (id,)).fetchall()
        for item in items:
            conn.execute("UPDATE products SET stock=stock+%s WHERE id=%s", (item["quantity"], item["product_id"]))
        if current["promo_code_id"]:
            conn.execute("UPDATE promo_codes SET current_uses=current_uses-1 WHERE id=%s", (current["promo_code_id"],))
        conn.execute("UPDATE orders SET status='CANCELED' WHERE id=%s", (id,))
        return order_response(conn, id)


def change_order_status(id, body, user):
    with connect() as conn:
        current = order(conn, id, user, lock=True)
        if NEXT_STATUS.get(current["status"]) != body.status.value:
            raise ApiError(409, "INVALID_STATE_TRANSITION", "Illegal order status transition")
        conn.execute("UPDATE orders SET status=%s WHERE id=%s", (body.status.value, id))
        return order_response(conn, id)
