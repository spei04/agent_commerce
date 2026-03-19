import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

# Iteration 2 default: Postgres (run `docker compose up -d`).
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg2://agent:agent@localhost:5432/agent_commerce",
)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
