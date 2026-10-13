"""Disproportionality statistics (spec §9–11).

Deterministic, dependency-free, hand-verified formulas. This module is the
AUTHORITATIVE source of PRR/ROR numbers — the LLM never computes them
(spec §65 Rule 4).

2x2 contingency table (full background population, spec §5):

                    Event       No Event
    Drug              a            b
    Other drugs       c            d

Methods (Evans et al. 2001 for PRR screening; ROR with log-Woolf CI):

    PRR = [a/(a+b)] / [c/(c+d)]
    SE(ln PRR) = sqrt(1/a - 1/(a+b) + 1/c - 1/(c+d))
    ROR = (a/b) / (c/d) = ad/bc
    SE(ln ROR) = sqrt(1/a + 1/b + 1/c + 1/d)

Screening rules (documented as screening only — never causality, spec §2):
    PRR screen:  PRR >= 2  AND  chi-square >= 4  AND  a >= 3
    ROR screen:  lower 95% bound of ROR > 1

Zero-cell handling: the log-based measures are undefined when any cell is 0.
We apply the Haldane–Anscombe continuity correction (add 0.5 to all four
cells) ONLY when computing ln-based statistics for tables containing a zero,
and flag it (`continuity_corrected`). The raw table is never altered.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

Z_975 = 1.959963984540054  # two-sided 95%


@dataclass(frozen=True)
class Table2x2:
    a: int
    b: int
    c: int
    d: int

    def __post_init__(self) -> None:
        for name in ("a", "b", "c", "d"):
            v = getattr(self, name)
            if not isinstance(v, int) or v < 0:
                raise ValueError(f"2x2 cell {name} must be a non-negative int, got {v!r}")

    @property
    def total(self) -> int:
        return self.a + self.b + self.c + self.d

    @property
    def drug_cases(self) -> int:
        return self.a + self.b

    @property
    def event_cases(self) -> int:
        return self.a + self.c

    @property
    def has_zero(self) -> bool:
        return self.a == 0 or self.b == 0 or self.c == 0 or self.d == 0


@dataclass(frozen=True)
class SignalStat:
    """Everything downstream needs: point estimates + CIs + screening flags."""
    a: int
    b: int
    c: int
    d: int
    prr: float | None
    prr_lower: float | None
    prr_upper: float | None
    ror: float | None
    ror_lower: float | None
    ror_upper: float | None
    chi_square: float | None
    continuity_corrected: bool
    prr_screen: bool          # PRR >= 2 AND chi2 >= 4 AND a >= 3
    ror_screen: bool          # ROR lower 95% bound > 1
    insufficient: bool        # not enough data to compute meaningful stats

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def prr_point(t: Table2x2) -> float | None:
    """PRR = [a/(a+b)] / [c/(c+d)].

    None when either group total is zero, or when c == 0 (the ratio is
    infinite — undefined without a continuity correction; prr_ci handles
    the corrected estimate).
    """
    if (t.a + t.b) == 0 or (t.c + t.d) == 0 or t.c == 0:
        return None
    return (t.a / (t.a + t.b)) / (t.c / (t.c + t.d))


def prr_ci(t: Table2x2) -> tuple[float | None, float | None, float | None]:
    """PRR with 95% CI via log transform.

    Needs a>0 and c>0 for the raw log interval. When c == 0 (PRR infinite)
    but both group totals are positive, the Haldane–Anscombe 0.5 correction
    is applied to all cells for the point estimate and CI; ror_ci flags
    continuity_corrected for the same tables.
    """
    if (t.a + t.b) == 0 or (t.c + t.d) == 0:
        return None, None, None
    if t.c == 0:
        a, b, c, d = t.a + 0.5, t.b + 0.5, t.c + 0.5, t.d + 0.5
        point = (a / (a + b)) / (c / (c + d))
        se = math.sqrt(1.0 / a - 1.0 / (a + b) + 1.0 / c - 1.0 / (c + d))
        return (point,
                math.exp(math.log(point) - Z_975 * se),
                math.exp(math.log(point) + Z_975 * se))
    point = prr_point(t)
    if point is None or t.a == 0:
        return point, None, None
    se = math.sqrt(1.0 / t.a - 1.0 / (t.a + t.b) + 1.0 / t.c - 1.0 / (t.c + t.d))
    lo = math.exp(math.log(point) - Z_975 * se)
    hi = math.exp(math.log(point) + Z_975 * se)
    return point, lo, hi


def ror_point(t: Table2x2) -> float | None:
    """ROR = ad/bc; None if undefined (zero denominator without correction)."""
    denom = t.b * t.c
    if denom == 0:
        return None
    return (t.a * t.d) / denom


def ror_ci(t: Table2x2) -> tuple[float | None, float | None, float | None, bool]:
    """ROR with 95% CI (Woolf log method).

    Returns (point, lower, upper, continuity_corrected). When any cell is 0,
    the Haldane–Anscombe 0.5 correction is applied to all cells for the CI
    computation and the returned point estimate uses the corrected table too
    (flagged), since the raw point is infinite/undefined.
    """
    corrected = t.has_zero
    # Haldane–Anscombe: add 0.5 to all cells when any cell is zero.
    a, b, c, d = ((t.a + 0.5, t.b + 0.5, t.c + 0.5, t.d + 0.5)
                  if corrected else (t.a, t.b, t.c, t.d))
    if b * c == 0:
        return None, None, None, corrected
    point = (a * d) / (b * c)
    se = math.sqrt(1.0 / a + 1.0 / b + 1.0 / c + 1.0 / d)
    lo = math.exp(math.log(point) - Z_975 * se)
    hi = math.exp(math.log(point) + Z_975 * se)
    return point, lo, hi, corrected


def chi_square(t: Table2x2) -> float | None:
    """Pearson chi-square with Yates continuity correction (1 df).

    Used only as a screening support statistic (spec §9); None when any
    expected count degenerates (total or marginals zero).
    """
    n = t.total
    if n == 0:
        return None
    row1, row2 = t.drug_cases, t.c + t.d
    col1, col2 = t.event_cases, t.b + t.d
    if row1 == 0 or row2 == 0 or col1 == 0 or col2 == 0:
        return None
    # Yates correction: max(0, |ad-bc| - n/2)^2 * n / (row1*row2*col1*col2)
    adbc = t.a * t.d - t.b * t.c
    num = max(0.0, abs(adbc) - n / 2.0)
    return (num * num * n) / (row1 * row2 * col1 * col2)


def compute(t: Table2x2, min_cases: int = 3,
            prr_threshold: float = 2.0,
            chi2_threshold: float = 4.0) -> SignalStat:
    """Full statistic bundle for a 2x2 table (deterministic)."""
    prr, prr_lo, prr_hi = prr_ci(t)
    ror, ror_lo, ror_hi, corrected = ror_ci(t)
    chi2 = chi_square(t)

    insufficient = (
        t.total == 0 or t.a < min_cases or prr is None or chi2 is None
    )
    prr_screen = (
        not insufficient
        and prr >= prr_threshold
        and chi2 >= chi2_threshold
        and t.a >= min_cases
    )
    ror_screen = (not insufficient) and ror_lo is not None and ror_lo > 1.0

    if t.a < min_cases or t.total == 0:
        # Keep numbers but mark as below screening floor.
        pass

    return SignalStat(
        a=t.a, b=t.b, c=t.c, d=t.d,
        prr=prr, prr_lower=prr_lo, prr_upper=prr_hi,
        ror=ror, ror_lower=ror_lo, ror_upper=ror_hi,
        chi_square=chi2,
        continuity_corrected=corrected,
        prr_screen=bool(prr_screen),
        ror_screen=bool(ror_screen),
        insufficient=insufficient,
    )
