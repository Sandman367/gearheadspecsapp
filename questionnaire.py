"""
The Spec Tree questionnaire engine.

Shared deliberately by seed.py and app.py. The seeder needs to know every field
the questionnaire can ever trigger (so spec_fields is a complete, closed set
before anything runs), and the server needs to walk the same branching to
decide what a submitted set of answers actually triggers.

If the client walked the tree and the server trusted the result, a hand-written
POST could claim any fields it liked. So the client walks it for the UI, and
the server walks it again for real.
"""
import json
import os

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

_cache = None


def load():
    global _cache
    if _cache is None:
        with open(os.path.join(DATA, "questionnaire.json"), encoding="utf-8") as f:
            _cache = json.load(f)
    return _cache


class QuestionnaireError(Exception):
    pass


def canonical(label):
    """Resolve a questionnaire label onto the field it already is.

    The wizard and the CB919 sheet were written at different times and name
    some of the same things differently. Without this, answering the wizard
    would create 'Intake Valve Clearance' next to the existing
    'Valve Clearance — Intake' and the bike would show both, one of them empty.
    """
    return load()["aliases"].get(label, label)


def questions_triggering(label):
    """Which answers ask for this field, by canonical label.

    Deleting a field the questionnaire still asks for leaves a path that cannot
    be answered: build_spec_tree refuses to invent the field, so the whole
    submission fails with a 500. That happened to "Chain and sprockets" and
    made the commonest drive system unanswerable, so the delete route checks
    here first.
    """
    doc = load()
    hits = []
    for qid, q in doc["questions"].items():
        groups = [(o["l"], o.get("fields") or {}) for o in q.get("options", [])]
        if isinstance(q.get("next"), list):
            groups += [(None, r.get("add_fields") or {})
                       for r in q["next"]]
        for opt, fields in groups:
            for labels in fields.values():
                if any(canonical(x) == label for x in labels):
                    hits.append({"question_id": qid, "option": opt,
                                 "text": q.get("text", "")})
    return hits


def all_triggerable_fields():
    """Every (label, category) any path through the questionnaire can trigger.

    Used at seed time so the field registry is complete up front — answering
    the questionnaire then only ever creates specs rows, never new fields.
    """
    out = {}
    for q in load()["questions"].values():
        for opt in q["options"]:
            for category, labels in (opt.get("fields") or {}).items():
                for label in labels:
                    out[canonical(label)] = category
        nxt = q.get("next")
        if isinstance(nxt, list):
            for rule in nxt:
                for category, labels in (rule.get("add_fields") or {}).items():
                    for label in labels:
                        out[canonical(label)] = category
    return out


def _matches(rule, qid, answers):
    when = rule.get("when")
    if not when:
        return True  # the fallback rule
    if "answer" in when and answers.get(qid) not in when["answer"]:
        return False
    if "answer_of" in when:
        for other_qid, allowed in when["answer_of"].items():
            if answers.get(other_qid) not in allowed:
                return False
    return True


def run(answers):
    """Walk the questionnaire with the given answers.

    Returns (path, fields, not_sure, bike_type, effective) where
      path      — question ids actually asked, in order
      fields    — {canonical_label: category} triggered
      not_sure  — [{question_id, text}] the answerer could not commit to
      bike_type — the q0 answer, used to route the bike to the right expert
      effective — {question_id: option} the answers this walk actually USED

    `effective` exists because the submitted answers can contain more than the
    walk reaches. A wizard that lets you go back and change an earlier answer
    leaves the replies to questions that are no longer asked sitting in the
    payload: answer "yes, it has a fuel pump", say it is mechanical, then go
    back and change it to "no fuel pump", and the mechanical reply is still
    there. Treating that as an answer put a mechanical-pump spec on a bike with
    no pump. Only what the walk used is a real answer about the bike.
    """
    doc = load()
    questions = doc["questions"]
    answers = dict(answers)  # 'auto' rules write here; don't mutate the caller's

    qid = "q0"
    path, fields, not_sure = [], {}, []
    effective = {}
    bike_type = None
    guard = 0

    while qid != "done":
        guard += 1
        if guard > 200:
            raise QuestionnaireError("questionnaire did not terminate")
        q = questions.get(qid)
        if not q:
            raise QuestionnaireError(f"unknown question: {qid}")

        answer = answers.get(qid)
        if answer is None:
            raise QuestionnaireError(f"no answer for {qid}: {q['text']}")
        option = next((o for o in q["options"] if o["l"] == answer), None)
        if not option:
            raise QuestionnaireError(f"{answer!r} is not an option for {qid}")

        path.append(qid)
        effective[qid] = answer

        # The bike type is the one answer that is a label rather than a set
        # of fields; "not sure" leaves it unset, so the page shows no type
        # until admin settles it.
        if qid == "q0" and not option.get("not_sure"):
            bike_type = option["t"]

        # "Not 100% sure" is not an answer, it is a question for admin. The
        # branch still has to be taken, but no field values are claimed.
        if option.get("not_sure"):
            not_sure.append({"question_id": qid, "text": q["text"]})
        else:
            for category, labels in (option.get("fields") or {}).items():
                for label in labels:
                    fields[canonical(label)] = category

        nxt = q.get("next")
        if isinstance(nxt, str):
            qid = nxt
            continue

        for rule in nxt:
            if not _matches(rule, qid, answers):
                continue
            # An 'auto' answer stands in for a question that is not worth
            # asking because this answer already settles it.
            for auto_qid, auto_ans in (rule.get("auto") or {}).items():
                answers[auto_qid] = auto_ans
                # An auto answer is still an answer about this bike — "no
                # battery" settles "no electric starter" — so it counts as
                # used, even though it was never put on screen.
                effective[auto_qid] = auto_ans
                auto_q = questions[auto_qid]
                auto_opt = next(o for o in auto_q["options"] if o["l"] == auto_ans)
                for category, labels in (auto_opt.get("fields") or {}).items():
                    for label in labels:
                        fields[canonical(label)] = category
            for category, labels in (rule.get("add_fields") or {}).items():
                for label in labels:
                    fields[canonical(label)] = category
            qid = rule["goto"]
            break
        else:
            raise QuestionnaireError(f"no branch matched at {qid}")

    return path, fields, not_sure, bike_type, effective
