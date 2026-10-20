"""Official price snapshot used for Buddy requests.

Only exact, normalized model aliases are priced.  Unknown models, unknown
cache counts and ambiguous Beijing calendar dates stay ``None`` rather than
silently becoming zero.  Every value returned here is USD.
"""
from datetime import date, datetime, timedelta, timezone
import math

from .core import PRICES, Row, cost_value

PRICE_VERSION = "2026-10-19-v1"
CHECKED = "2026-10-19"
USD_CNY = 6.8
SOURCE_URLS = (
    "https://developers.openai.com/api/docs/pricing",
    "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
    "https://www.beijing.gov.cn/cs/gncs/zcwj/202603/t20260327_4568275.html",
)
BEIJING = timezone(timedelta(hours=8))

# CNY per 1M tokens: (uncached input, cached input, output).
DEEPSEEK_PRICES = {
    "flash": {"peak": (2.0, .04, 8.0), "idle": (1.0, .02, 4.0)},
    "pro": {"peak": (9.0, .30, 27.0), "idle": (4.5, .15, 13.5)},
}
DEEPSEEK_ALIASES = {
    "deepseek-flash": "flash",
    "deepseek-v4.1-flash": "flash",
    "deepseek-v4.1-flash-1017": "flash",
    "deepseek-v4-flash": "flash",
    "deepseek-v4-flash-vision-exp": "flash",
    "deepseek-v4-pro": "pro",
    "deepseek-v4-pro-0813": "pro",
}


def _holiday_range(first, last):
    start, end = date.fromisoformat(first), date.fromisoformat(last)
    values = set()
    while start <= end:
        values.add(start)
        start += timedelta(days=1)
    return values


# Inclusive official 2026 public holidays.  Make-up weekends still count as
# weekends here because the official rule is explicitly Monday-Friday.
HOLIDAYS_2026 = frozenset().union(*(
    _holiday_range("2026-01-01", "2026-01-03"),
    _holiday_range("2026-02-15", "2026-02-23"),
    _holiday_range("2026-04-04", "2026-04-06"),
    _holiday_range("2026-05-01", "2026-05-05"),
    _holiday_range("2026-06-19", "2026-06-21"),
    _holiday_range("2026-09-25", "2026-09-27"),
    _holiday_range("2026-10-01", "2026-10-07"),
))


def model_key(model):
    """Return the exact normalized alias, never a substring match."""
    if not isinstance(model, str):
        return None
    return model.strip().lower() or None


def family(model):
    return DEEPSEEK_ALIASES.get(model_key(model))


def _time(at):
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return None
    if not math.isfinite(at):
        return None
    try:
        return datetime.fromtimestamp(float(at), BEIJING)
    except (OverflowError, OSError, ValueError):
        return None


def period(at):
    """Return ``peak``/``idle``, or ``None`` when 2026 history is required.

    Weekends are always idle, including official make-up workdays.  For dates
    outside 2026 a weekday in peak hours is ambiguous (it might be a holiday),
    so pricing stays unknown; every other out-of-calendar time is definitely
    idle.
    """
    value = _time(at)
    if value is None:
        return None
    weekday = value.weekday() < 5
    in_hours = (9 <= value.hour < 12) or (14 <= value.hour < 18)
    if not weekday:
        return "idle"
    if value.year != 2026:
        return None if in_hours else "idle"
    if value.date() in HOLIDAYS_2026:
        return "idle"
    return "peak" if in_hours else "idle"


def _counts(row):
    values = (row.get("input"), row.get("output"), row.get("cache"))
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0
           for value in values):
        return None
    inp, out, cache = values
    if cache > inp:
        return None
    return inp, out, cache


def _gpt_cost(row):
    key = model_key(row.get("model"))
    if key not in PRICES:
        return None
    counts = _counts(row)
    if counts is None:
        return None
    inp, out, cache = counts
    value = Row(key="", at=0, model=key,
                usage={"input_tokens": inp, "cached_input_tokens": cache,
                       "output_tokens": out},
                duration=None, tier="standard", tier_source="unknown")
    return cost_value(value)


def cost(row):
    """USD cost for one Buddy request, or ``None`` when not determinable."""
    name = family(row.get("model"))
    if name is not None:
        when = period(row.get("at"))
        if when is None:
            return None
        counts = _counts(row)
        if counts is None:
            return None
        inp, out, cache = counts
        uncached, cached, output = DEEPSEEK_PRICES[name][when]
        return (((inp - cache) * uncached + cache * cached + out * output)
                / 1_000_000 / USD_CNY)
    return _gpt_cost(row)


def basis(row):
    """Short Chinese description of the price basis, or ``None``."""
    name = family(row.get("model"))
    if name is not None:
        when = period(row.get("at"))
        if when is None:
            return "DeepSeek 官方快照（北京时段未知）"
        return ("DeepSeek 官方快照 2026-10-19（"
                + ("峰时" if when == "peak" else "闲时") + "，汇率6.8）")
    if model_key(row.get("model")) in PRICES:
        return "OpenAI 官方价格（标准）"
    return None


# Small aliases keep call sites readable.
buddy_cost = cost
pricing_basis = basis
