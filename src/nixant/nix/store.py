"""Nix store path shapes accepted from Nix output before they reach argv."""

import re
from typing import TypeGuard

_STORE_PATH = re.compile(r"/nix/store/[a-z0-9]{32}-[^/\s]+")


def is_store_path(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and _STORE_PATH.fullmatch(value) is not None


def is_drv_path(value: object) -> TypeGuard[str]:
    return is_store_path(value) and value.endswith(".drv")
