"""fetch and submit. Deterministic plumbing only: no LLM, no token output."""
import hashlib
import json
import logging
import time

from . import control, limits
from .config import get_token
from .canvas import CanvasError, UnknownOutcome
from .compose import add_signature, strip_signature, to_html
from .textutil import html_to_text, message_hash, text_hash
from .validate import validate_message

log = logging.getLogger(__name__)

MAX_ITEM_CHARS = 1500
MAX_INPUT_BYTES = 20000
ACTIONS = ("none", "reply", "new_thread")
RECOVERY_FETCHES = 3  # lookups after an unknown POST outcome before concluding the post is absent
RECOVERY_GAP_SECONDS = 5  # Canvas may not have indexed the new post yet; wait before every lookup
MAX_REJECTED_ATTEMPTS = 2  # per cycle; after that the cycle ends with action none


class InvalidDecision(Exception):
    pass


def _norm(raw, kind, parent_id=None):
    parent = raw.get("parent_id", parent_id)
    return {
        "id": str(raw["id"]),
        "kind": kind,
        "parent_id": str(parent) if parent is not None else None,
        "author_id": str(raw.get("user_id")),
        "author": str(raw.get("user_name") or "unknown")[:100],
        "created_at": raw.get("created_at"),
        "text": html_to_text(raw.get("message")),
    }


def collect_items(client):
    """Every live entry and reply in the topic, normalised."""
    items = []
    for entry in client.list_entries():
        if entry.get("deleted"):
            continue
        items.append(_norm(entry, "entry"))
        if entry.get("recent_replies") or entry.get("has_more_replies"):
            for reply in client.list_replies(entry["id"]):
                if not reply.get("deleted"):
                    items.append(_norm(reply, "reply", parent_id=entry["id"]))
    return items


MAX_OFFER = 25
SNIPPET_CHARS = 120


def _snippet(text):
    text = " ".join(text.split())
    return text if len(text) <= SNIPPET_CHARS else text[:SNIPPET_CHARS - 1] + "…"


def _newest_first(item):
    return (item["created_at"] or "", int(item["id"]))


def fetch_new(client, memory):
    """Up to MAX_OFFER of the newest unseen items. Records nothing."""
    me = str(client.get_self()["id"])
    items = collect_items(client)
    by_id = {i["id"]: i for i in items}
    unseen = [i for i in items if i["author_id"] != me and not memory.is_seen(i["id"])]
    unseen.sort(key=_newest_first, reverse=True)
    out = []
    for item in unseen[:MAX_OFFER]:
        text = item["text"]
        parent = by_id.get(item["parent_id"]) if item["parent_id"] else None
        out.append({
            "id": item["id"], "kind": item["kind"], "parent_id": item["parent_id"],
            "root_id": item["parent_id"] or item["id"],
            "parent_snippet": _snippet(parent["text"]) if parent else None,
            "author_id": item["author_id"], "author": item["author"],
            "created_at": item["created_at"],
            "text": text[:MAX_ITEM_CHARS], "truncated": len(text) > MAX_ITEM_CHARS,
            "untrusted": True,
        })
    result = {"me": me, "count": len(out), "total_unseen": len(unseen), "items": out}
    if len(unseen) > len(out):
        result["backlog_note"] = (f"{len(unseen) - len(out)} older unseen items not shown; "
                                  "they are offered by later fetches once these are decided.")
    return result


def parse_decision(raw):
    if len(raw.encode("utf-8", "replace")) > MAX_INPUT_BYTES:
        raise InvalidDecision("input too large")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise InvalidDecision("stdin is not valid JSON") from exc
    if not isinstance(data, dict):
        raise InvalidDecision("expected a JSON object")
    action = data.get("action")
    if action not in ACTIONS:
        raise InvalidDecision(f"action must be one of {', '.join(ACTIONS)}")
    reason = data.get("reason") or ""
    if not isinstance(reason, str):
        raise InvalidDecision("reason must be a string")
    target, message = data.get("target_entry_id"), data.get("message")
    if action != "none":
        if not isinstance(message, str) or not message.strip():
            raise InvalidDecision("message is required")
    if action == "reply":
        if isinstance(target, int) and not isinstance(target, bool):
            target = str(target)
        if not isinstance(target, str) or not target.isdigit():
            raise InvalidDecision("reply needs a numeric target_entry_id")
    elif action == "new_thread":
        target = None
    else:
        target = str(target) if target is not None else None
    return {"action": action, "target": target, "message": message, "reason": reason[:500]}


