"""Adjusted daily close adapter for backend decision evaluation.

Vendor dependencies stay in the existing data layer. Empty/missing evidence is
not filled, forward-filled or converted to zero. No credentials are required.
"""

from datetime import timedelta

from .errors import VendorUnavailableError


class AdjustedCloseSource:
    def closes(self, ticker, start, end):
        import yfinance as yf

        try:
            frame = yf.download(
                ticker,
                start=start.isoformat(),
                end=(end + timedelta(days=1)).isoformat(),
                interval="1d",
                auto_adjust=True,
                progress=False,
                threads=False,
                timeout=20,
            )
            if frame.empty:
                return {}
            column = frame["Close"]
            if getattr(column, "ndim", 1) == 2:
                column = column[ticker]
            return {stamp.date(): float(value) for stamp, value in column.items()}
        except Exception:
            raise VendorUnavailableError("Evaluation price source unavailable") from None
