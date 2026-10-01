"""Shared measurement helpers: word error rate and percentiles."""
from __future__ import annotations

import re


def normalize_words(text: str) -> list[str]:
    text = text.lower().replace("-", " ")
    return re.findall(r"[\w']+", text, re.UNICODE)


def wer(reference: str, hypothesis: str) -> tuple[float, list[tuple[str, str]]]:
    """Word error rate (Levenshtein over words) and the list of (ref, hyp) substitutions/deletions/insertions."""
    r, h = normalize_words(reference), normalize_words(hypothesis)
    d = [[0] * (len(h) + 1) for _ in range(len(r) + 1)]
    for i in range(len(r) + 1):
        d[i][0] = i
    for j in range(len(h) + 1):
        d[0][j] = j
    for i in range(1, len(r) + 1):
        for j in range(1, len(h) + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (r[i - 1] != h[j - 1]))
    errors, i, j = [], len(r), len(h)
    while i > 0 or j > 0:
        if i > 0 and j > 0 and d[i][j] == d[i - 1][j - 1] + (r[i - 1] != h[j - 1]):
            if r[i - 1] != h[j - 1]:
                errors.append((r[i - 1], h[j - 1]))
            i, j = i - 1, j - 1
        elif i > 0 and d[i][j] == d[i - 1][j] + 1:
            errors.append((r[i - 1], "")); i -= 1
        else:
            errors.append(("", h[j - 1])); j -= 1
    return (d[len(r)][len(h)] / max(len(r), 1)), errors[::-1]


def percentile(values, q: float):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    k = (len(vals) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(vals) - 1)
    return round(vals[lo] + (vals[hi] - vals[lo]) * (k - lo), 1)