def _find_new_posts(items, me, mhash, target, before_ids):
    return [i for i in items
            if i["id"] not in before_ids and i["author_id"] == me
            and text_hash(i["text"]) == mhash and i["parent_id"] == target]


def _find_after_unknown(client, me, mhash, target, before_ids):
    """Look for my post after an unknown POST outcome, giving Canvas time to index it.

    Waits before each of up to RECOVERY_FETCHES lookups. CanvasError propagates.
    """
    matches = []
    for n in range(RECOVERY_FETCHES):
        time.sleep(RECOVERY_GAP_SECONDS)
        matches = _find_new_posts(collect_items(client), me, mhash, target, before_ids)
        if matches:
            break
        log.warning("my post not visible yet (lookup %d/%d)", n + 1, RECOVERY_FETCHES)
    return matches


def _result(status, code, **extra):
    return {"status": status, **extra}, code


def run_submit(config, memory, get_client, raw, clock):
    try:
        d = parse_decision(raw)
    except InvalidDecision as exc:
        rid = memory.start_run("submit", "invalid", None, None)
        memory.finish_run(rid, "rejected", str(exc))
        memory.incr("rejected")
        return _result("invalid_input", 2, detail=str(exc))
    if d["action"] != "none" and memory.rejected_streak() >= MAX_REJECTED_ATTEMPTS:
        return _give_up(memory, d)
    rid = memory.start_run("submit", d["action"], d["target"], d["reason"], d["message"])
    memory.incr("submits")
    if d["action"] == "none":
        memory.finish_run(rid, "none")
        memory.mark_offered_seen()
        return _result("none", 0)
    try:
        result, code, outcome = _post_flow(config, memory, get_client, d, clock)
    except Exception as exc:  # any surprise is a failed run, never a post
        log.exception("submit failed")
        result, code, outcome = {"status": "failed", "detail": type(exc).__name__}, 1, "failed"
    detail = result.get("detail") or "; ".join(result.get("problems", [])) or result.get("status")
    memory.finish_run(rid, outcome, detail)
    if outcome != "rejected":  # a rejected attempt leaves the cycle open for a corrected retry
        memory.mark_offered_seen()
    return result, code


def _give_up(memory, d):
    """Too many rejected attempts this cycle: the submission is dropped and the cycle ends with none."""
    why = (f"{MAX_REJECTED_ATTEMPTS} rejected attempts this cycle; ending with action none "
           f"(dropped {d['action']})")
    rid = memory.start_run("submit", "none", None, why)
    memory.incr("submits")
    memory.incr("cycles_given_up")
    memory.finish_run(rid, "none", why)
    memory.mark_offered_seen()
    return _result("none", 0, detail=why)


