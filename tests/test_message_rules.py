"""Message rules: limits, formatting, names, style, signature, rejection handling, drafts."""
import re
import sqlite3

import pytest

from agent import config
from agent.compose import SIGNATURE, add_signature, count_sentences, parse_blocks, strip_signature, to_html
from agent.memory import Memory
from agent.textutil import html_to_text, normalize
from agent.validate import validate_message
from tests.mock_canvas import TOKEN

NAMES = ["Robyn Smith", "Alex Chen"]
INTRO = "Two things stand out."
BULLETS = "Two things stand out:\n- The step size matters.\n- The conditioning matters too."


def v(msg, names=NAMES, past=(), banned=None):
    return validate_message(msg, token=TOKEN, past_posts=list(past), author_names=names,
                            banned_phrases_path=banned)


def has(problems, fragment):
    return any(fragment in p for p in problems)


def reply(target, msg, reason="r"):
    return {"action": "reply", "target_entry_id": str(target), "message": msg, "reason": reason}


def db(runner):
    return sqlite3.connect(runner.env["STATE_DIR"] + "/agent.db")


# --- 1. limits ---------------------------------------------------------------------------------
def test_ten_sentences_ok_eleven_rejected():
    ten = " ".join(f"Point number {i} holds." for i in range(10))
    assert v(ten) == []
    assert has(v(ten + " One more."), "11 sentences")


def test_sentence_counting_handles_abbreviations_and_decimals():
    blocks = parse_blocks("Use a rate of 0.5, e.g. for the first epoch, i.e. early on. Does it help? Yes!")
    assert count_sentences(blocks) == 3


def test_each_bullet_counts_as_one_sentence():
    assert count_sentences(parse_blocks(BULLETS)) == 3
    nine = " ".join(f"Point {i} holds." for i in range(5))
    msg = nine + "\n\n" + "\n".join(f"- item {i}" for i in range(6))  # 5 + 6 = 11
    assert has(v(msg), "11 sentences")


def test_at_most_six_bullets():
    ok = "Reasons:\n" + "\n".join(f"- reason {i}" for i in range(6))
    assert v(ok) == []
    assert has(v(ok + "\n- reason 7"), "7 bullets")


def test_bullets_need_an_intro_sentence():
    assert has(v("- first\n- second"), "intro sentence")
    assert v("Intro here.\n- a\n- b") == []   # a list directly after a paragraph is fine


def test_character_limit():
    assert v("word " * 240) == []                      # 1199 chars once stripped
    assert has(v("word " * 241), "1200 characters")    # 1204 chars


# --- 1. forbidden characters and markup -------------------------------------------------------
@pytest.mark.parametrize("msg", ["It works — mostly.", "Pages 3–5 cover it.", "A ― B."])
def test_dashes_rejected(msg):
    assert has(v(msg), "em dash or en dash")


def test_plain_hyphens_are_fine():
    assert v("The well-known trick works for 3-5 steps.") == []


@pytest.mark.parametrize("msg", ["Nice work \U0001f600", "Looks right ✅", "Heart ❤️ here"])
def test_emoji_rejected(msg):
    assert has(v(msg), "emoji")


def test_math_symbols_are_not_emoji():
    assert v("If x ≤ y then f(x) → 0 and ⌈x⌉ is defined.") == []


@pytest.mark.parametrize("msg", [
    "See https://example.com/x", "See https://canvas.mit.edu/courses/1", "See www.example.org",
    "Go to example.com/page", "See [the docs](page)",
])
def test_links_rejected_even_canvas(msg):
    assert has(v(msg), "link")


@pytest.mark.parametrize("msg,fragment", [
    ("# Heading\nBody text.", "heading"),
    ("This is **bold** text.", "bold"),
    ("| a | b |\n| --- | --- |\n| 1 | 2 |", "table"),
    ("Intro.\n\n* star bullet", 'bullets must start with "- "'),
])
def test_markdown_rejected(msg, fragment):
    assert has(v(msg), fragment)


# --- 3. names ---------------------------------------------------------------------------------
def test_full_and_last_name_rejected_and_named():
    assert has(v("Robyn Smith made a good point about variance."), 'full name "Robyn Smith"')
    assert has(v("As Smith noted, variance matters."), 'last name "Smith"')
    assert has(v("As smith's post says, variance matters."), 'last name "Smith"')


