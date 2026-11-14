"""Compare matching fiscal periods, currencies, and metrics."""

from calendar import monthrange
from datetime import date


def _matches_month_end(period_end, fiscal_ends):
    # Yahoo may label a retail-calendar quarter with its calendar month end.
    if not period_end:
        return False
    end = date.fromisoformat(period_end)
    if end.day != monthrange(end.year, end.month)[1]:
        return False
    return any(0 < (end - date.fromisoformat(candidate)).days <= 6 for candidate in fiscal_ends)


def compare_consensus_to_guidance(estimates, outlook):
    if not isinstance(estimates, dict) or not isinstance(outlook, dict):
        return []
    company = outlook.get("company")
    if not isinstance(company, dict):
        return []
    comparisons = []
    for guidance in company.get("guidance", []):
        metric = guidance["metric"]
        if not guidance.get("currency") or (metric == "eps" and not guidance.get("basis")):
            continue
        period_suffix = "q" if guidance["period_type"] == "quarter" else "y"
        eligible = [
            row for row in estimates.get("periods", [])
            if row["period"].endswith(period_suffix)
            and row.get(metric, {}).get("avg") is not None
            and row[metric].get("currency") == guidance["currency"]
            and (metric != "eps" or row[metric].get("methodology") == guidance["basis"])
        ]
        fiscal_ends = guidance["period_ends"]
        matches = [row for row in eligible if row["period_end"] in fiscal_ends]
        if not matches and period_suffix == "q":
            matches = [row for row in eligible if _matches_month_end(row["period_end"], fiscal_ends)]
        if len(matches) != 1:
            continue
        value = matches[0][metric]["avg"]
        low, high = guidance["low"], guidance["high"]
        midpoint = (low + high) / 2
        if value < low:
            position, boundary = "below", low
        elif value > high:
            position, boundary = "above", high
        else:
            position, boundary = "within", value
        comparisons.append({
            "metric": metric, "fiscal_period": guidance["fiscal_period"],
            "currency": guidance["currency"], "position": position,
            "difference_vs_range": round(value - boundary, 6),
            "difference_vs_midpoint": round(value - midpoint, 6),
        })
    return comparisons
