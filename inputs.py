"""Submission validation and operator-scoped, single-read UTF-8 input."""
import os
from pathlib import Path
import stat

from .client import DecisionsError, MAX_REQUEST_BYTES


def submission_input(arguments, ctx):
    if not isinstance(arguments, dict) or set(arguments) - {
        "input", "input_path", "questions", "background", "repetitions"
    }:
        raise DecisionsError("expected only input or input_path, questions, background and repetitions")
    if ("input" in arguments) == ("input_path" in arguments):
        raise DecisionsError("provide exactly one of input or input_path")
    repetitions = arguments.get("repetitions", 1)
    if type(repetitions) is not int or not 1 <= repetitions <= 20:
        raise DecisionsError("repetitions must be an integer in 1..20 (intentional paid calls)")
    if not isinstance(arguments.get("background", True), bool):
        raise DecisionsError("background must be boolean")
    text = arguments.get("input") if "input" in arguments else read_input(arguments["input_path"], ctx)
    return text, repetitions


def scoped_path(value, ctx):
    roots = ctx.get_config("input_roots", [])
    if not roots:
        raise DecisionsError("file reads disabled; operator must configure input_roots")
    if not isinstance(roots, list) or any(not isinstance(root, str) or not Path(root).is_absolute()
                                          for root in roots):
        raise DecisionsError("input_roots must be an operator-configured list of absolute directories")
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise DecisionsError("input_path must be an absolute file path within input_roots")
    path = Path(value).resolve(strict=True)
    allowed = [Path(root).resolve(strict=True) for root in roots]
    if not any(root.is_dir() and path.is_relative_to(root) for root in allowed):
        raise DecisionsError("input_path is outside operator-approved input_roots")
    return path


def read_input(value, ctx):
    try:
        path = scoped_path(value, ctx)
        if not hasattr(os, "O_NOFOLLOW"):
            raise DecisionsError("scoped file reads require POSIX no-follow support")
        # Nonblocking prevents special files from hanging before fstat can reject them.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise DecisionsError("input_path must be a regular UTF-8 file")
            if info.st_size > MAX_REQUEST_BYTES:
                raise DecisionsError(f"input file exceeds {MAX_REQUEST_BYTES} bytes; shorten input")
            with os.fdopen(fd, "rb", closefd=False) as source:
                data = source.read(MAX_REQUEST_BYTES + 1)
        finally:
            os.close(fd)
        if len(data) > MAX_REQUEST_BYTES:
            raise DecisionsError(f"input file exceeds {MAX_REQUEST_BYTES} bytes; shorten input")
        return data.decode("utf-8")
    except UnicodeError:
        raise DecisionsError("input_path must contain valid UTF-8") from None
    except (OSError, RuntimeError, ValueError) as error:
        if isinstance(error, DecisionsError):
            raise
        raise DecisionsError("input_path unavailable; check path, input_roots and file permissions") from None
