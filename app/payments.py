"""Razorpay: order creation and webhook handling."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time

import httpx
from sqlalchemy.orm import Session

from . import credits
from .config import Settings
from .db import Order

log = logging.getLogger("infinity.payments")
RAZORPAY_ORDERS_URL = "https://api.razorpay.com/v1/orders"


class PaymentError(Exception):
    pass


def create_order(session: Session, settings: Settings, user_id: int) -> Order:
    if not (settings.razorpay_key_id and settings.razorpay_key_secret):
        raise PaymentError("Payments are not configured")
    amount = settings.plan_price_inr * 100
    payload = {
        "amount": amount,
        "currency": "INR",
        "receipt": f"u{user_id}-{int(time.time())}",
        "notes": {"user_id": str(user_id), "credits": str(settings.plan_credits)},
    }
    try:
        r = httpx.post(RAZORPAY_ORDERS_URL, json=payload, timeout=20,
                       auth=(settings.razorpay_key_id, settings.razorpay_key_secret))
    except httpx.HTTPError as e:
        raise PaymentError(f"Could not reach Razorpay: {e}") from e
    if r.status_code != 200:
        log.error("Razorpay order creation failed: %s %s", r.status_code, r.text[:300])
        raise PaymentError("Could not create the payment order")
    data = r.json()
    order = Order(id=data["id"], user_id=user_id, amount_paise=amount, credits=settings.plan_credits)
    session.add(order)
    session.commit()
    return order


def verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def handle_webhook(session: Session, settings: Settings, body: bytes, signature: str | None) -> dict:
    """Process a verified Razorpay webhook. Raises PaymentError on a bad signature."""
    if not verify_signature(settings.razorpay_webhook_secret, body, signature):
        raise PaymentError("invalid signature")
    event = json.loads(body)
    if event.get("event") != "payment.captured":
        return {"status": "ignored", "event": event.get("event")}
    payment = event["payload"]["payment"]["entity"]
    payment_id = payment["id"]
    order = session.get(Order, payment.get("order_id") or "")
    if order is None:
        log.warning("payment %s for unknown order %s", payment_id, payment.get("order_id"))
        return {"status": "ignored", "reason": "unknown order"}
    if int(payment.get("amount", 0)) != order.amount_paise or payment.get("currency", "INR") != "INR":
        log.error("payment %s amount mismatch for order %s", payment_id, order.id)
        return {"status": "ignored", "reason": "amount mismatch"}
    added = credits.add_purchase(session, order.user_id, order.credits, payment_id,
                                 note=f"Razorpay order {order.id}")
    order.status = "paid"
    session.commit()
    log.info("payment %s: %s", payment_id, "credits added" if added else "already processed")
    return {"status": "ok", "credited": added}