def _post_flow(config, memory, get_client, d, clock):
    client = get_client()
    action, target, message = d["action"], d["target"], d["message"]
    final_text = add_signature(message)
    html = to_html(final_text)
    mhash = message_hash(html)

    def reject(problems):
        memory.incr("rejected")
        return {"status": "rejected", "problems": problems}, 4, "rejected"

    def refuse(counter, detail):
        memory.incr(counter)
        return {"status": "refused", "detail": detail}, 4, "refused"

    ctl = control.check(client)
    if ctl.error:
        return {"status": "failed", "detail": ctl.reason}, 1, "failed"
    if not ctl.ok:
        return refuse("control_blocked", ctl.reason)

    me = str(client.get_self()["id"])
    items = collect_items(client)
    _reconcile_pending(memory, items, me, clock)

    ok, why = limits.check_can_post(memory, clock(), posts_this_run=0)
    if not ok:
        return refuse("limit_blocked", why)

    problems = []
    if action == "reply":
        tgt = next((i for i in items if i["id"] == target), None)
        if tgt is None:
            problems.append("target entry does not exist")
        elif tgt["kind"] != "entry":
            problems.append("can only reply to top-level entries")
        elif tgt["author_id"] == me:
            problems.append("will not reply to my own post")
    past = [strip_signature(i["text"]) for i in items if i["author_id"] == me]
    past += [strip_signature(a["message"]) for a in memory.actions_with_status("posted")]
    names = memory.known_author_names() | {i["author"] for i in items if i["author_id"] != me}
    try:
        token = get_token()
    except Exception:
        token = ""
    problems += validate_message(message, token=token, past_posts=past, author_names=names,
                                 banned_phrases_path=config.banned_phrases_path)
    if problems:
        return reject(problems)

    key = hashlib.sha256(f"{action}|{target}|{mhash}".encode()).hexdigest()
    existing = memory.get_action(key)
    if existing and existing["status"] == "posted":
        return reject(["identical action was already posted"])

    if config.dry_run:
        memory.incr("dry_runs")
        return {"status": "dry_run", "detail": "DRY_RUN=1: nothing posted",
                "would_post": {"action": action, "target_entry_id": target, "message": final_text,
                               "html": html}}, 0, "dry_run"

    if existing:
        aid = existing["id"]
        memory.set_action(aid, "pending", detail="retry of failed intent")
    else:
        aid = memory.create_action(key, action, target, final_text, mhash)  # BEFORE the POST

    before_ids = {i["id"] for i in items}

    def send():
        return client.post_reply(target, html) if action == "reply" else client.post_entry(html)

    matches = None
    for attempt in range(2):
        try:
            send()
            break
        except UnknownOutcome as exc:
            log.warning("POST outcome unknown (%s); looking for my post", exc)
            try:
                matches = _find_after_unknown(client, me, mhash, target, before_ids)
            except CanvasError:
                memory.set_action(aid, "pending", detail="outcome unknown; lookup failed")
                return {"status": "failed", "detail": "outcome unknown and lookup failed; left pending"}, 1, "failed"
            if matches:
                break
            if attempt == 1:
                memory.set_action(aid, "failed", detail="no post found after unknown outcome and one retry")
                return {"status": "failed", "detail": "post not found after retry"}, 1, "failed"
            fresh = control.check(client)
            if not fresh.ok:
                memory.set_action(aid, "failed", detail="control changed before retry")
                return {"status": "refused", "detail": fresh.reason}, 4, "refused"
        except CanvasError as exc:
            memory.set_action(aid, "failed", detail=str(exc)[:200])
            return {"status": "failed", "detail": str(exc)}, 1, "failed"

    try:
        matches = _find_new_posts(collect_items(client), me, mhash, target, before_ids)
    except CanvasError:
        matches = []
    if not matches:
        memory.set_action(aid, "pending", detail="posted but not verified")
        return {"status": "failed", "detail": "could not verify post; left pending"}, 1, "failed"
    extra = len(matches) - 1
    if extra:
        memory.incr("duplicates_detected", extra)
        log.error("%d duplicate post(s) detected for the same message", extra)
    memory.set_action(aid, "posted", entry_id=matches[0]["id"])
    memory.record_post()
    memory.incr("posts_posted")
    return {"status": "posted", "entry_id": matches[0]["id"], "duplicates": extra}, 0, "posted"


def _reconcile_pending(memory, items, me, clock):
    """Settle actions left pending by a crash or an unverified post."""
    for act in memory.actions_with_status("pending"):
        found = [i for i in items if i["author_id"] == me and text_hash(i["text"]) == act["message_hash"]
                 and i["parent_id"] == act["target_entry_id"]]
        if found:
            memory.set_action(act["id"], "posted", entry_id=found[0]["id"], detail="reconciled")
            memory.record_post()
        else:
            memory.set_action(act["id"], "failed", detail="pending but never observed")