def test_first_name_only_as_possessive_agent():
    assert v("Robyn's agent raised variance.") == []
    assert v("Robyn’s agent raised variance.") == []
    assert has(v("Robyn raised variance."), '"Robyn" is allowed only as "Robyn\'s agent"')
    assert has(v("Robyn's agent agreed with Robyn here."), '"Robyn" is allowed only')
    assert has(v("Robyn's idea is good."), '"Robyn" is allowed only')


def test_unrelated_words_are_not_names():
    assert v("Robynne and Alexander both asked.") == []


def test_unknown_or_tiny_names_ignored():
    assert v("Anything goes here.", names=["unknown", "A", ""]) == []


# --- 4. style ---------------------------------------------------------------------------------
@pytest.mark.parametrize("msg", [
    "Great point about variance.", "I agree with the second claim.", "Thanks for the clarification.",
    "Interesting question about the prior.", "great point, though.",
])
def test_praise_openers_rejected(msg):
    assert has(v(msg), "opens with filler or praise")


def test_opener_words_are_fine_mid_message():
    assert v("The prior is interesting because it is flat. I agree with the second claim.") == []


@pytest.mark.parametrize("msg,phrase", [
    ("We should delve into the proof.", "delve"),
    ("It is a rich tapestry of results.", "tapestry"),
    ("We can leverage the Hessian.", "leverage"),
    ("The estimator is robust to noise.", "robust"),
    ("It's worth noting that variance drops.", "worth noting"),
    ("In conclusion, variance drops.", "In conclusion"),
    ("This is not just fast but also correct.", "not just fast but"),
    ("It isn't merely biased, but also noisy.", "isn't merely biased, but"),
])
def test_default_banned_phrases(msg, phrase):
    assert has(v(msg), f'banned phrase: "{phrase}"') or has(v(msg), "banned phrase")
    assert has(v(msg), phrase)


def test_banned_phrase_list_is_editable_without_code_changes(tmp_path):
    path = tmp_path / "banned.txt"
    path.write_text("# comment\n\nzebra stripes\nre:\\bfoo\\d+\\b\n")
    assert has(v("The zebra   stripes are plain.", banned=path), 'banned phrase: "zebra   stripes"')
    assert has(v("See foo42 here.", banned=path), "foo42")
    assert v("We can leverage the Hessian.", banned=path) == []   # not in this list
    path.write_text("")
    assert v("The zebra stripes are plain.", banned=path) == []


def test_missing_or_broken_banned_list_fails_closed(tmp_path):
    assert has(v("Fine message.", banned=tmp_path / "nope.txt"), "banned phrase list unusable")
    bad = tmp_path / "bad.txt"
    bad.write_text("re:(unclosed\n")
    assert has(v("Fine message.", banned=bad), "banned phrase list unusable")


# --- 5. signature -----------------------------------------------------------------------------
@pytest.mark.parametrize("msg", ["Variance drops.\n\nNitya's AI agent", "Variance drops. Nitya’s AI agent"])
def test_agent_written_signature_rejected(msg):
    assert has(v(msg), "signature")


def test_signature_helpers():
    assert add_signature("Hello.\n") == f"Hello.\n\n{SIGNATURE}"
    assert strip_signature(add_signature("Hello.")) == "Hello."
    assert "—" not in SIGNATURE and "-" not in SIGNATURE


def test_similarity_ignores_the_signature_on_past_posts():
    past = add_signature("Gradient descent converges slowly with poor conditioning.")
    assert has(v("Gradient descent converges slowly with poor conditioning!", past=[past]), "too similar")
    assert v("Short note.", past=[add_signature("Totally unrelated words here.")]) == []


# --- 2. formatting ----------------------------------------------------------------------------
def test_bullet_message_converts_to_the_right_html():
    html = to_html(add_signature(BULLETS))
    assert html == ("<p>Two things stand out:</p>"
                    "<ul><li>The step size matters.</li><li>The conditioning matters too.</li></ul>"
                    "<p>Nitya&#x27;s AI agent</p>")


def test_blank_line_blocks_become_paragraphs_and_wrapped_lines_join():
    assert to_html("One\nstill one.\n\nTwo.") == "<p>One still one.</p><p>Two.</p>"


def test_html_injection_is_escaped():
    html = to_html('<script>alert("x")</script> & <img src=x onerror=1>')
    assert "<script" not in html and "<img" not in html
    assert html == ("<p>&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; "
                    "&lt;img src=x onerror=1&gt;</p>")


