import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone

from . import limits
from .canvas import CanvasClient, CanvasError
from .compose import add_signature
from .config import Config, ConfigError, get_token, setup_logging
from .memory import Memory
from .pipeline import fetch_new, run_submit

log = logging.getLogger(__name__)


def _emit(out, obj):
    out.write(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")
    out.flush()


def main(argv=None, *, env=None, stdin=None, stdout=None, clock=time.time, session=None):
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    parser = argparse.ArgumentParser(prog="python3 -m agent")
    sub = parser.add_subparsers(dest="cmd", required=True)
    fetch = sub.add_parser("fetch", help="print up to 25 newest unseen forum items as JSON")
    fetch.add_argument("--peek", action="store_true", help="print the same JSON; record nothing")
    sub.add_parser("submit", help="read a decision JSON on stdin; post if it says so")
    sub.add_parser("status", help="print counters, recent runs, STOPPED state")
    sub.add_parser("drafts", help="list the last 20 decisions with their message text and reason")
    args = parser.parse_args(argv)

    setup_logging()
    try:
        config = Config.from_env(env)
    except ConfigError as exc:
        _emit(stdout, {"status": "config_error", "detail": str(exc)})
        return 2

    if args.cmd == "status":
        memory = Memory(config.db_path, clock)
        _emit(stdout, {
            "stopped": limits.is_stopped(config), "stop_reason": limits.stop_reason(config),
            "dry_run": config.dry_run, "fault": config.fault or None,
            "posts_last_60_min": memory.count_posts_since(clock() - limits.WINDOW_SECONDS),
            "pending_actions": len(memory.actions_with_status("pending")),
            "counters": memory.counters(), "recent_runs": memory.recent_runs(10)})
        return 0

    if args.cmd == "drafts":
        memory = Memory(config.db_path, clock)
        try:
            stdout.write(format_drafts(memory.recent_decisions(20)))
        finally:
            memory.close()
        return 0

    if limits.is_stopped(config):
        _emit(stdout, {"status": "stopped", "detail": limits.stop_reason(config)})
        return 3

    memory = Memory(config.db_path, clock)
    peek = args.cmd == "fetch" and args.peek
    if not peek:
        memory.abort_stale_runs()

    def get_client():
        config.require_ids()
        return CanvasClient(config, get_token(env), session=session)

    try:
        if args.cmd == "fetch":
            if peek:
                return _peek(get_client, memory, stdout)
            return _fetch(config, memory, get_client, stdout)
        result, code = run_submit(config, memory, get_client, stdin.read(), clock)
        _emit(stdout, result)
        return code
    finally:
        if not peek:
            limits.after_run(memory, config)
        memory.close()


def format_drafts(decisions):
    """Human-readable review of what the agent decided, newest first."""
    if not decisions:
        return "no decisions recorded yet\n"
    blocks = []
    for d in decisions:
        when = datetime.fromtimestamp(d["ts"], timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        target = f" -> entry {d['target_entry_id']}" if d["target_entry_id"] else ""
        lines = [f"#{d['id']}  {when}  {d['action']}{target}  [{d['outcome']}]"]
        message = d["message"]
        if message and d["action"] != "none":
            if d["outcome"] in ("dry_run", "posted"):
                message = add_signature(message)  # the tool appends this when it posts
            lines.append("  message:")
            lines += [("    " + line).rstrip() for line in message.splitlines()]
        else:
            lines.append("  message: (none)")
        lines.append(f"  reason: {d['reason'] or '(none)'}")
        if d["outcome"] == "rejected" and d["detail"]:
            lines.append(f"  problems: {d['detail']}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


def _peek(get_client, memory, stdout):
    try:
        data = fetch_new(get_client(), memory)
    except (CanvasError, ConfigError) as exc:
        _emit(stdout, {"status": "failed", "detail": str(exc)})
        return 1
    try:
        _emit(stdout, data)
    except BrokenPipeError:
        pass
    return 0


def _fetch(config, memory, get_client, stdout):
    rid = memory.start_run("fetch")
    try:
        data = fetch_new(get_client(), memory)
    except (CanvasError, ConfigError) as exc:
        memory.finish_run(rid, "failed", str(exc)[:200])
        _emit(stdout, {"status": "failed", "detail": str(exc)})
        return 1
    try:
        _emit(stdout, data)  # print first: if the reader is gone, nothing is recorded as offered
    except BrokenPipeError:
        memory.finish_run(rid, "aborted", "reader closed the pipe")
        return 0
    memory.record_offered(rid, data["items"])
    memory.incr("fetches")
    memory.incr("items_offered", data["count"])
    memory.finish_run(rid, "ok", f"{data['count']} offered of {data['total_unseen']} unseen")
    return 0
