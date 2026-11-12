"""Format market evidence for the model and the reader."""

import re

import pandas as pd

from tradingagents.dataflows.vendors.yahoo.candles import DailyCandle
from tradingagents.dataflows.vendors.yahoo.snapshot import _fmt
from tradingagents.technical_analysis.indicators import trend_comparisons
from tradingagents.technical_analysis.zones import ZoneEvidence


def _last_completed_date(rows):
    if rows is None or rows.empty:
        return None
    return _fmt(rows["Date"].iloc[-1])


def add_candle_disclosure(report, candle: DailyCandle, completed_rows, latest_price, *, language):
    """Place a readable candle summary beneath the first report heading."""
    turkish = language.strip().casefold() in {"tr", "turkish", "türkçe"}
    if turkish:
        title = "Günlük mum bilgisi"
        labels = ("Mum tarihi", "Mum durumu", "Veri zamanı", "Son tamamlanmış mum", "Son gözlenen fiyat")
        statuses = {True: "Kapandı", False: "Henüz kapanmadı", None: "Kapanışı doğrulanamadı"}
        sources = {"vendor regular-market timestamp": "piyasa veri zamanı", "data fetch time": "verinin alındığı zaman"}
        unavailable = "Veri yok"
    else:
        title = "Daily candle information"
        labels = ("Candle date", "Candle status", "Data timestamp", "Last completed candle", "Last observed price")
        statuses = {True: "Closed", False: "Still open", None: "Close unverified"}
        sources = {}
        unavailable = "Unavailable"

    timestamp = unavailable
    if candle.as_of_timestamp:
        timestamp = candle.as_of_timestamp.replace("T", " ", 1)
        source = sources.get(candle.timestamp_basis, candle.timestamp_basis)
        details = ", ".join(value for value in (candle.timezone, source) if value)
        if details:
            timestamp += f" ({details})"
    values = [
        candle.date or unavailable,
        statuses[candle.closed] if candle.date else unavailable,
        timestamp,
        _last_completed_date(completed_rows) or unavailable,
    ]
    if candle.date and candle.closed is not True and pd.notna(latest_price):
        values.append(_fmt(latest_price))

    lines = "\n".join(f"- **{label}:** {value}" for label, value in zip(labels[:len(values)], values, strict=True))
    disclosure = f"**{title}**\n\n{lines}"
    heading = re.match(r"^\s*#{1,6}[ \t]+[^\n]+\n", report)
    if heading:
        index = heading.end()
        return f"{report[:index]}\n{disclosure}\n\n{report[index:].lstrip()}"
    return f"{disclosure}\n\n{report}"


def render_zones(zones: list[ZoneEvidence], *, language="English") -> str:
    """Show one line per zone for the model and the user's report."""
    turkish = language.strip().casefold() in {"tr", "turkish", "türkçe"}
    labels = {"support": "Destek", "resistance": "Direnç"} if turkish else {
        "support": "Support", "resistance": "Resistance",
    }
    lines = []
    for zone in zones:
        lower, upper = zone.bounds
        summary = f"- **{labels[zone.side]} ({zone.period} bar):** {_fmt(lower)}–{_fmt(upper)}."
        if turkish:
            summary += f" İlk oluşum {zone.formation_date}."
            summary += (
                f" Sonraki temas sayısı {zone.contact_count}."
                if zone.contact_count else " Sonraki temas görülmedi."
            )
        else:
            summary += f" First formed {zone.formation_date}."
            summary += (
                f" Subsequent contacts: {zone.contact_count}."
                if zone.contact_count else " No subsequent contact."
            )
        lines.append(summary)
    return "\n".join(lines)


def price_context(rows, count):
    fields = ["Date", "Open", "High", "Low", "Close", "Volume"]
    return "\n".join(
        [",".join(fields)]
        + [",".join(_fmt(value) for value in row)
           for row in rows[fields].tail(count).itertuples(index=False, name=None)]
    )


