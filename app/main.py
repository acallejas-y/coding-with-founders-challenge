from fastapi import FastAPI
from contextlib import asynccontextmanager
from app.database import engine, SessionLocal
from app import models


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create tables
    models.Base.metadata.create_all(bind=engine)
    # Seed data if empty
    db = SessionLocal()
    try:
        count = db.query(models.Transaction).count()
        if count == 0:
            import subprocess
            import sys
            subprocess.run([sys.executable, "scripts/generate_test_data.py"], check=False)
    finally:
        db.close()
    yield


app = FastAPI(
    title="VidaRx Transaction State Recovery API",
    description="Recovers payment transactions that timed out and landed in 'unknown' state",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
def health_check():
    return {"status": "ok", "service": "vidarx-recovery-api"}


# Routers will be imported here after creation
from app.routers import transactions, bulk  # noqa: E402
app.include_router(transactions.router, prefix="/api/v1/transactions", tags=["transactions"])
app.include_router(bulk.router, prefix="/api/v1/transactions", tags=["bulk"])
