import pytest

from app import credits
from app.db import LedgerEntry


def test_purchase_reserve_deduct(db, user):
    assert credits.balance(db, user.id) == 0
    assert credits.add_purchase(db, user.id, 1000, "pay_1")
    assert credits.balance(db, user.id) == 1000

    credits.reserve(db, user.id, "job-1", 300)
    assert credits.balance(db, user.id) == 700          # held credits cannot be spent
    assert credits.reserved(db, user.id) == 300

    credits.deduct(db, "job-1")
    assert credits.balance(db, user.id) == 700          # final charge equals the hold
    assert credits.reserved(db, user.id) == 0
    kinds = [e.kind for e in db.query(LedgerEntry).filter_by(job_id="job-1").order_by(LedgerEntry.id)]
    assert kinds == ["hold", "release", "charge"]


def test_reserve_release_on_failure(db, user):
    credits.add_purchase(db, user.id, 500, "pay_2")
    credits.reserve(db, user.id, "job-2", 200)
    assert credits.balance(db, user.id) == 300
    credits.release(db, "job-2")
    assert credits.balance(db, user.id) == 500
    assert credits.reserved(db, user.id) == 0


def test_insufficient_credits(db, user):
    credits.add_purchase(db, user.id, 50, "pay_3")
    with pytest.raises(credits.InsufficientCredits) as exc:
        credits.reserve(db, user.id, "job-3", 60)
    assert exc.value.needed == 60 and exc.value.available == 50
    assert credits.balance(db, user.id) == 50
    assert db.query(LedgerEntry).filter_by(job_id="job-3").count() == 0


def test_holds_count_against_balance(db, user):
    credits.add_purchase(db, user.id, 100, "pay_4")
    credits.reserve(db, user.id, "job-a", 80)
    with pytest.raises(credits.InsufficientCredits):
        credits.reserve(db, user.id, "job-b", 30)


def test_job_settles_only_once(db, user):
    credits.add_purchase(db, user.id, 100, "pay_5")
    credits.reserve(db, user.id, "job-4", 40)
    credits.deduct(db, "job-4")
    with pytest.raises(credits.LedgerError):
        credits.deduct(db, "job-4")
    with pytest.raises(credits.LedgerError):
        credits.release(db, "job-4")
    assert credits.balance(db, user.id) == 60


def test_double_reserve_rejected(db, user):
    credits.add_purchase(db, user.id, 100, "pay_6")
    credits.reserve(db, user.id, "job-5", 10)
    with pytest.raises(credits.LedgerError):
        credits.reserve(db, user.id, "job-5", 10)
    assert credits.balance(db, user.id) == 90


def test_release_without_reservation(db, user):
    with pytest.raises(credits.LedgerError):
        credits.release(db, "missing-job")
