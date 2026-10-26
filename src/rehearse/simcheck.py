"""The sim check: did the simulated person behave like a person? Code only, no model.

A sim that gives up, says it is an AI, or states facts it was never given makes a clean
scoreboard that means nothing. A "fail" here voids the run's other checks; "partial" does not.
"""

from __future__ import annotations

import re

from .loop import is_stop

ORDER = {"pass": 0, "partial": 1, "fail": 2}

_APOS = "['\u2019]"
_KIND = (r"(?:ai(?:\s+(?:assistant|language\s+model))?|(?:large\s+)?language\s+model|chatbot"
         r"|virtual\s+assistant|artificial\s+intelligence)")
# Clear self-identification: "I'm an AI", "as a language model", followed by the end of the clause.
_END = r"(?=\s*(?:[.,;:!?)\"\u201d]|$)|\s+(?:and|but|so|that|i)\b)"
_SELF_AI = re.compile(rf"\b(?:i{_APOS}?m|i\s+am|as\s+an?)\s+(?:(?:an?)\s+)?{_KIND}\b{_END}", re.I)
_SELF_NAMED = re.compile(rf"\bi{_APOS}?m\s+(?:claude|chatgpt|gemini)\b|\bi\s+am\s+(?:claude|chatgpt|gemini)\b"
                         r"|\bmade\s+by\s+(?:anthropic|openai|google)\b|\bi\s+was\s+trained\b", re.I)
_MENTION = re.compile(r"\bai\b|\bassistant\b|\blanguage\s+model\b|\bchatbot\b", re.I)
_STAGE_WORDS = (r"laugh|sigh|smil|paus|nod|shrug|grin|chuckl|groan|cough|frown|beat|silence|"
                r"typing|shakes|looks|stares|clears throat")
_STAGE = re.compile(rf"\*[^*\n]{{1,60}}\*|\[[^\]\n]{{1,40}}\]|\((?:[a-z ]*)(?:{_STAGE_WORDS})[a-z ]*\)", re.I)
_MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()
_MONTH_DAY = re.compile(r"\b(" + "|".join(_MONTHS) + r")[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b|\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?("
                        + "|".join(_MONTHS) + r")[a-z]*\b", re.I)
_TIME = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\b\.?|\b(\d{1,2}):(\d{2})\b", re.I)
_NUMBER = re.compile(r"\d+(?:[.,:/]\d+)*")
_UNITS = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
          "fifteen sixteen seventeen eighteen nineteen").split()
_TENS = {w: 10 * i for i, w in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if i >= 2}
_WORD_NUMBER = re.compile(r"\b(" + "|".join(_TENS) + r")(?:[- ](" + "|".join(_UNITS[1:10]) + r"))?\b|\b("
                          + "|".join(_UNITS) + r")\b", re.I)


def _word_numbers(text: str) -> set[str]:
    """Numbers written as words: "nine to five" makes 9 and 5 known; "twenty-five" makes 25."""
    out = set()
    for m in _WORD_NUMBER.finditer(text):
        if m.group(3):
            out.add(str(_UNITS.index(m.group(3).lower())))
        else:
            out.add(str(_TENS[m.group(1).lower()] + (_UNITS.index(m.group(2).lower()) if m.group(2) else 0)))
    return out


def _time_alternatives(m) -> set[str]:
    """Every 24-hour reading of a time: "5pm" is {"t17:00"}; a bare "7:30" is {"t07:30", "t19:30"}."""
    if m.group(1):
        h, minute = int(m.group(1)), int(m.group(2) or 0)
        h = h % 12 + (12 if m.group(3).lower() == "p" else 0)
        return {f"t{h:02d}:{minute:02d}"}
    h, minute = int(m.group(4)), int(m.group(5))
    alts = {f"t{h % 24:02d}:{minute:02d}"}
    if 1 <= h <= 12:
        alts.add(f"t{h % 12 + 12:02d}:{minute:02d}")
    return alts


def claims(text: str) -> list[tuple[str, set[str]]]:
    """Numbers, dates and times a text states, as (what to show, acceptable normal forms).

    A date such as "October 23rd" is one claim ("oct 23"), and a time such as "5pm" is one claim
    that "17:00" also satisfies; their parts are not listed again on their own.
    """
    out, taken = [], []
    for m in _MONTH_DAY.finditer(text):
        month, day = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
        form = f"{month[:3].lower()} {int(day)}"
        out.append((form, {form}))
        taken.append(m.span())
    for m in _TIME.finditer(text):
        out.append((m.group(0).strip(), _time_alternatives(m)))
        taken.append(m.span())
    for m in _NUMBER.finditer(text):
        if any(a <= m.start() < b for a, b in taken):
            continue
        tok = m.group(0)
        tok = tok.replace(",", "") if re.fullmatch(r"\d{1,3}(,\d{3})+", tok) else tok
        out.append((tok, {tok}))
    return out