def indicator_context(rows, groups, calculated):
    dates = rows["Date"].tail(10)
    output = []
    for specs in groups.values():
        for spec in specs:
            result = calculated.get(spec.name)
            if result is None:
                output.append(f"{spec.name}: could not be calculated.")
                continue
            series = [
                f"{_fmt(date)}={_fmt(value)}"
                for date, value in zip(dates, result.values.tail(10), strict=True)
            ]
            output.append(f"{spec.display_label}: " + ", ".join(series))
            output.extend(result.metrics)
    output.extend(trend_comparisons(groups, {name: result.values for name, result in calculated.items()}, dates))
    return output


def unavailable_report(reason: str, language: str, candle: DailyCandle | None = None, latest_price=None):
    turkish = language.strip().casefold() in {"tr", "turkish", "türkçe"}
    explanations = {
        "no_market_data": "Yahoo Finance, seçilen tarih için kullanılabilir günlük fiyat verisi döndürmedi.",
        "provider_unavailable": "Yahoo Finance’a erişilemedi. Günlük fiyat verisi alınamadı.",
        "no_completed_candle": "Mevcut günlük mumun kapanışı doğrulanamadı. Hesaplamaya uygun tamamlanmış mum yok.",
    } if turkish else {
        "no_market_data": "Yahoo Finance returned no usable daily prices for the selected date.",
        "provider_unavailable": "Yahoo Finance was unavailable. Daily prices could not be retrieved.",
        "no_completed_candle": "The available daily candle could not be verified as closed. No completed candles are available.",
    }
    heading = "Teknik analiz verisi yok" if turkish else "Technical analysis unavailable"
    report = f"## {heading}\n\n{explanations[reason]}"
    if candle is not None:
        report = add_candle_disclosure(report, candle, None, latest_price, language=language)
    return report


_DECISION_LINE = re.compile(
    r"^\s*(?:[-+>]|#{1,6}|\d+\.)?\s*"
    r"(?:rating|final transaction proposal|derecelendirme|nihai işlem önerisi)\s*[:：]",
    re.IGNORECASE,
)


# Match a standalone metadata recap while leaving dated price commentary intact.
_CANDLE_RECAP = re.compile(
    r"^\s*(?:[\w.^=/+-]+\s*[—–-]\s*)?(?:analiz tarihi|analysis date)\s*:\s*\d{4}-\d{2}-\d{2}\s*;"
    r"\s*(?:değerlendirme ufku|analysis horizon|evaluation horizon)\s*:[^.]+\."
    r"\s*(?:son tamamlanmış günlük (?:bar|mum)|(?:the )?last completed daily (?:bar|candle))[^.]+\."
    r"(?:\s*(?:bar kapanmıştır|the (?:bar|candle) is closed)\.)?"
    r"\s*(?:veri zaman damgası|data timestamp)\s*:\s*[0-9T:+-]+(?:,\s*[\w./+-]+)?\.?\s*$",
    re.IGNORECASE,
)


def render_market_report(report, candle, completed, latest_price, zones, *, language):
    lines = []
    for line in report.splitlines():
        plain = re.sub(r"[*_`]", "", line)
        if not (_DECISION_LINE.match(plain) or _CANDLE_RECAP.fullmatch(plain)):
            lines.append(line)
    report = "\n".join(lines).strip()
    report = add_candle_disclosure(report, candle, completed, latest_price, language=language)
    turkish = language.strip().casefold() in {"tr", "turkish", "türkçe"}
    title = "SciPy destek ve direnç bölgeleri" if turkish else "SciPy support and resistance zones"
    if zones:
        note = (
            "Yerel tepe ve diplerden çıkarılan yakın veya yeni oluşmuş bölgeler. Son kapanışın altındakiler destek, "
            "üstündekiler direnç adayıdır. Kapanış bölge içindeyse tepe veya dip kaynaklı sınıflandırma gösterilir."
            if turkish else
            "Nearby or recently formed zones from local highs and lows. Zones below the latest close are support candidates "
            "and zones above it are resistance candidates. A zone containing the close keeps its original pivot type."
        )
        body = "\n\n".join([note, render_zones(zones, language=language)])
    else:
        body = (
            "Tamamlanmış mumlardan ve geçerli ATR değerlerinden aday bölge çıkarılamadı."
            if turkish else "No candidate zones found with completed candles and valid ATR values."
        )
    return "\n\n".join([report.rstrip(), f"### {title}", body])
