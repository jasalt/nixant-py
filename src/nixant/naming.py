"""Instance names come exclusively from the Nix configuration."""

import re

from nixant.errors import UsageError
from nixant.models import INSTANCE_NAME


def validate_instance_name(name: str) -> str:
    if INSTANCE_NAME.fullmatch(name) is None:
        raise UsageError(
            f"invalid instance name {name!r}: use at most 63 characters of "
            "[a-z0-9-], starting with a letter and not ending with a dash"
        )
    return name


def sanitize(text: str) -> str:
    """Incus-name-safe form; callers supply a fallback when empty."""
    base = re.sub(r"[^a-z0-9]+", "-", text.lower())
    return re.sub(r"^[0-9-]+", "", base).strip("-")
