"""Create local configuration without overwriting existing credentials."""
import secrets
from pathlib import Path

root = Path(__file__).resolve().parents[1]
path = root / ".env"
if path.exists():
    print("Existing .env kept unchanged.")
else:
    text = (root / ".env.example").read_text(encoding="utf-8")
    text = text.replace("replace-with-generated-secret", secrets.token_urlsafe(24))
    with path.open("x", encoding="utf-8") as file:
        file.write(text)
    path.chmod(0o600)
    print("Created .env with a random database password. Model credentials remain empty.")
