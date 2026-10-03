"""Parsing and per-round sampling for review-author scope specifications."""

from __future__ import annotations

import random
import re
import time
from collections.abc import Iterable
from decimal import Decimal

_AUTHOR = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?")
_PROBABILITY = re.compile(r"(?:0(?:\.\d+)?|1(?:\.0+)?)")


class ReviewAuthorSpecError(ValueError):
    pass


def _parse_review_author_spec(value: str) -> tuple[str, Decimal]:
    login, separator, probability_text = value.partition(":")
    if ":" in probability_text:
        raise ReviewAuthorSpecError(f"invalid review author specification: {value!r}")
    if len(login) > 39 or not _AUTHOR.fullmatch(login) or "--" in login:
        raise ReviewAuthorSpecError(f"invalid GitHub login in review author specification: {value!r}")
    if separator:
        if not _PROBABILITY.fullmatch(probability_text):
            raise ReviewAuthorSpecError(f"invalid review author probability: {value!r}")
        probability = Decimal(probability_text)
    else:
        probability = Decimal(1)
    return login.casefold(), probability


def normalize_review_author_specs(values: Iterable[str]) -> tuple[str, ...]:
    """Validate, deduplicate, and canonicalize ``login[:probability]`` entries."""
    by_login: dict[str, Decimal] = {}
    for value in values:
        login, probability = _parse_review_author_spec(value)
        previous = by_login.get(login)
        if previous is not None and previous != probability:
            raise ReviewAuthorSpecError(f"conflicting probabilities for review author {login!r}")
        by_login[login] = probability

    def token(item: tuple[str, Decimal]) -> str:
        login, probability = item
        if probability == 1:
            return login
        return f"{login}:{format(probability.normalize(), 'f')}"

    return tuple(token(item) for item in sorted(by_login.items()))


def author_logins(specs: Iterable[str]) -> tuple[str, ...]:
    """Return the normalized logins in an author-scope specification."""
    return tuple(
        login for login, _ in (_parse_review_author_spec(spec) for spec in normalize_review_author_specs(specs))
    )


def select_review_authors(
    specs: Iterable[str], eligible_authors: Iterable[str], *, timestamp: int | None = None
) -> tuple[list[str], int | None]:
    """Select eligible author scope using priority authors and relative probability weights.

    A probability-1 author is a priority author: if any such author has actionable review work,
    all eligible priority authors are selected. Otherwise one eligible author with a positive
    probability is selected, with each author's chance proportional to its configured value. This
    makes probabilities useful for choosing among available work instead of accidentally filtering
    every peer out before eligibility is known.
    """
    normalized = normalize_review_author_specs(specs)
    seed = time.time_ns() if timestamp is None else timestamp
    if not normalized:
        return [], seed
    rng = random.Random(seed)
    eligible = {author.casefold() for author in eligible_authors}
    parsed = [_parse_review_author_spec(spec) for spec in normalized]
    priority = [login for login, probability in parsed if probability == 1 and login in eligible]
    if priority:
        return priority, seed
    weighted = [(login, probability) for login, probability in parsed if probability > 0 and login in eligible]
    if not weighted:
        return [], seed
    total = sum((probability for _, probability in weighted), start=Decimal(0))
    draw = rng.random() * float(total)
    for login, probability in weighted:
        draw -= float(probability)
        if draw < 0:
            return [login], seed
    return [weighted[-1][0]], seed


def sample_review_authors(specs: Iterable[str], *, timestamp: int | None = None) -> tuple[list[str], int | None]:
    """Compatibility helper for callers that still need independent author draws.

    Review loops use :func:`select_review_authors`, which selects from eligible authors after the
    survey. This helper preserves the historical stateless sampling behavior for external callers.
    """
    normalized = normalize_review_author_specs(specs)
    if not normalized:
        return [], None
    seed = time.time_ns() if timestamp is None else timestamp
    rng = random.Random(seed)
    allowed: list[str] = []
    for spec in normalized:
        login, probability = _parse_review_author_spec(spec)
        if probability == 1 or rng.random() < float(probability):
            allowed.append(login)
    return allowed, seed
