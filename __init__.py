"""Hermes standalone Decisions tools. No import-time workers or network activity."""
from .worker import Worker

QUESTION = {
    "type": "object", "additionalProperties": False,
    "properties": {"type": {"type": "string", "enum": ["predicate"]},
                   "instructions": {"type": "string"}},
    "required": ["type", "instructions"],
}


def register(ctx):
    worker = Worker(ctx)
    ctx.register_tool(
        name="decisions_submit", toolset="decisions", handler=worker.submit, is_async=True,
        schema={"description": "Submit predicate decisions; background by default. No automatic retries. "
                "Use decisions_status for the original structured response; restart loses jobs.",
                "parameters": {"type": "object", "additionalProperties": False,
                               "properties": {"input": {"type": "string"},
                                              "questions": {"type": "array", "minItems": 1,
                                                            "maxItems": 64, "items": QUESTION},
                                              "background": {"type": "boolean", "default": True}},
                               "required": ["input", "questions"]}})
    ctx.register_tool(
        name="decisions_status", toolset="decisions", handler=worker.status, is_async=True,
        schema={"description": "Retrieve a Decisions job belonging to the current home/session. "
                "Completed result preserves the decoded JSON object, not raw response bytes.",
                "parameters": {"type": "object", "additionalProperties": False,
                               "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]}})
