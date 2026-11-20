"""Recover a Windows-reserved local PostgreSQL port without changing credentials."""

import argparse
import os
import re
import socket
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from dotenv import dotenv_values


def port_status(port):
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return "available"
        except OSError as error:
            return "reserved" if getattr(error, "winerror", None) == 10013 else "occupied"


def ensure_local_port(path, *, check=False, probe=port_status):
    values = dotenv_values(path)
    port = int(values.get("POSTGRES_PORT", "55432"))
    status = probe(port)
    if status != "reserved" or check:
        return port, port, status
    if "POSTGRES_PORT" in os.environ or "DATABASE_URL" in os.environ:
        raise ValueError("Explicit environment variables override .env; update the database port in that environment.")
    url = urlsplit(values.get("DATABASE_URL", ""))
    if url.hostname not in {"localhost", "127.0.0.1", "::1"} or url.port != port:
        raise ValueError("Cannot automatically change a non-local or mismatched DATABASE_URL.")
    replacement = next((value for value in range(15432, 15452) if probe(value) == "available"), None)
    if replacement is None:
        raise ValueError("No available PostgreSQL fallback port in 15432-15451.")
    new_url = urlunsplit(url._replace(netloc=url.netloc.rsplit(":", 1)[0] + f":{replacement}"))
    text = path.read_text(encoding="utf-8")
    for key, value in {"POSTGRES_PORT": str(replacement), "DATABASE_URL": new_url}.items():
        assignment = f"{key}={value}"
        text, count = re.subn(rf"(?m)^{key}=.*$", lambda _, line=assignment: line, text)
        if count != 1:
            raise ValueError("Expected exactly one database URL and port assignment in .env.")
    descriptor, temporary = tempfile.mkstemp(prefix=".env.port-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(text)
        os.chmod(temporary, path.stat().st_mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return port, replacement, "changed"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    old, new, status = ensure_local_port(Path(__file__).resolve().parents[1] / ".env", check=args.check)
    if status == "changed":
        print(f"Windows reserves port {old}; updated this project's POSTGRES_PORT and DATABASE_URL to {new}.")
    else:
        print(f"PostgreSQL port {old}: {status}.")
    if status == "reserved":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