def test_html_reads_back_to_plain_text():
    canonical = add_signature("Two things stand out:\n\n- The step size matters.\n- The conditioning matters too.")
    assert html_to_text(to_html(canonical)) == canonical
    loose = add_signature(BULLETS)   # no blank line before the list: same words, same lines
    assert normalize(html_to_text(to_html(loose))) == normalize(loose)
    tricky = "Use <b>tags</b> & \"quotes\" carefully."
    assert html_to_text(to_html(tricky)) == tricky


# --- CLI: post path, rejection, give-up, drafts -----------------------------------------------
def test_bullet_message_is_posted_as_html_with_signature_last(runner, mock):
    e = mock.add(7, "Alice", "<p>Question?</p>")
    code, res = runner.submit(reply(e["id"], BULLETS))
    assert code == 0 and res["status"] == "posted"
    body = mock.entries[-1]["message"]
    assert ("<ul><li>The step size matters.</li><li>The conditioning matters too.</li></ul>"
            "<p>Nitya&#x27;s AI agent</p>") in body
    assert body.count("AI agent") == 1


def test_html_injection_attempt_is_escaped_when_posted(runner, mock):
    e = mock.add(7, "Alice", "<p>Question?</p>")
    code, res = runner.submit(reply(e["id"], 'Try <img src=x onerror=alert(1)> in the form.'))
    assert code == 0 and res["status"] == "posted"   # also proves the post is found again on read-back
    body = mock.entries[-1]["message"]
    assert "&lt;img src=x onerror=alert(1)&gt;" in body and "<img" not in body


def test_em_dash_message_is_rejected_and_recorded(runner, mock):
    e = mock.add(7, "Alice", "<p>Question?</p>")
    code, res = runner.submit(reply(e["id"], "It converges — slowly."))
    assert code == 4 and res["status"] == "rejected" and set(res) == {"status", "problems"}
    assert has(res["problems"], "em dash")
    assert mock.posts() == []
    assert db(runner).execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0
    run = db(runner).execute("SELECT kind, action, outcome, detail, message FROM runs").fetchone()
    assert run[:3] == ("submit", "reply", "rejected") and "em dash" in run[3] and "converges" in run[4]


def test_agent_written_signature_is_rejected_by_submit(runner, mock):
    e = mock.add(7, "Alice", "<p>Question?</p>")
    code, res = runner.submit(reply(e["id"], "Variance drops.\n\nNitya's AI agent"))
    assert res["status"] == "rejected" and mock.posts() == []


def test_target_problems_use_the_same_rejected_shape(runner, mock):
    code, res = runner.submit(reply(999999, "Variance drops."))
    assert code == 4 and res["status"] == "rejected" and has(res["problems"], "target entry does not exist")


