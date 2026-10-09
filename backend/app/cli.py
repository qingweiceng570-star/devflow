import argparse
import getpass
import json
import re
import secrets

from sqlalchemy import select

from app.core.config import get_settings
from app.core.errors import DomainError
from app.persistence.database import create_database
from app.persistence.models import User
from app.services.auth_service import create_admin


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["create-admin"])
    parser.add_argument("--username", default="admin")
    parser.add_argument(
        "--generate",
        action="store_true",
        help="Write a random initial password to a private local file",
    )
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,80}", args.username):
        parser.error("Username must contain 3-80 ASCII letters/digits/_.-")
    settings = get_settings()
    engine, factory = create_database(settings)
    try:
        with factory() as db:
            if db.scalar(select(User).where(User.username == args.username)):
                print("Administrator already exists; password was not changed.")
                return
            password = (
                secrets.token_urlsafe(20)
                if args.generate
                else getpass.getpass("New password (12+ chars): ")
            )
            create_admin(db, args.username, password)
        if args.generate:
            path = settings.storage_root / "initial-admin.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8") as file:
                json.dump({"username": args.username, "password": password}, file)
            path.chmod(0o600)
            print(f"Credentials written to {path}. Keep locally and remove after recording them.")
        else:
            print("Administrator created.")
    except DomainError as error:
        parser.error(error.message)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
