"""Canvas forum agent: a guarded I/O layer. It never calls an LLM."""
import os
import sys

from .cli import main


def _run():
    try:
        code = main()
        sys.stdout.flush()
        return code
    except BrokenPipeError:
        # Reader went away (e.g. `| head`). Silence the interpreter's flush-at-exit error too.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0


sys.exit(_run())
