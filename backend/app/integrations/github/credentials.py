from app.core.config import Settings
from app.core.errors import DomainError

CREDENTIAL_REFS = {"github-default"}


def resolve_read_token(settings: Settings, credential_ref: str) -> str:
    """Map a stored credential reference to a server-side token value.

    Credential values never travel through requests or the model; an empty
    token means anonymous access, which only works for public repositories.
    """
    if credential_ref not in CREDENTIAL_REFS:
        raise DomainError("CREDENTIAL_UNKNOWN", "凭据引用未配置", 400)
    return settings.github_read_token.get_secret_value()
