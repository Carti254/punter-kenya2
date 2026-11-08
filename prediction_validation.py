"""Evaluation and calibration primitives for historical football forecasts.

These functions do not claim a model is calibrated; they measure that only when
walk-forward historical predictions and settled outcomes are supplied.
"""
import math


def _validate(probabilities, actual):
    if len(probabilities) != len(actual) or not probabilities:
        raise ValueError("Probabilities and outcomes must have the same non-zero length.")
    rows = []
    for vector, outcome in zip(probabilities, actual):
        if len(vector) != 3 or outcome not in (0, 1, 2):
            raise ValueError("Each forecast must contain 3 probabilities and an outcome index 0, 1 or 2.")
        values = [float(x) for x in vector]
        if any(not math.isfinite(x) or x < 0 or x > 1 for x in values):
            raise ValueError("Probabilities must be finite and between 0 and 1.")
        total = sum(values)
        if total <= 0:
            raise ValueError("A forecast cannot have zero total probability.")
        rows.append(([x / total for x in values], int(outcome)))
    return rows


def brier_score(probabilities, actual):
    rows = _validate(probabilities, actual)
    return sum(sum((p[i] - (1.0 if i == y else 0.0)) ** 2 for i in range(3)) for p, y in rows) / len(rows)


def log_loss(probabilities, actual, floor=1e-15):
    rows = _validate(probabilities, actual)
    return -sum(math.log(max(floor, p[y])) for p, y in rows) / len(rows)


def reliability_bins(probabilities, actual, bins=10):
    if bins < 2:
        raise ValueError("At least two reliability bins are required.")
    rows = _validate(probabilities, actual)
    output = []
    for outcome in range(3):
        for index in range(bins):
            low, high = index / bins, (index + 1) / bins
            selected = [(p[outcome], 1.0 if y == outcome else 0.0) for p, y in rows
                        if low <= p[outcome] < high or (index == bins - 1 and p[outcome] == 1)]
            if selected:
                output.append({
                    "outcome": outcome, "lower": low, "upper": high,
                    "count": len(selected),
                    "mean_probability": sum(x[0] for x in selected) / len(selected),
                    "observed_frequency": sum(x[1] for x in selected) / len(selected),
                })
    return output


def apply_temperature(probabilities, temperature):
    if not math.isfinite(float(temperature)) or float(temperature) <= 0:
        raise ValueError("Temperature must be positive and finite.")
    p = _validate([probabilities], [0])[0][0]
    logits = [math.log(max(1e-15, value)) / float(temperature) for value in p]
    peak = max(logits)
    exp_values = [math.exp(value - peak) for value in logits]
    total = sum(exp_values)
    return [value / total for value in exp_values]


def fit_temperature(probabilities, actual, minimum_samples=50):
    """Grid-fit a scalar temperature; caller must use a chronological holdout."""
    rows = _validate(probabilities, actual)
    if len(rows) < minimum_samples:
        raise ValueError(f"At least {minimum_samples} settled forecasts are required; got {len(rows)}.")
    best = None
    for step in range(10, 401):
        temperature = step / 100
        loss = log_loss([apply_temperature(p, temperature) for p, _ in rows], [y for _, y in rows])
        if best is None or loss < best["log_loss"]:
            best = {"temperature": temperature, "log_loss": loss}
    return best
