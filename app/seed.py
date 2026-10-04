from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import provision_user
from app.db import SessionLocal
from app.models import User
from app.passwords import hash_password

ACCOUNTS = (
    ("admin@test.com", "admin123", "Admin", "admin"),
    ("customer@test.com", "customer123", "Customer", "customer"),
)


def seed(db: Session) -> None:
    for email, password, name, role in ACCOUNTS:
        user = db.scalar(select(User).where(User.email == email))
        if user is not None:
            if not user.pass_hash:
                user.pass_hash = hash_password(password)
                db.commit()
            continue
        _tenant, created = provision_user(db, email, name)
        created.role = role
        created.pass_hash = hash_password(password)
        db.commit()


def main() -> None:
    db = SessionLocal()
    try:
        seed(db)
    finally:
        db.close()


if __name__ == "__main__":
    main()
