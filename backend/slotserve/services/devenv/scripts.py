"""The scripts copied into dev environment containers now live in ``slotserve/containers/scripts.py``."""

from ...containers.scripts import (
    ACCOUNTS_SCRIPT,
    LIB_SCRIPT,
    PROFILE,
    ROOT_SETUP,
    SHARED_SETUP,
    SSH_SCRIPT,
    START_SCRIPT,
)

__all__ = ["ACCOUNTS_SCRIPT", "LIB_SCRIPT", "PROFILE", "ROOT_SETUP", "SHARED_SETUP", "SSH_SCRIPT", "START_SCRIPT"]
