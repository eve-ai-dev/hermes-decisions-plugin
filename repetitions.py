"""Sequential intentional requests and compact probability-only group progress."""
import asyncio

from .client import DecisionsError, decide


class Repetitions:
    def __init__(self, requested, count):
        self.result = {"requested": requested, "completed": 0, "success": 0, "failed": 0,
                       "not_started": requested, "attempts": [],
                       "questions": [{"probabilities": [], "mean": None, "min": None, "max": None}
                                     for _ in range(count)]}

    def record_success(self, response):
        for summary, answer in zip(self.result["questions"], response["answers"]):
            values = summary["probabilities"]
            values.append(answer["probability"])
            summary.update(mean=sum(values) / len(values), min=min(values), max=max(values))
        self.result["success"] += 1

    async def run(self, settings, body, count):
        for _ in range(self.result["requested"]):
            attempt = {"state": "running"}
            self.result["attempts"].append(attempt)
            self.result["not_started"] -= 1
            try:
                self.record_success(await decide(settings, body, count))
                attempt["state"] = "completed"
            except asyncio.CancelledError:
                attempt.update(state="cancelled", error="worker unloaded; outcome uncertain, not retried")
                raise
            except DecisionsError as error:
                attempt.update(state="failed", error=str(error))
                self.result["failed"] += 1
            except Exception:
                attempt.update(state="failed", error="internal Decisions error; outcome uncertain, not retried")
                self.result["failed"] += 1
                self.result["completed"] += 1
                return "failed"
            self.result["completed"] += 1
        if not self.result["success"]:
            return "failed"
        return "partial" if self.result["failed"] else "completed"
