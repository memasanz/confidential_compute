"""Durable claims fence controller commits; external create/delete are idempotent.

Activation is different: if its claim expires, retire rather than retry it.
SDK/PKI failures retain reservations. Deletion alone confirms and frees a slot.
"""

import asyncio
import copy
import logging
import time

from errors import WorkerFailed

log = logging.getLogger(__name__)


class Controller:
    def __init__(self, store, azure, transport):
        self.store, self.azure, self.transport = store, azure, transport

    async def tick(self):
        began = time.monotonic()
        await asyncio.to_thread(self.store.sweep)
        observed = await asyncio.to_thread(self.azure.inventory)
        inventory_done = time.monotonic()
        rows = await asyncio.to_thread(self.store.view)
        known = {v["id"]: v for k, v in rows.items() if k.startswith("w:")}
        for wid, lifetime in observed.items():
            await asyncio.to_thread(self.store.reconcile_orphan, wid, lifetime)
        checked = [(wid, w) for wid, w in known.items() if w["state"] in ("active", "ready")]
        results = await asyncio.gather(*(asyncio.to_thread(self.azure.healthy, w) for _, w in checked),
                                       return_exceptions=True)
        for (wid, _), healthy in zip(checked, results):
            if isinstance(healthy, BaseException):
                raise healthy
            if not healthy:
                await asyncio.to_thread(self.store.fail_worker, wid)
        health_done = time.monotonic()
        await asyncio.to_thread(self.store.assign)
        rows = await asyncio.to_thread(self.store.view)
        claimed = []
        for key, value in rows.items():
            if not key.startswith("w:"):
                continue
            worker = await asyncio.to_thread(self.store.claim, value["id"])
            if worker:
                claimed.append(worker)
        # Independent workers: a slow delete must not delay another worker's provisioning.
        await asyncio.gather(*(self.work(worker) for worker in claimed))
        # Reserve only after reconciliation; no uncounted management creation.
        for _ in range(self.store.limits.desired_ready):
            worker = await asyncio.to_thread(self.store.reserve)
            if worker is None:
                break
        log.info("Pool tick took %.1fs (inventory %.1fs, health %.1fs, %d operations)",
                 time.monotonic() - began, inventory_done - began, health_done - inventory_done, len(claimed))

    async def work(self, worker):
        started = time.monotonic()
        try:
            await self.work_inner(worker)
        finally:
            log.info("Pool %s operation took %.1fs", worker["state"], time.monotonic() - started)

    async def work_inner(self, worker):
        wid, op, state = worker["id"], worker["claim"], worker["state"]
        created_confirmed = False
        try:
            async with asyncio.timeout(self.store.limits.operation):
                if state == "provisioning":
                    routing = await asyncio.to_thread(self.azure.ensure, worker)
                    if routing is None:
                        await asyncio.to_thread(self.store.complete, wid, op, "provisioning")
                        return
                    candidate = copy.deepcopy(worker)
                    candidate["routing"] = routing
                    created_confirmed = True
                    await self.transport.ready(candidate)
                    await asyncio.to_thread(self.store.complete, wid, op, "ready", routing)
                elif state == "activating":
                    await self.transport.activate(worker)
                    await asyncio.to_thread(self.store.complete, wid, op, "active")
                elif state == "deleting":
                    confirmed = await asyncio.to_thread(self.azure.destroy, worker)
                    await asyncio.to_thread(self.store.complete, wid, op,
                                            "deleted" if confirmed else "deleting")
        except Exception as exc:
            detail = str(exc) if str(exc).startswith("External boundary failed (") else ""
            log.warning("Pool %s operation failed (%s%s); reservation retained", state,
                        type(exc).__name__, " " + detail if detail else "")
            if (state == "provisioning" and created_confirmed and not isinstance(exc, WorkerFailed)
                    and self.store.clock() < worker["created"] + self.store.limits.provisioning_age // 2):
                # A started worker may need time before its TLS endpoint answers; ready never activates
                # a session, so retry it on the next tick within a bounded grace instead of recycling.
                try:
                    await asyncio.to_thread(self.store.complete, wid, op, "provisioning")
                except Exception:
                    pass
                return
            if state == "provisioning" and not created_confirmed and not isinstance(exc, WorkerFailed):
                # ensure() is idempotent: release the claim so the next tick retries instead of
                # waiting for the claim to expire (a child process that dies costs minutes otherwise).
                try:
                    await asyncio.to_thread(self.store.complete, wid, op, "provisioning")
                except Exception:
                    pass
                return
            if state == "activating" or (state == "provisioning" and (
                    created_confirmed or isinstance(exc, WorkerFailed))):
                await asyncio.to_thread(self.store.fail_worker, wid, op)
            # Uncertain create cannot be converted into "absent => deleted".
            # Keep provisioning until a retry observes the deterministic resource.
            # Uncertain deletion keeps claim/capacity until expiry and reconciliation.
            # No exception strings: SDK failures can contain deployment secrets.

    async def run(self, interval):
        if interval <= 0:
            raise ValueError("Explicit positive reconciliation interval required")
        while True:
            try:
                await self.tick()
            except Exception as exc:
                # State/inventory outage fails closed: no provisioning on incomplete inventory.
                log.warning("Pool reconciliation unavailable (%s)", type(exc).__name__)
            await asyncio.sleep(interval)
