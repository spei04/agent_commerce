import os
import time

from database import SessionLocal

# The worker intentionally imports the app module for shared settlement/outbox logic.
from main import _settle_simulated_payments, _process_webhooks


POLL_SECONDS = float(os.environ.get("WORKER_POLL_SECONDS", "0.5"))


def main() -> None:
    while True:
        db = SessionLocal()
        try:
            _settle_simulated_payments(db)
            _process_webhooks(db)
        finally:
            db.close()
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()

