import logging

log = logging.getLogger(__name__)

MAX_POSTS_PER_WINDOW = 3
WINDOW_SECONDS = 60 * 60
MAX_POSTS_PER_RUN = 1
MAX_CONSECUTIVE_FAILED_RUNS = 3


def is_stopped(config):
    return config.stopped_path.exists()


def stop_reason(config):
    try:
        return config.stopped_path.read_text().strip()
    except OSError:
        return ""


def write_stopped(config, reason):
    config.state_dir.mkdir(parents=True, exist_ok=True)
    config.stopped_path.write_text(reason + "\n")
    log.error("STOPPED written: %s", reason)


def check_can_post(memory, now, posts_this_run):
    """Return (ok, reason)."""
    if posts_this_run >= MAX_POSTS_PER_RUN:
        return False, "at most 1 post per run"
    recent = memory.count_posts_since(now - WINDOW_SECONDS)
    if recent >= MAX_POSTS_PER_WINDOW:
        return False, f"rate limit: {recent} posts in the last 60 minutes (max {MAX_POSTS_PER_WINDOW})"
    return True, "ok"


def after_run(memory, config):
    """Write state/STOPPED once the most recent runs have all failed."""
    last = memory.last_outcomes(MAX_CONSECUTIVE_FAILED_RUNS)
    if len(last) == MAX_CONSECUTIVE_FAILED_RUNS and all(o == "failed" for o in last):
        write_stopped(config, f"{MAX_CONSECUTIVE_FAILED_RUNS} consecutive failed runs")
        return True
    return False
