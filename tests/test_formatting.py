from datetime import timedelta

from app.bot.control import manageable, value_options
from app.crypto import TokenCrypto
from app.formatting import (clean_number, event_message, fmt_duration, params_message, parse_tz,
                            split_message)

from .conftest import FakeOwen

DEVICE = FakeOwen().device_data
TS = 1771570607  # 20.02.2026 13:56:47 GMT+7


def rec(**kw):
    base = {"id": 1, "event_id": 5, "start_dt": TS, "end_dt": None, "message": "Не удалось поджечь",
            "data": [], "device_id": 167962, "is_critical": 1}
    base.update(kw)
    return base


def test_parse_tz():
    assert parse_tz("GMT+7:00").utcoffset(None) == timedelta(hours=7)
    assert parse_tz("GMT±0:00").utcoffset(None) == timedelta(0)
    assert parse_tz("GMT-3:30").utcoffset(None) == -timedelta(hours=3, minutes=30)
    assert parse_tz(None).utcoffset(None) == timedelta(0)


def test_duration():
    assert fmt_duration(30) == "<1 мин"
    assert fmt_duration(42 * 60) == "42 мин"
    assert fmt_duration(3 * 3600 + 5 * 60) == "3 ч 5 мин"
    assert fmt_duration(2 * 86400 + 3600) == "2 д 1 ч"


def test_clean_number():
    assert clean_number("10.00000000000000000000") == "10"
    assert clean_number("28.90000") == "28.9"
    assert clean_number("abc") == "abc"


def test_start_message_critical():
    text = event_message(rec(), "start", DEVICE, None)
    lines = text.split("\n")
    assert lines[0] == "🔴 <b>Котельная РиМ — авария</b>"
    assert lines[1] == "Не удалось поджечь"
    assert lines[-1] == "🕒 20.02.2026 13:56"
    assert "P5" not in text and "Условие" not in text


def test_start_message_info_and_company():
    text = event_message(rec(is_critical=0, message="Котельная в режиме поджига"), "start", DEVICE, "РИМ")
    assert text.startswith("🔵 <b>Котельная РиМ</b> · РИМ")


def test_end_message_interval():
    text = event_message(rec(end_dt=TS + 42 * 60), "end", DEVICE, None)
    assert text.split("\n")[0] == "✅ <b>Котельная РиМ — авария завершена</b>"
    assert text.split("\n")[-1] == "🕒 20.02.2026 13:56 → 14:38 (42 мин)"


def test_values_only_when_useful():
    data = [{"id": 1, "v": "0", "fv": "Ожидание"}, {"id": 2, "v": "96.40000", "fv": ""}]
    text = event_message(rec(message="Перегрев", data=data), "start", DEVICE, None)
    assert "Режим работы" not in text  # состояние уже отражено в названии события
    assert "Температура в водяном контуре: <b>96.4 °C</b>" in text


def test_html_escaped():
    text = event_message(rec(message="<b>x</b> & y"), "start", {"name": "A<B"}, None)
    assert "&lt;b&gt;x&lt;/b&gt; &amp; y" in text and "A&lt;B" in text


def test_params_message_groups():
    device = {"name": "К", "time_zone": "GMT+7:00", "last_dt": str(TS),
              "parameter_categories": [{"id": 9, "name": "Температура"}],
              "parameters": [{"id": 2, "name": "Вода", "category_id": 9, "formatted_value": "30.0",
                              "measurement": {"title": "°C", "visible": 1}},
                             {"id": 3, "name": "Без категории", "value": "1.000"}]}
    text = params_message(device)
    assert "<b>Температура</b>\nВода: <b>30.0 °C</b>" in text
    assert text.index("Температура") < text.index("Прочее")
    assert "Без категории: <b>1</b>" in text


def test_split_message():
    parts = split_message("\n".join(["x" * 100] * 100), limit=1000)
    assert all(len(p) <= 1000 for p in parts)
    assert sum(p.count("x" * 100) for p in parts) == 100


def test_crypto_roundtrip():
    from cryptography.fernet import Fernet
    a, b = TokenCrypto(Fernet.generate_key().decode()), TokenCrypto(Fernet.generate_key().decode())
    enc = a.encrypt("secret-token")
    assert "secret-token" not in enc
    assert a.decrypt(enc) == "secret-token"
    assert b.decrypt(enc) is None
    assert a.fingerprint("x") == b.fingerprint("x")


def test_control_options():
    params = [
        {"id": 1, "name": "Пуск", "in_manageable": 1, "is_writable": 1, "format": 2, "value_descriptions": []},
        {"id": 2, "name": "Авария", "in_manageable": 0, "is_writable": 1, "format": 2,
         "value_descriptions": [{"value": "0", "description": "Норма"}, {"value": "1", "description": "Авария"}]},
    ]
    assert [p["id"] for p in manageable({"parameters": params})] == [1]
    assert value_options(params[0]) == [("0", "0"), ("1", "1")]
    assert value_options(params[1]) == [("0", "Норма"), ("1", "Авария")]
