import os
from typing import Optional

try:
    import keyring  # type: ignore[reportMissingImports]
except ImportError:  # pragma: no cover - optional at runtime
    keyring = None


SERVICE_NAME = "atm-app"


def get_secret(name: str, default: Optional[str] = None, required: bool = False) -> str:
    """
    Resolve a secret from keychain first, then environment fallback.
    """
    value = None
    if keyring is not None:
        try:
            value = keyring.get_password(SERVICE_NAME, name)
        except Exception:
            value = None

    if not value:
        env_val = os.environ.get(name, "")
        value = env_val.strip() if isinstance(env_val, str) else env_val

    if required and not value:
        raise ValueError(
            f"Missing required secret: {name}. Set it in keychain (preferred) "
            f"or environment."
        )
    return value if value is not None else (default or "")


def set_secret(name: str, value: str) -> None:
    if keyring is None:
        raise RuntimeError("keyring is not installed. Run: pip install -r requirements.txt")
    keyring.set_password(SERVICE_NAME, name, value)


def delete_secret(name: str) -> None:
    if keyring is None:
        raise RuntimeError("keyring is not installed. Run: pip install -r requirements.txt")
    try:
        keyring.delete_password(SERVICE_NAME, name)
    except Exception:
        # Keep delete idempotent for setup scripts.
        pass
