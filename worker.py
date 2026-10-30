"""One private continuing loop, bounded volatile jobs; host supervises each task."""
import asyncio
from collections import OrderedDict
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from copy import deepcopy
import json
import threading
from uuid import uuid4

from agent.memory_provider import spawn_context_thread
from gateway.session_context import async_delivery_supported, get_session_env
from hermes_constants import get_hermes_home

from .client import DecisionsError, Settings, decide, request_bytes
from .inputs import submission_input
from .repetitions import Repetitions

CAPACITY = 4
RETENTION = 32
FOREGROUND_WAIT_LIMIT = 240  # Released native running-loop bridge stops waiting at 300s.


@dataclass(frozen=True)
class Origin:
    owner: tuple
    session_key: str
    notify: bool

    @classmethod
    def capture(cls):
        key = get_session_env("HERMES_SESSION_KEY") or ""
        session_id = get_session_env("HERMES_SESSION_ID") or ""
        if not (key or session_id):
            raise DecisionsError("a captured session key or session id is required")
        return cls((str(get_hermes_home().resolve()), "key" if key else "id", key or session_id), key,
                   async_delivery_supported())


class Worker:
    def __init__(self, ctx):
        self.ctx = ctx
        self.loop = asyncio.new_event_loop()
        self.jobs = OrderedDict()
        self.tasks = set()
        self.closed = False
        self.gate = threading.Lock()
        self.thread = spawn_context_thread(self.loop.run_forever, name="decisions-worker")
        self.thread.start()
        ctx.on_unload(self.close)

    def call(self, coroutine):
        with self.gate:
            if self.closed:
                coroutine.close()
                raise DecisionsError("Decisions worker unloaded")
            future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        try:
            return future.result(timeout=5)
        except FutureTimeout:
            future.cancel()
            raise DecisionsError("Decisions worker unavailable; submission outcome uncertain") from None

    async def submit(self, args):
        try:
            origin = Origin.capture()
            settings = Settings.read(self.ctx)
            text, repetitions = submission_input(args, self.ctx)
            body = request_bytes({**args, "input": text}, settings)
            background = args.get("background", True)
            job_id, completed = self.call(self.admit(origin, settings, body, len(args["questions"]), repetitions))
            if background:
                return json.dumps({"job_id": job_id, "state": "running"})
            # Same task and transport; wait outside the continuing loop.
            completed.result(timeout=min(repetitions * settings.timeout + 5, FOREGROUND_WAIT_LIMIT))
            return json.dumps(self.call(self.lookup(job_id, origin)), allow_nan=False)
        except DecisionsError as error:
            return json.dumps({"error": str(error)})
        except FutureTimeout:
            return json.dumps({"error": "foreground wait timeout; outcome uncertain, use decisions_status",
                               "job_id": job_id})

    async def admit(self, origin, settings, body, count, repetitions):
        if len(self.tasks) >= CAPACITY:
            raise DecisionsError(f"Decisions capacity {CAPACITY} active requests reached; wait before submitting")
        job_id = uuid4().hex
        self.jobs[job_id] = {"owner": origin.owner, "job_id": job_id, "state": "running"}
        task = self.ctx.spawn_task(self.run(job_id, origin, settings, body, count, repetitions), name="decisions-request")
        self.tasks.add(task)
        # Newer than the task lease: unload drains/cancels on its owning loop FIRST.
        cleanup = self.ctx.on_unload(lambda: None if task.done() else self.close())
        task.add_done_callback(lambda done: self.tasks.discard(done))
        task.add_done_callback(lambda done: cleanup.dispose())
        from concurrent.futures import Future
        completed = Future()
        task.add_done_callback(lambda done: completed.set_result(None))
        return job_id, completed

    async def run(self, job_id, origin, settings, body, count, repetitions):
        record = self.jobs[job_id]
        try:
            if repetitions == 1:
                record["result"] = await decide(settings, body, count)
                record["state"] = "completed"
            else:
                group = Repetitions(repetitions, count)
                record["result"] = group.result
                record["state"] = await group.run(settings, body, count)
        except asyncio.CancelledError:
            record.update(state="cancelled", error="worker unloaded; outcome uncertain, not retried")
        except DecisionsError as error:
            record.update(state="failed", error=str(error))
        except Exception:
            record.update(state="failed", error="internal Decisions error; outcome uncertain, not retried")
        record["notification_accepted"] = False
        if origin.notify and record["state"] != "cancelled":
            try:
                record["notification_accepted"] = bool(self.ctx.inject_message(
                    f"Decisions {job_id}: {record['state']}. Retrieve structured output with decisions_status.",
                    session_key=origin.session_key or None))
            except Exception:
                pass  # Best effort only; result remains retrievable.
        self.jobs.move_to_end(job_id)  # Retain by completion order, not admission order.
        self.trim()

    def trim(self):
        completed = [key for key, value in self.jobs.items() if value["state"] != "running"]
        for key in completed[:-RETENTION]:
            del self.jobs[key]

    async def lookup(self, job_id, origin):
        record = self.jobs.get(job_id)
        if record is None or record["owner"] != origin.owner:
            raise DecisionsError("job not found in this home/session (expired or unknown)")
        return deepcopy({key: value for key, value in record.items() if key != "owner"})

    async def status(self, args):
        try:
            if not isinstance(args, dict) or set(args) != {"job_id"} or not isinstance(args["job_id"], str):
                raise DecisionsError("status requires only string job_id")
            return json.dumps(self.call(self.lookup(args["job_id"], Origin.capture())), allow_nan=False)
        except DecisionsError as error:
            return json.dumps({"error": str(error)})

    async def drain(self):
        pending = list(self.tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self.jobs.clear()

    def close(self):
        with self.gate:
            if self.closed:
                return
            self.closed = True
        asyncio.run_coroutine_threadsafe(self.drain(), self.loop).result(timeout=5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        if self.thread.is_alive():
            raise RuntimeError("Decisions worker did not stop")
        self.loop.close()
