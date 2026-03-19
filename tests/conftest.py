import os
import tempfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def client():
    # Use a per-test SQLite DB so tests don't require Postgres running.
    # This repo's "real" dev path is Postgres + Alembic; this keeps MVP tests lightweight.
    db_fd, db_path = tempfile.mkstemp(prefix="agent_commerce_test_", suffix=".db")
    os.close(db_fd)

    os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///{db_path}"
    os.environ["DEMO_MODE"] = "1"
    os.environ["JWT_SECRET"] = "test-jwt-secret"
    os.environ["API_KEY_PEPPER"] = "test-pepper"

    import database
    from database import Base

    engine = create_engine(
        os.environ["DATABASE_URL"],
        connect_args={"check_same_thread": False},
    )
    database.engine = engine
    database.SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    # Import models after DB is configured.
    import models  # noqa: F401

    Base.metadata.create_all(bind=engine)

    import main

    # Ensure anything in main uses the test SessionLocal.
    main.SessionLocal = database.SessionLocal

    def _override_get_db():
        db = database.SessionLocal()
        try:
            yield db
        finally:
            db.close()

    main.app.dependency_overrides[main.get_db] = _override_get_db

    with TestClient(main.app) as c:
        yield c

