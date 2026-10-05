import hashlib
import hmac
import json

from app import credits
from app.db import Order
from conftest import WEBHOOK_SECRET


def _event(payment_id="pay_ABC", order_id="order_1", amount=64900, event="payment.captured"):
    return json.dumps({
        "entity": "event", "event": event,
        "payload": {"payment": {"entity": {"id": payment_id, "order_id": order_id, "amount": amount,
                                           "currency": "INR", "status": "captured"}}},
    }).encode()


def _post(client, body, secret=WEBHOOK_SECRET):
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post("/api/razorpay/webhook", content=body,
                       headers={"X-Razorpay-Signature": sig, "Content-Type": "application/json"})


def _order(db, user, credits_=1000):
    db.add(Order(id="order_1", user_id=user.id, amount_paise=64900, credits=credits_))
    db.commit()


def test_payment_captured_adds_credits_once(client, db, user):
    _order(db, user)
    body = _event()
    r1 = _post(client, body)
    assert r1.status_code == 200 and r1.json()["credited"] is True
    assert credits.balance(db, user.id) == 1000
    # Razorpay retries / duplicate deliveries must not add credits again.
    r2 = _post(client, body)
    assert r2.status_code == 200 and r2.json()["credited"] is False
    # Same payment id inside a differently-shaped event (e.g. re-sent later).
    r3 = _post(client, _event(payment_id="pay_ABC"))
    assert r3.json()["credited"] is False
    assert credits.balance(db, user.id) == 1000


def test_invalid_signature_rejected(client, db, user):
    _order(db, user)
    r = _post(client, _event(), secret="wrong")
    assert r.status_code == 400
    r = client.post("/api/razorpay/webhook", content=_event())
    assert r.status_code == 400
    assert credits.balance(db, user.id) == 0


def test_unknown_order_and_amount_mismatch_ignored(client, db, user):
    _order(db, user)
    assert _post(client, _event(order_id="order_other")).json()["status"] == "ignored"
    assert _post(client, _event(payment_id="pay_X", amount=100)).json()["status"] == "ignored"
    assert credits.balance(db, user.id) == 0


def test_other_events_ignored(client, db, user):
    _order(db, user)
    r = _post(client, _event(event="payment.failed"))
    assert r.json()["status"] == "ignored"
    assert credits.balance(db, user.id) == 0
