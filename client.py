"""Predicate-only Decisions transport; no retries, redirects or ambient proxy/auth."""
import asyncio
import json
import math
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from agent.secret_scope import get_secret

DEFAULT_ENDPOINT = "https://api.openai.com/v1/decisions"
MAX_REQUEST_BYTES = 262144
MAX_RESPONSE_BYTES = 1048576


class DecisionsError(ValueError):
    """Safe, actionable error text: never remote bodies, URLs or secret values."""


def invalid_constant(value):
    raise ValueError("non-JSON numeric constant")


def finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite JSON number")
    return number


def endpoint(value):
    if not isinstance(value, str):
        raise DecisionsError("endpoint must be an absolute HTTP(S) URL")
    try:
        parts = urlsplit(value)
        valid = parts.scheme in {"http", "https"} and parts.hostname and parts.port != 0
    except ValueError:
        valid = False
    if not valid or parts.username or parts.password or parts.query or parts.fragment:
        raise DecisionsError("endpoint must be HTTP(S), without credentials, query or fragment")
    return value


@dataclass(frozen=True)
class Settings:
    endpoint: str
    model: str
    credential_ref: str
    timeout: float

    @classmethod
    def read(cls, ctx):
        target = endpoint(ctx.get_config("endpoint", DEFAULT_ENDPOINT))
        model = ctx.get_config("decision_model", "gpt-6-luna")
        ref = ctx.get_config("credential_ref", "OPENAI_API_KEY")
        bound = ctx.get_config("credential_endpoint", DEFAULT_ENDPOINT)
        timeout = ctx.get_config("timeout", 120)
        if not isinstance(model, str) or not model.strip():
            raise DecisionsError("model must be a nonempty string")
        if not isinstance(ref, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", ref):
            raise DecisionsError("credential_ref must name a scoped secret")
        if target != endpoint(bound):
            raise DecisionsError("credential_endpoint must exactly match endpoint; operator approval required")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise DecisionsError("timeout must be a number of seconds in (0, 600]")
        if not math.isfinite(timeout) or not 0 < timeout <= 600:
            raise DecisionsError("timeout must be a number of seconds in (0, 600]")
        return cls(target, model, ref, float(timeout))


def request_bytes(arguments, settings):
    if not isinstance(arguments, dict) or set(arguments) - {"input", "questions", "background"}:
        raise DecisionsError("expected only input, questions and optional background")
    if not isinstance(arguments.get("input"), str):
        raise DecisionsError("input must be a string")
    questions = arguments.get("questions")
    if not isinstance(questions, list) or not 1 <= len(questions) <= 64:
        raise DecisionsError("questions must contain 1..64 predicate questions")
    for question in questions:
        if not isinstance(question, dict) or set(question) != {"type", "instructions"}:
            raise DecisionsError("each question requires only type and instructions")
        if question["type"] != "predicate" or not isinstance(question["instructions"], str):
            raise DecisionsError("only predicate questions with string instructions are supported")
    if not isinstance(arguments.get("background", True), bool):
        raise DecisionsError("background must be boolean")
    characters = len(arguments["input"]) + sum(len(q["instructions"]) for q in questions)
    if characters > MAX_REQUEST_BYTES:
        raise DecisionsError(f"request exceeds {MAX_REQUEST_BYTES} bytes; shorten input/questions")
    data = json.dumps({"model": settings.model, "input": arguments["input"],
                       "questions": questions}, ensure_ascii=True).encode()
    if len(data) > MAX_REQUEST_BYTES:
        raise DecisionsError(f"request exceeds {MAX_REQUEST_BYTES} bytes; shorten input/questions")
    return data


def decode_response(data, count):
    try:
        result = json.loads(data, parse_constant=invalid_constant, parse_float=finite_float)
    except (ValueError, UnicodeError, RecursionError):
        raise DecisionsError("invalid JSON response") from None
    answers = result.get("answers") if isinstance(result, dict) else None
    if not isinstance(answers, list) or len(answers) != count:
        raise DecisionsError("response must contain one answer per question")
    for answer in answers:
        if not isinstance(answer, dict):
            raise DecisionsError("invalid predicate answer")
        probability = answer.get("probability")
        if answer.get("type") != "predicate" or "name" not in answer or answer["name"] is not None:
            raise DecisionsError("answer requires predicate type and null name")
        if isinstance(probability, bool) or not isinstance(probability, (int, float)):
            raise DecisionsError("answer probability must be numeric in [0, 1]")
        if not math.isfinite(probability) or not 0 <= probability <= 1:
            raise DecisionsError("answer probability must be numeric in [0, 1]")
    return result


async def decide(settings, body, count):
    try:
        secret = get_secret(settings.credential_ref)
    except Exception:
        raise DecisionsError("scoped credential unavailable") from None
    if not secret or not isinstance(secret, str):
        raise DecisionsError("scoped credential missing; configure credential_ref for this profile")
    try:
        async with asyncio.timeout(settings.timeout):
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=settings.timeout) as client:
                async with client.stream("POST", settings.endpoint, content=body, headers={
                    "Authorization": f"Bearer {secret}", "Content-Type": "application/json",
                    "Accept-Encoding": "identity",
                }) as response:
                    if response.status_code != 200:
                        raise DecisionsError(f"Decisions HTTP {response.status_code}; not retried")
                    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                        raise DecisionsError("compressed responses are unsupported; send identity encoding")
                    data = bytearray()
                    async for chunk in response.aiter_raw(chunk_size=65536):
                        if len(data) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise DecisionsError(f"response exceeds {MAX_RESPONSE_BYTES} bytes")
                        data.extend(chunk)
        return decode_response(data, count)
    except (TimeoutError, httpx.TimeoutException):
        raise DecisionsError("Decisions timeout; outcome uncertain, not retried") from None
    except httpx.HTTPError:
        raise DecisionsError("Decisions transport error; outcome uncertain, not retried") from None
