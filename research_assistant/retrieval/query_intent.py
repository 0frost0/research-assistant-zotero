"""Conservative definition search hints, never a source of answer facts."""
import re


QUERY_ALIASES = {
    "心衰": "heart failure hf",
    "心力衰竭": "heart failure hf",
    "痴呆": "dementia",
    "阿尔茨海默": "alzheimer dementia",
}


def definition_subject(question):
    value = question.strip().rstrip("？?。.!！").strip()
    value = re.sub(r"^(?:请问|请解释(?:一下)?|请介绍(?:一下)?)\s*", "", value)
    for pattern in (r"什么是\s*(.+)", r"(.+?)(?:是什么|是指什么|的定义(?:是什么)?)",
                    r"what (?:is|are)\s+(.+)", r"(?:define|definition of)\s+(.+)"):
        match = re.fullmatch(pattern, value, re.IGNORECASE)
        if match:
            subject = match[1].strip()
            # Do not strip conditions from a multi-part or follow-up question.
            if 1 < len(subject) <= 120 and not re.search(r"[，,；;？?]|以及|并且", subject):
                return subject
    return None


def definition_terms(question):
    subject = definition_subject(question)
    if not subject:
        return []
    aliases = QUERY_ALIASES.get(subject)
    terms = [subject]
    if aliases:
        terms.extend(term for term, value in QUERY_ALIASES.items() if value == aliases)
        terms.append(aliases.removesuffix(" hf"))
    return list(dict.fromkeys(terms))


def definition_query(question):
    terms = definition_terms(question)
    return " ".join(terms) + " definition 定义" if terms else ""


def definition_rank(question, quote):
    """Prioritize a subject's defining sentence; still requires semantic review.

    A definition of an unrelated term in a topic-related passage is not a
    definition of the requested subject. Keep the subject and predicate adjacent.
    """
    for term in definition_terms(question):
        subject = re.escape(term).replace(r"\ ", r"\s+")
        if term.isascii():
            subject = r"(?<!\w)" + subject + r"(?!\w)"
        parenthesis = r"\s*(?:[（(][^）)\n]{1,60}[）)]\s*)?"
        predicate = r"(?:is\s+(?:a\b|an\b|defined\b|characteri[sz]ed\b)|refers\s+to\b|是|指的是|定义为)"
        if re.search(subject + parenthesis + predicate, quote, re.IGNORECASE):
            return 1
    return 0
