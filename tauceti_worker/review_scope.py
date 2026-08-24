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


def sample_review_authors(specs: Iterable[str], *, timestamp: int | None = None) -> tuple[list[str], int | None]:
    """Compute this round's ordinary allowed-author array from normalized specifications.

    The timestamp is the random seed and is returned for observable/reproducible logs. Entries with
    the default probability 1.0 are always present; all other decisions are independent draws.
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