def test_two_rejected_attempts_then_the_cycle_ends_with_none(runner, mock):
    e = mock.add(7, "Alice", "<p>Question?</p>")
    runner.run("fetch")
    bad = reply(e["id"], "It converges — slowly.")
    assert runner.submit(bad)[1]["status"] == "rejected"
    assert runner.submit(bad)[1]["status"] == "rejected"
    code, res = runner.submit(reply(e["id"], "A perfectly good message."))   # third attempt is dropped
    assert code == 0 and res["status"] == "none" and "2 rejected attempts" in res["detail"]
    assert mock.posts() == []
    last = db(runner).execute("SELECT action, outcome FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    assert last == ("none", "none")
    # the cycle is over: the offered item is now seen, and a new fetch starts a fresh cycle
    assert runner.run("fetch", "--peek")[1]["count"] == 0
    e2 = mock.add(8, "Bob", "<p>Another?</p>")
    runner.run("fetch")
    assert runner.submit(reply(e2["id"], "A perfectly good message."))[1]["status"] == "posted"


def test_none_is_always_allowed_after_rejections(runner, mock):
    e = mock.add(7, "Alice", "<p>Question?</p>")
    for _ in range(2):
        runner.submit(reply(e["id"], "Bad — dash."))
    code, res = runner.submit({"action": "none", "reason": "giving up"})
    assert code == 0 and res["status"] == "none" and "detail" not in res


def test_rejected_attempt_does_not_mark_items_seen(runner, mock):
    e = mock.add(7, "Alice", "<p>Question?</p>")
    runner.run("fetch")
    runner.submit(reply(e["id"], "Bad — dash."))
    assert runner.run("fetch", "--peek")[1]["count"] == 1
    assert runner.submit(reply(e["id"], "Variance drops."))[1]["status"] == "posted"
    assert runner.run("fetch", "--peek")[1]["count"] == 0


def test_names_come_from_seen_items(runner, mock):
    e = mock.add(7, "Robyn Smith", "<p>Question?</p>")
    runner.run("fetch")
    runner.submit({"action": "none"})
    assert db(runner).execute("SELECT author FROM seen_items").fetchone() == ("Robyn Smith",)
    code, res = runner.submit(reply(e["id"], "Smith asked about variance."))
    assert res["status"] == "rejected" and has(res["problems"], 'last name "Smith"')
    runner.submit({"action": "none"})
    code, res = runner.submit(reply(e["id"], "Robyn's agent asked about variance, so here is a note."))
    assert res["status"] == "posted"


def test_names_of_just_offered_items_count_before_they_are_marked_seen(runner, mock):
    e = mock.add(7, "Robyn Smith", "<p>Question?</p>")
    runner.run("fetch")      # offered, not yet seen
    code, res = runner.submit(reply(e["id"], "Robyn Smith asked about variance."))
    assert res["status"] == "rejected" and has(res["problems"], "full name")


def test_banned_phrases_file_is_read_on_each_submit(runner, mock, tmp_path):
    e = mock.add(7, "Alice", "<p>Question?</p>")
    path = tmp_path / "banned.txt"
    path.write_text("zebra\n")
    assert runner.submit(reply(e["id"], "A zebra appears."), BANNED_PHRASES_FILE=str(path))[1]["status"] == "rejected"
    runner.submit({"action": "none"})
    path.write_text("giraffe\n")
    assert runner.submit(reply(e["id"], "A zebra appears."), BANNED_PHRASES_FILE=str(path))[1]["status"] == "posted"


def test_old_databases_are_upgraded(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE seen_items (item_id TEXT PRIMARY KEY, kind TEXT, author_id TEXT, first_seen REAL NOT NULL);
        CREATE TABLE offered_items (run_id INTEGER NOT NULL, item_id TEXT NOT NULL, kind TEXT, author_id TEXT,
                                    PRIMARY KEY (run_id, item_id));
        CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL, action TEXT,
                           target_entry_id TEXT, reason TEXT, outcome TEXT NOT NULL, detail TEXT);""")
    con.close()
    m = Memory(path)
    rid = m.start_run("submit", "reply", "1", "why", "text")
    m.finish_run(rid, "ok")
    assert m.recent_decisions()[0]["message"] == "text" and m.known_author_names() == set()


# --- 7. drafts --------------------------------------------------------------------------------
def test_drafts_lists_decisions_with_message_and_reason(runner, mock):
    e = mock.add(7, "Alice", "<p>Question?</p>")
    runner.submit(reply(e["id"], BULLETS, reason="answers the question"), DRY_RUN="1")
    runner.submit({"action": "none", "reason": "nothing useful to add"})
    runner.submit(reply(e["id"], "Bad — dash.", reason="tried again"))
    code, out = runner.run_text("drafts")
    assert code == 0
    assert mock.posts() == []
    newest, middle, oldest = re.split(r"\n\n(?=#\d+ )", out)
    assert "[rejected]" in newest and "Bad — dash." in newest
    assert "reason: tried again" in newest and "problems:" in newest and "em dash" in newest
    assert "[none]" in middle and "message: (none)" in middle and "reason: nothing useful to add" in middle
    assert "[dry_run]" in oldest and "-> entry" in oldest and "reason: answers the question" in oldest
    assert "    - The step size matters." in oldest
    assert "\n\n    Nitya's AI agent\n  reason:" in oldest   # what would be posted, signature last


def test_drafts_shows_only_the_last_20(runner, mock):
    for i in range(25):
        runner.submit({"action": "none", "reason": f"reason {i}"})
    code, out = runner.run_text("drafts")
    assert out.count("[none]") == 20 and "reason 24" in out and "reason 4\n" not in out


def test_drafts_with_nothing_recorded(runner):
    assert runner.run_text("drafts") == (0, "no decisions recorded yet\n")


def test_drafts_works_while_stopped(runner, env):
    import pathlib
    state = pathlib.Path(env["STATE_DIR"])
    state.mkdir(parents=True)
    (state / "STOPPED").write_text("test\n")
    assert runner.run_text("drafts")[0] == 0
