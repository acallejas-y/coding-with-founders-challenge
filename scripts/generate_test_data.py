"""
Generates and seeds SQLite with 150+ transactions for VidaRx recovery API testing.

Distribution:
- 60% approved, 25% declined, 10% pending, 5% unknown (real_state)
- 4 processors: bancosur, mexpay, andespsp, cashvoucher
- Currencies: MXN, COP, CLP
- 15-20 duplicate clusters (same customer_id + amount + within 5 minutes)
- Edge cases: stale transactions, currency mismatches, null customer_id
"""
import sys
import os
import random
from datetime import datetime, timedelta
import uuid

# Allow running from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import engine, SessionLocal
from app import models

random.seed(42)

PROCESSORS = ["bancosur", "mexpay", "andespsp", "cashvoucher"]
CURRENCIES = ["MXN", "COP", "CLP"]
REAL_STATES = (
    ["approved"] * 60 +
    ["declined"] * 25 +
    ["pending"] * 10 +
    ["unknown"] * 5
)

BASE_TIME = datetime(2024, 1, 15, 8, 0, 0)


def txn_id():
    return f"txn_{uuid.uuid4().hex[:8]}"


def make_transaction(
    customer_id,
    amount,
    currency,
    processor,
    real_state,
    created_at,
    notes=None
):
    return models.Transaction(
        id=txn_id(),
        customer_id=customer_id,
        amount=round(amount, 2),
        currency=currency,
        processor=processor,
        status="unknown",
        real_state=real_state,
        created_at=created_at,
        notes=notes,
    )


def generate_transactions():
    transactions = []

    # --- 1. Regular transactions (120 base) ---
    customers = [f"cust_{i:04d}" for i in range(1, 61)]
    for i in range(120):
        customer = random.choice(customers)
        amount = round(random.uniform(100, 50000), 2)
        currency = random.choice(CURRENCIES)
        processor = random.choice(PROCESSORS)
        real_state = random.choice(REAL_STATES)
        offset_hours = random.uniform(0, 72)
        created_at = BASE_TIME + timedelta(hours=offset_hours)
        transactions.append(make_transaction(
            customer, amount, currency, processor, real_state, created_at
        ))

    # --- 2. Duplicate clusters (15-20 clusters, ~3 txns each) ---
    # ~10 accidental retries (identical context, same processor)
    for i in range(10):
        customer = f"cust_dup_{i:03d}"
        amount = round(random.uniform(500, 20000), 2)
        currency = random.choice(CURRENCIES)
        processor = random.choice(PROCESSORS)
        base_time = BASE_TIME + timedelta(hours=random.uniform(0, 48))
        # 2-3 duplicates within 5 minutes
        n_dups = random.choice([2, 3])
        for j in range(n_dups):
            offset_seconds = random.randint(0, 280)
            created_at = base_time + timedelta(seconds=offset_seconds * j)
            real_state = "approved" if j == 0 else random.choice(["approved", "unknown"])
            transactions.append(make_transaction(
                customer, amount, currency, processor,
                real_state, created_at,
                notes=f"accidental_retry_cluster_{i}"
            ))

    # ~5 legitimate (same price, different items)
    for i in range(5):
        customer = f"cust_legit_{i:03d}"
        amount = round(random.uniform(1000, 5000), 2)
        currency = random.choice(CURRENCIES)
        base_time = BASE_TIME + timedelta(hours=random.uniform(0, 48))
        for j in range(2):
            offset_seconds = random.randint(60, 240)
            created_at = base_time + timedelta(seconds=offset_seconds * j)
            processor = random.choice(PROCESSORS)
            real_state = "approved"
            transactions.append(make_transaction(
                customer, amount, currency, processor,
                real_state, created_at,
                notes=f"legit_same_price_cluster_{i}"
            ))

    # --- 3. Edge cases ---

    # 5 stale transactions (>30 days old)
    for i in range(5):
        customer = f"cust_{random.randint(1, 60):04d}"
        amount = round(random.uniform(100, 10000), 2)
        currency = random.choice(CURRENCIES)
        processor = random.choice(PROCESSORS)
        real_state = random.choice(REAL_STATES)
        created_at = BASE_TIME - timedelta(days=random.randint(31, 90))
        transactions.append(make_transaction(
            customer, amount, currency, processor, real_state, created_at,
            notes="stale_transaction"
        ))

    # 3 transactions with mismatched currency context
    for i in range(3):
        customer = f"cust_{random.randint(1, 60):04d}"
        amount = round(random.uniform(100, 10000), 2)
        currency = "USD"  # unusual for this system
        processor = random.choice(PROCESSORS)
        real_state = random.choice(REAL_STATES)
        created_at = BASE_TIME + timedelta(hours=random.uniform(0, 24))
        transactions.append(make_transaction(
            customer, amount, currency, processor, real_state, created_at,
            notes="mismatched_currency"
        ))

    # 4 transactions with null customer_id
    for i in range(4):
        amount = round(random.uniform(100, 10000), 2)
        currency = random.choice(CURRENCIES)
        processor = random.choice(PROCESSORS)
        real_state = random.choice(REAL_STATES)
        created_at = BASE_TIME + timedelta(hours=random.uniform(0, 24))
        transactions.append(make_transaction(
            None, amount, currency, processor, real_state, created_at,
            notes="null_customer_id"
        ))

    return transactions


def main():
    print("Creating database tables...")
    models.Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        existing = db.query(models.Transaction).count()
        if existing > 0:
            print(f"Database already has {existing} transactions. Skipping seed.")
            return

        print("Generating transactions...")
        transactions = generate_transactions()

        db.bulk_save_objects(transactions)
        db.commit()

        count = db.query(models.Transaction).count()
        print(f"Successfully seeded {count} transactions.")

        # Print summary
        from sqlalchemy import func as sqlfunc
        states = db.query(
            models.Transaction.real_state,
            sqlfunc.count(models.Transaction.id)
        ).group_by(models.Transaction.real_state).all()
        print("\nReal state distribution:")
        for state, cnt in states:
            print(f"  {state}: {cnt}")

        processors = db.query(
            models.Transaction.processor,
            sqlfunc.count(models.Transaction.id)
        ).group_by(models.Transaction.processor).all()
        print("\nProcessor distribution:")
        for proc, cnt in processors:
            print(f"  {proc}: {cnt}")

    finally:
        db.close()


if __name__ == "__main__":
    main()
