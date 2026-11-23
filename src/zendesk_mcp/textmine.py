"""Small, dependency-free text tools for ticket text: tokenizing, key terms, TF-IDF vectors and
similarity clustering. Good enough to group "same issue" tickets; not a language model."""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Iterable

STOP = set("""
a about above after again against all also am an and any are aren't as at be because been before being below
between both but by can can't cannot could couldn't did didn't do does doesn't doing don't down during each few for
from further had hadn't has hasn't have haven't having he her here hers herself him himself his how i i'd i'll i'm
i've if in into is isn't it it's its itself just let's me more most mustn't my myself no nor not of off on once only
or other ought our ours ourselves out over own same shan't she should shouldn't so some such than that that's the
their theirs them themselves then there there's these they they'd they'll they're they've this those through to too
under until up very was wasn't we we'd we'll we're we've were weren't what what's when when's where where's which
while who who's whom why why's with won't would wouldn't you you'd you'll you're you've your yours yourself
yourselves will shall may might must get got getting gets go going goes went see seen seeing make made making
hi hello hey dear thanks thank thx regards best cheers kind sincerely please pls team support help helping ticket
request issue issues problem problems question questions know need needs needed want wants like just also still
able unable trying tried try anyone someone something anything everything one two three us via etc yes ok okay sure
today yesterday tomorrow now currently recently soon asap urgent day days week weeks time times morning afternoon
attached attachment screenshot image png jpg email emails sent send reply message sorry apologies wondering
following follow update updates use using used work working works worked way thing things look looking seems seem
""".split())

_URL = re.compile(r"https?://\S+|www\.\S+")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_TOKEN = re.compile(r"[a-z][a-z0-9_+#.-]*[a-z0-9+#]|[a-z][0-9]")
_QUOTED = re.compile(r"\n(?:On .{5,200}wrote:|-{2,} ?Original Message|From: .+\nSent: ).*", re.S | re.I)
_SIGNOFF = re.compile(r"\n(?:--|best|regards|thanks|cheers|sent from)[\s,].*", re.S | re.I)


def clean(text: str | None) -> str:
    text = _QUOTED.sub("", text or "")
    text = _SIGNOFF.sub("", text)
    return _EMAIL.sub(" ", _URL.sub(" ", text))


def tokens(text: str | None, extra_stop: set[str] | frozenset = frozenset()) -> list[str]:
    out = []
    for tok in _TOKEN.findall(clean(text).lower()):
        tok = tok.strip(".-_")
        if len(tok) < 2 or tok in STOP or tok in extra_stop or tok.replace(".", "").isdigit():
            continue
        out.append(tok)
    return out


def key_terms(text: str | None, extra_stop: set[str] | frozenset = frozenset()) -> set[str]:
    """Unigrams plus adjacent-word bigrams (after stopword removal)."""
    toks = tokens(text, extra_stop)
    return set(toks) | {f"{a} {b}" for a, b in zip(toks, toks[1:]) if a != b}


def ticket_text(t: dict, desc_chars: int = 600) -> str:
    """Subject counts twice: it's the customer's own summary of the problem."""
    subject = t.get("subject") or ""
    return f"{subject}\n{subject}\n{(t.get('description') or '')[:desc_chars]}"


def name_stopwords(names: Iterable[str | None]) -> set[str]:
    return {p.lower() for n in names if n for p in re.split(r"[\s.@_-]+", n) if len(p) >= 2}


# ----------------------------------------------------------------------------- TF-IDF + clustering

def tfidf(docs: list[list[str]]) -> list[dict[str, float]]:
    df = Counter(term for d in docs for term in set(d))
    n = len(docs)
    vecs = []
    for d in docs:
        tf = Counter(d)
        v = {t: (1 + math.log(c)) * math.log((1 + n) / (1 + df[t])) for t, c in tf.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        vecs.append({t: x / norm for t, x in v.items() if x > 0})
    return vecs


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(x * b.get(t, 0.0) for t, x in a.items())


def cluster(vecs: list[dict[str, float]], threshold: float = 0.45, top_terms: int = 6) -> list[list[int]]:
    """Group documents whose cosine similarity ≥ threshold (single-link, union-find). Candidate pairs come
    from an inverted index over each document's strongest terms, so this stays fast for a few thousand docs."""
    parent = list(range(len(vecs)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    index: dict[str, list[int]] = {}
    for i, v in enumerate(vecs):
        for term in sorted(v, key=v.get, reverse=True)[:top_terms]:
            index.setdefault(term, []).append(i)
    seen: set[tuple[int, int]] = set()
    for ids in index.values():
        if len(ids) > 400:  # a term in hundreds of docs is too generic to pair on
            continue
        for x in range(len(ids)):
            for y in range(x + 1, len(ids)):
                i, j = ids[x], ids[y]
                if (i, j) in seen:
                    continue
                seen.add((i, j))
                if find(i) != find(j) and cosine(vecs[i], vecs[j]) >= threshold:
                    parent[find(i)] = find(j)
    groups: dict[int, list[int]] = {}
    for i in range(len(vecs)):
        groups.setdefault(find(i), []).append(i)
    return sorted(groups.values(), key=len, reverse=True)


def top_terms(vecs: list[dict[str, float]], k: int = 5) -> list[str]:
    total: Counter = Counter()
    for v in vecs:
        total.update(v)
    return [t for t, _ in total.most_common(k)]
