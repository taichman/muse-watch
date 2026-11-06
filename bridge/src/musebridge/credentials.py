"""The paired device's credentials: rotate them when due and find the Muse VM.

The bridge doesn't run ``musegadget run`` (which would let Muse execute shell
commands on this Mac), so it rotates the device token itself, the same way
``musegadget.service`` does.
"""

from __future__ import annotations

import asyncio
import logging
import time

from musegadget import config, identity, muse_api
from musegadget.service import TOKEN_REFRESH_AGE_S

log = logging.getLogger(__name__)


class NotPaired(Exception):
    pass


async def _refresh(pairing: dict, force: bool) -> dict:
    age = time.time() - pairing.get("access_token_saved_at", 0)
    if not force and age < TOKEN_REFRESH_AGE_S:
        return pairing
    tokens, status = await asyncio.to_thread(
        muse_api.refresh_device_token,
        pairing["refresh_token"], identity.load_or_create().node_id,
        muse_api.api_root(pairing.get("api_url_v2", "")),
        config.sdk_token(),
    )
    if tokens:
        pairing = {
            **pairing,
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
            "access_token_saved_at": int(time.time()),
        }
        config.save_json(config.PAIRING_FILE, pairing)
        log.info("device token rotated")
        return pairing
    if status == 401:
        config.delete_json(config.PAIRING_FILE)
        raise NotPaired("the pairing was revoked; run `musebridge pair` again")
    if force:
        raise ConnectionError("device token refresh failed")
    return pairing  # transient failure: keep using the current token


async def find_vm(vm_id: str | None = None) -> tuple[dict, dict]:
    """Return ``(pairing, vm)`` for the requested VM, or the account's default one."""
    pairing = config.load_json(config.PAIRING_FILE)
    if not pairing or not pairing.get("access_token"):
        raise NotPaired("not paired; run `musebridge pair` first")
    pairing = await _refresh(pairing, force=False)
    for attempt in range(2):
        vms, status = await asyncio.to_thread(
            muse_api.fetch_vms_with_status, pairing["access_token"],
            muse_api.api_root(pairing.get("api_url_v2", "")),
        )
        if status == 401 and attempt == 0:
            pairing = await _refresh(pairing, force=True)
            continue
        if status != 200 or not vms:
            raise ConnectionError(f"Muse VM lookup failed (HTTP {status})")
        break
    if vm_id:
        matches = [vm for vm in vms if vm["vm_id"] == vm_id]
        if len(matches) != 1:
            raise ValueError(f"no leased Muse VM has id {vm_id}")
        return pairing, matches[0]
    return pairing, next((vm for vm in vms if vm["is_default"]), vms[0])