def known_numbers(text: str) -> set[str]:
    """Everything a text makes acceptable: its claims, every number in it, and their parts.

    "7:30" makes 7:30, 7, 30 and both 24-hour readings known; "Oct 27" makes "oct 27" and 27 known.
    """
    known = set()
    for _, alts in claims(text):
        known |= alts
    for tok in _NUMBER.findall(text):
        known.add(tok.replace(",", "") if re.fullmatch(r"\d{1,3}(,\d{3})+", tok) else tok)
        known |= {part for part in re.split(r"[.,:/]", tok) if part}
    return known | _word_numbers(text)


def _item(name, result, evidence):
    return {"name": name, "result": result, "evidence": evidence}


def sim_check(run) -> dict:
    """Check the sim's side of a saved run. Returns items, an overall result, and void info."""
    replies = [t for t in run.turns if t["side"] == "person" and t["n"] >= 1]
    stop_word = run.meta.get("sim_stop_word") or "DONE"
    items = []

    empty = [f"sim-{c['index']:02d}" for c in run.calls
             if c["seat"] == "sim" and not (c["result"].get("text") or "").strip()]
    if "empty reply twice" in run.meta.get("stop_reason", "") or empty:
        items.append(_item("empty_reply", "fail", "the sim answered with nothing" +
                           (f" ({', '.join(empty)})" if empty else "")))
    else:
        items.append(_item("empty_reply", "pass", "no empty sim replies"))

    said = [t["text"] for t in replies]
    sure = next((x for x in said if _SELF_AI.search(x) or _SELF_NAMED.search(x)), None)
    vague = next((x for x in said if _MENTION.search(x)), None)
    if sure:
        items.append(_item("says_it_is_an_ai", "fail", f"the sim says what it is: {sure[:60]!r}"))
    elif vague:
        items.append(_item("says_it_is_an_ai", "partial", f"mentions being an AI? check: {vague[:60]!r}"))
    else:
        items.append(_item("says_it_is_an_ai", "pass", "never says it is an AI"))

    first = replies[0]["text"] if replies else ""
    items.append(_item("stop_on_first_reply", "fail", f"stop word on its first reply: {first[:60]!r}")
                 if replies and is_stop(first, stop_word)[0]
                 else _item("stop_on_first_reply", "pass", "did not stop on its first reply"))

    hits = [t["text"][:60] for t in replies if _STAGE.search(t["text"])]
    items.append(_item("stage_directions", "partial", f"stage direction in: {hits[0]!r}") if hits
                 else _item("stage_directions", "pass", "no stage directions"))

    n = sum(1 for t in replies if t.get("label_stripped"))
    items.append(_item("label_stripped", "partial", f"a label was stripped from {n} reply(ies)") if n
                 else _item("label_stripped", "pass", "no labels stripped"))

    if run.meta.get("stop_with_text"):
        items.append(_item("stop_with_text", "partial", "the sim said more than the stop word when it stopped"))
    else:
        items.append(_item("stop_with_text", "pass", "stop word was alone, or the sim did not stop"))

    known = known_numbers(run.knowledge) | known_numbers(run.input("persona.md"))
    invented: list[str] = []
    for t in replies:
        seen = set(known)
        for s_ in run.turns:
            if s_["side"] == "person" and s_["n"] == 0 or s_["side"] == "subject" and s_["n"] <= t["n"]:
                seen |= known_numbers(s_["text"])
        for shown, alts in claims(t["text"]):
            if not alts & seen and shown not in invented:
                invented.append(shown)
    items.append(_item("invented_numbers", "partial", "may be invented: " + ", ".join(invented)) if invented
                 else _item("invented_numbers", "pass", "every number, date and time is on the knowledge page, "
                                                         "in the persona, or in the subject's earlier turns"))

    worst = max((i["result"] for i in items), key=ORDER.get)
    failed = [f"{i['name']}: {i['evidence']}" for i in items if i["result"] == "fail"]
    return {"result": worst, "items": items, "void": bool(failed), "void_reason": "; ".join(failed)}
