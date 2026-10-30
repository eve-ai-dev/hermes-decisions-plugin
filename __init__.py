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
        name="decisions_submit", toolset="decisions", emoji="⚖️", handler=worker.submit, is_async=True,
        schema={"description": "Submit predicate decisions; background by default. No automatic retries. "
                "Use input OR input_path within operator input_roots. Repetitions makes 1..20 paid sequential "
                "calls for stability, not calibration. Use decisions_status; restart loses jobs.",
                "parameters": {"type": "object", "additionalProperties": False,
                               "properties": {"input": {"type": "string"},
                                              "input_path": {"type": "string", "description": "Absolute UTF-8 file in operator input_roots."},
                                              "repetitions": {"type": "integer", "minimum": 1, "maximum": 20, "default": 1},
                                              "questions": {"type": "array", "minItems": 1,
                                                            "maxItems": 64, "items": QUESTION},
                                              "background": {"type": "boolean", "default": True}},
                               "required": ["questions"],
                               "oneOf": [{"required": ["input"], "not": {"required": ["input_path"]}},
                                         {"required": ["input_path"], "not": {"required": ["input"]}}]}})
    ctx.register_tool(
        name="decisions_status", toolset="decisions", emoji="⚖️", handler=worker.status, is_async=True,
        schema={"description": "Retrieve a Decisions job belonging to the current home/session. "
                "Single-call result preserves decoded JSON; repeated calls return compact probabilities/counts.",
                "parameters": {"type": "object", "additionalProperties": False,
                               "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]}})
