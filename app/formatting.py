import re
from datetime import datetime, timedelta, timezone
from html import escape

_TZ_RE = re.compile(r"GMT\s*([+\-±])?\s*(\d{1,2})(?::(\d{2}))?")


def parse_tz(value: str | None) -> timezone:
    """'GMT+7:00' / 'GMT±0:00' / 'GMT-3:30' -> timezone."""
    m = _TZ_RE.search(value or "")
    if not m:
        return timezone.utc
    sign = -1 if m.group(1) == "-" else 1
    return timezone(sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0)))


def _dt(ts, tz: timezone) -> datetime:
    return datetime.fromtimestamp(int(ts), tz)


def fmt_ts(ts, tz: timezone, seconds: bool = False) -> str:
    if ts in (None, "", 0, "0"):
        return "—"
    return _dt(ts, tz).strftime("%d.%m.%Y %H:%M:%S" if seconds else "%d.%m.%Y %H:%M")


def fmt_duration(seconds: int) -> str:
    minutes = max(seconds, 0) // 60
    if minutes < 1:
        return "<1 мин"
    days, rem = divmod(minutes, 1440)
    hours, mins = divmod(rem, 60)
    if days:
        return f"{days} д {hours} ч" if hours else f"{days} д"
    if hours:
        return f"{hours} ч {mins} мин" if mins else f"{hours} ч"
    return f"{mins} мин"


def fmt_interval(start, end, tz: timezone) -> str:
    """'14:32 → 15:14 (42 мин)'; дата добавляется, если событие не сегодняшнее или переходит через сутки."""
    s, e = _dt(start, tz), _dt(end, tz)
    today = datetime.now(tz).date()
    if s.date() == e.date() == today:
        text = f"{s:%H:%M} → {e:%H:%M}"
    elif s.date() == e.date():
        text = f"{s:%d.%m.%Y} {s:%H:%M} → {e:%H:%M}"
    else:
        text = f"{s:%d.%m %H:%M} → {e:%d.%m %H:%M}"
    return f"{text} ({fmt_duration(int(end) - int(start))})"


def clean_number(value) -> str:
    """'10.00000000000000000000' -> '10', '28.90000' -> '28.9'."""
    s = str(value if value is not None else "—")
    if re.fullmatch(r"-?\d+\.\d+", s):
        s = s.rstrip("0").rstrip(".")
    return s


def _unit(p: dict) -> str:
    m = p.get("measurement") or {}
    return (m.get("title") or "").strip() if m.get("visible") else ""


def _useful_values(rec: dict, params: dict[int, dict]) -> list[str]:
    """Значения параметров, которые добавляют информацию к названию события.

    Параметры-состояния (с описаниями значений: «Норма/Авария», «Ожидание/Поджиг») уже
    отражены в названии события — их не показываем. Показываем измерения (температура и т.п.).
    """
    message = (rec.get("message") or "").lower()
    lines = []
    for item in rec.get("data") or []:
        p = params.get(item.get("id"))
        if p is None or p.get("value_descriptions"):
            continue
        value = item.get("fv") or clean_number(item.get("v"))
        if str(value).lower() in message:
            continue
        unit = _unit(p)
        lines.append(f"{escape(p['name'])}: <b>{escape(str(value))}{' ' + escape(unit) if unit else ''}</b>")
    return lines


def event_message(rec: dict, phase: str, device: dict | None, company: str | None) -> str:
    """Уведомление о начале (phase='start') или окончании ('end') события из журнала.

    company — показывается, только если передано (в чате несколько аккаунтов).
    """
    critical = bool(rec.get("is_critical"))
    tz = parse_tz((device or {}).get("time_zone"))
    device_name = (device or {}).get("name") or f"Прибор #{rec.get('device_id')}"

    if phase == "start":
        icon, suffix = ("🔴", " — авария") if critical else ("🔵", "")
    else:
        icon, suffix = ("✅", " — авария завершена") if critical else ("⚪", " — завершено")
    header = f"{icon} <b>{escape(device_name)}{suffix}</b>"
    if company:
        header += f" · {escape(company)}"

    lines = [header, escape(rec.get("message") or "")]
    if phase == "start":
        lines += _useful_values(rec, {p["id"]: p for p in (device or {}).get("parameters", [])})
        lines.append(f"🕒 {fmt_ts(rec.get('start_dt'), tz)}")
    else:
        lines.append(f"🕒 {fmt_interval(rec['start_dt'], rec['end_dt'], tz)}")
    return "\n".join(lines)


def param_value(p: dict) -> str:
    value = p.get("formatted_value")
    if value in (None, ""):
        value = clean_number(p.get("value"))
    unit = _unit(p)
    if unit:
        value = f"{value} {unit}"
    if p.get("fault"):
        value = f"{value} ⚠️"
    return str(value)


def params_message(device: dict) -> str:
    tz = parse_tz(device.get("time_zone"))
    params = [p for p in device.get("parameters", []) if p.get("in_parameters", 1)] \
        or device.get("parameters", [])
    categories = {c["id"]: c["name"] for c in device.get("parameter_categories", [])}

    groups: dict[str, list[dict]] = {}
    for p in params:
        groups.setdefault(categories.get(p.get("category_id"), "Прочее"), []).append(p)

    lines = [f"📊 <b>{escape(device.get('name', ''))}</b>",
             f"🕒 данные на {fmt_ts(device.get('last_dt'), tz, seconds=True)}"]
    for name in sorted(groups, key=lambda n: (n == "Прочее", n)):
        lines.append(f"\n<b>{escape(name)}</b>")
        for p in sorted(groups[name], key=lambda p: (p.get("list_order") or 0, p.get("name", ""))):
            lines.append(f"{escape(p.get('name', ''))}: <b>{escape(param_value(p))}</b>")
    return "\n".join(lines)


STATUS_ICONS = {"alarm": "🔴", "unreadalarm": "🟠", "online": "🟢", "offline": "⚫"}
STATUS_TEXT = {
    "alarm": "авария",
    "unreadalarm": "есть непрочитанные аварии",
    "online": "на связи",
    "offline": "не на связи",
}


def status_icon(dev: dict) -> str:
    return STATUS_ICONS.get(str(dev.get("status")), "⚪")


def split_message(text: str, limit: int = 4000) -> list[str]:
    parts, current = [], ""
    for line in text.split("\n"):
        if current and len(current) + len(line) + 1 > limit:
            parts.append(current)
            current = ""
        current += ("\n" if current else "") + line
    if current:
        parts.append(current)
    return parts
