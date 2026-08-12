"""pacman's version comparison, because guessing it produces dead proposals.

A recipe change only reaches a user if pacman considers the new version
greater, so anything that decides what to propose has to agree with pacman
exactly. Approximating it is worse than not comparing at all: it produces
updates that look right in a table, are merged, are built, and are then never
offered to anybody.

This is a transcription of rpmvercmp() from libalpm, kept deliberately close
to the original so the two can be read side by side. The surprising rules are
upstream's, not ours:

  1.0.2a < 1.0.2      a leftover alpha segment never beats an empty one
  3.11.0 > 3.11.r5    a numeric segment always beats an alpha one
  1.0.2  = 1.0.2      separators of the same length are not compared
"""


def _isdigit(char: str) -> bool:
    return "0" <= char <= "9"


def _isalpha(char: str) -> bool:
    return ("a" <= char <= "z") or ("A" <= char <= "Z")


def _isalnum(char: str) -> bool:
    return _isdigit(char) or _isalpha(char)


def vercmp(a: str, b: str) -> int:
    """-1, 0 or 1, exactly as `vercmp` would answer."""

    if a == b:
        return 0

    one = two = ptr1 = ptr2 = 0
    while one < len(a) and two < len(b):
        while one < len(a) and not _isalnum(a[one]):
            one += 1
        while two < len(b) and not _isalnum(b[two]):
            two += 1

        if not (one < len(a) and two < len(b)):
            break

        # Different amounts of separator end the comparison, which is why
        # 1.0.2 and 1.0..2 are not the same version.
        if (one - ptr1) != (two - ptr2):
            return -1 if (one - ptr1) < (two - ptr2) else 1

        ptr1, ptr2 = one, two
        if _isdigit(a[ptr1]):
            while ptr1 < len(a) and _isdigit(a[ptr1]):
                ptr1 += 1
            while ptr2 < len(b) and _isdigit(b[ptr2]):
                ptr2 += 1
            isnum = True
        else:
            while ptr1 < len(a) and _isalpha(a[ptr1]):
                ptr1 += 1
            while ptr2 < len(b) and _isalpha(b[ptr2]):
                ptr2 += 1
            isnum = False

        segment_a, segment_b = a[one:ptr1], b[two:ptr2]
        if not segment_a:
            return -1
        if not segment_b:
            # One side has a number where the other has letters, and a number
            # is always the newer of the two.
            return 1 if isnum else -1

        if isnum:
            segment_a = segment_a.lstrip("0")
            segment_b = segment_b.lstrip("0")
            if len(segment_a) > len(segment_b):
                return 1
            if len(segment_b) > len(segment_a):
                return -1

        if segment_a != segment_b:
            return -1 if segment_a < segment_b else 1

        one, two = ptr1, ptr2

    if one >= len(a) and two >= len(b):
        return 0

    rest_a = a[one:]
    rest_b = b[two:]
    if (not rest_a and not (rest_b and _isalpha(rest_b[0]))) or (rest_a and _isalpha(rest_a[0])):
        return -1
    return 1


def newer(candidate: str, current: str) -> bool:
    """Whether pacman would offer candidate to somebody holding current."""

    return vercmp(candidate, current) > 0
