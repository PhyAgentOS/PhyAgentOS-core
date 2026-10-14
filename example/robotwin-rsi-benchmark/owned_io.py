"""Drain an already dispatched blocking operation before releasing cleanup authority."""

import asyncio


async def owned_call(function, *args):
    pending = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(pending)
    except asyncio.CancelledError:
        # Cancelling to_thread does not stop its native thread. A late start/action
        # must finish (possibly with an unknown result) before owned teardown runs.
        # Repeated cancellation must not detach that operation either.
        while not pending.done():
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if pending.done() and not pending.cancelled():
            pending.exception()  # Retrieve any exception without replacing cancellation.
        raise
