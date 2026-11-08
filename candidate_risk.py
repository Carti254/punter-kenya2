"""Transparent evidence-based risk flags for automatic accumulator candidates.

This is a screening heuristic, not a trained loss or upset-probability model.
It only uses evidence supplied on the candidate row and explicitly reports gaps.
"""
from __future__ import annotations

import math


def assess_candidate_risk(row: dict) -> dict:
    flags: list[str] = []
    gaps: list[str] = []
    score = 0
    reject = False

    confidence = str(row.get("model_confidence") or "").strip().upper()
    if confidence == "LOW":
        flags.append("low base-model confidence")
        score += 1
    elif not confidence:
        gaps.append("model confidence not supplied")

    disagreement = row.get("external_model_disagreement")
    try:
        disagreement = float(disagreement) if disagreement is not None else None
    except (TypeError, ValueError, OverflowError):
        disagreement = None
    if disagreement is not None and math.isfinite(disagreement):
        if disagreement >= 0.25:
            flags.append(f"large independent-model disagreement ({disagreement:.1%})")
            score += 3
            reject = True
        elif disagreement >= 0.15:
            flags.append(f"material independent-model disagreement ({disagreement:.1%})")
            score += 2
        elif disagreement >= 0.10:
            flags.append(f"some independent-model disagreement ({disagreement:.1%})")
            score += 1
    else:
        gaps.append("independent model comparison unavailable")

    upset = row.get("upset_risk")
    if isinstance(upset, dict):
        upset_level = str(upset.get("level") or "").upper()
        if upset_level == "HIGH":
            flags.append("high upset risk: independent model disagreement is material")
            score += 2
            reject = True
        elif upset_level == "MEDIUM":
            flags.append("moderate upset risk; favourite outcome needs extra scrutiny")
            score += 1
        if upset.get("draw_probability") is not None:
            try:
                draw_probability = float(upset["draw_probability"])
                if draw_probability >= 0.32:
                    flags.append(f"elevated draw probability ({draw_probability:.1%})")
                    score += 1
            except (TypeError, ValueError, OverflowError):
                gaps.append("draw-risk estimate was not numeric")
    else:
        gaps.append("ensemble upset-risk assessment unavailable")

    for field, label in (("home_sample", "home-team recent form"), ("away_sample", "away-team recent form")):
        value = row.get(field)
        try:
            sample = int(value) if value is not None else None
        except (TypeError, ValueError, OverflowError):
            sample = None
        if sample is None or sample <= 0:
            gaps.append(f"{label} sample unavailable")
        elif sample < 5:
            flags.append(f"thin {label} sample ({sample})")
            score += 1

    for field, label, minimum in (
        ("home_home_sample", "home venue sample", 3),
        ("away_away_sample", "away venue sample", 3),
    ):
        value = row.get(field)
        try:
            sample = int(value) if value is not None else 0
        except (TypeError, ValueError, OverflowError):
            sample = 0
        if sample < minimum:
            gaps.append(f"{label} below {minimum} matches")

    evidence_fields = (
        ("lineup_evidence", "confirmed line-ups"),
        ("team_news_evidence", "verified injury/suspension information"),
        ("xg_evidence", "xG and shot-quality data"),
        ("odds_history_evidence", "historical odds movement"),
        ("tactical_evidence", "tactical matchup evidence"),
    )
    for field, label in evidence_fields:
        if not row.get(field):
            gaps.append(f"{label} unavailable")

    team_news = row.get("team_news_evidence")
    absences = team_news.get("reported_absences") if isinstance(team_news, dict) else None
    if absences:
        flags.append(f"{len(absences)} reported absence(s) require impact review")
        score += 1

    level = "HIGH" if reject or score >= 3 else "MEDIUM" if score >= 1 else "LOW"
    return {
        "risk_score": score,
        "risk_level": level,
        "risk_flags": flags,
        "risk_gaps": gaps,
        "risk_rejected": reject,
        "risk_method": "transparent evidence-gap and model-disagreement screen; not a trained upset probability",
    }
