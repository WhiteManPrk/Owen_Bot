"""Опрос журналов событий OwenCloud и рассылка уведомлений о начале/окончании событий."""
import asyncio
import logging
import time
from collections import defaultdict

from .formatting import event_message
from .keyboards import read_button
from .notifier import Notifier
from .owen import OwenAuthError, OwenError
from .services import Services

log = logging.getLogger(__name__)

OVERLAP = 3600       # окно перекрытия: записи могут попадать в журнал с опозданием
PAGE = 500           # записей за один запрос events-log-forward
SEED_LIMIT = 100     # записей журнала при первом опросе прибора
KEEP_RECORDS = 2 * 86400
RESEED_AFTER = 6 * 3600  # прибор не опрашивался дольше — журнал перечитывается без рассылки


def matches(sub, rec: dict) -> bool:
    mode = sub["mode"]
    if mode == "all":
        return True
    if mode == "critical":
        return bool(rec.get("is_critical"))
    return rec.get("event_id") in set(sub["event_ids"])


class Poller:
    def __init__(self, svc: Services, notifier: Notifier, interval: int, access_hash: str | None = None):
        self.access_hash = access_hash
        self.svc = svc
        self.db = svc.db
        self.owen = svc.owen
        self.notifier = notifier
        self.interval = interval
        self._broken_now: set[int] = set()  # ключи, отказавшие в текущем цикле

    async def run(self) -> None:
        log.info("Поллер запущен, интервал %s с", self.interval)
        while True:
            started = time.monotonic()
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Ошибка цикла опроса")
            await asyncio.sleep(max(1.0, self.interval - (time.monotonic() - started)))

    async def tick(self) -> None:
        self._broken_now: set[int] = set()
        by_device = defaultdict(list)
        for sub in await self.db.active_subscriptions(self.access_hash):
            by_device[sub["device_id"]].append(sub)
        started = time.monotonic()
        for device_id, subs in by_device.items():
            await self.poll_device(device_id, subs)
        await self.db.cleanup_records(int(time.time()) - KEEP_RECORDS)
        log.debug("Цикл опроса: приборов %s, подписок %s, %.1f с", len(by_device),
                  sum(len(s) for s in by_device.values()), time.monotonic() - started)

    async def poll_device(self, device_id: int, subs: list) -> None:
        """Опрос через первый рабочий ключ среди подписчиков прибора."""
        tried = set()
        for sub in subs:
            conn_id = sub["connection_id"]
            if conn_id in tried or conn_id in self._broken_now:
                continue
            tried.add(conn_id)
            token = self.svc.token(sub)
            if token is None:
                await self._broken(sub, "ключ не расшифровывается (сменился ENCRYPTION_KEY)")
                continue
            try:
                events = await self.collect(token, device_id)
            except OwenAuthError:
                await self._broken(sub, "OwenCloud отклонил ключ")
                continue
            except OwenError as e:
                log.warning("Прибор %s: %s", device_id, e)
                return
            for rec, phase in events:
                log.info("Прибор %s: %s «%s» (запись %s, авария=%s)", device_id,
                         "начало" if phase == "start" else "окончание", rec.get("message"),
                         rec.get("id"), rec.get("is_critical"))
            if events:
                await self.dispatch(token, device_id, subs, events)
            return

    async def _broken(self, sub, reason: str) -> None:
        log.warning("Аккаунт %s: %s", sub["connection_id"], reason)
        self._broken_now.add(sub["connection_id"])
        await self.db.set_broken(sub["connection_id"], True)
        await self.notifier.send(
            sub["chat_id"],
            f"⚠️ Ключ OwenCloud «{sub['company']}» больше не работает ({reason}).\n"
            "Уведомления по нему остановлены. Добавьте ключ заново командой /accounts.")

    async def collect(self, token: str, device_id: int) -> list[tuple[dict, str]]:
        """Новые события прибора: [(запись журнала, 'start' | 'end')]."""
        now = int(time.time())
        state = await self.db.poll_state(device_id)
        if state is None or now - state["polled_at"] > RESEED_AFTER:
            # новый прибор или его давно никто не слушал — старые события не рассылаем
            await self._seed(token, device_id, now)
            return []
        cursor = state["cursor"]

        events: list[tuple[dict, str]] = []
        seen: set[int] = set()
        start = cursor - OVERLAP
        while True:
            recs = await self.owen.log_forward(token, device_id, start, PAGE)
            fresh = [r for r in recs if r["id"] not in seen]
            seen.update(r["id"] for r in fresh)
            known = await self.db.known_records([r["id"] for r in fresh]) if fresh else {}
            for r in fresh:
                ended_now = r.get("end_dt") is not None
                if r["id"] not in known:
                    # событие, начавшееся и закончившееся между опросами, — одним сообщением «окончание»
                    events.append((r, "end" if ended_now else "start"))
                    await self.db.add_record(r["id"], device_id, r["start_dt"], ended_now)
                elif ended_now and not known[r["id"]]:
                    events.append((r, "end"))
                    await self.db.mark_ended(r["id"])
                cursor = max(cursor, int(r["start_dt"]))
            if len(recs) < PAGE or not recs or int(recs[-1]["start_dt"]) <= start:
                break
            start = int(recs[-1]["start_dt"])

        # открытые записи, которые выпали из окна перекрытия
        stale = [i for i in await self.db.open_records(device_id) if i not in seen]
        for i in range(0, len(stale), 100):
            chunk = stale[i:i + 100]
            found = {r["id"]: r for r in await self.owen.log_by_ids(token, device_id, chunk)}
            for log_id in chunk:
                r = found.get(log_id)
                if r is None:
                    await self.db.mark_ended(log_id)  # запись удалена из журнала
                elif r.get("end_dt") is not None:
                    r.setdefault("device_id", device_id)
                    events.append((r, "end"))
                    await self.db.mark_ended(log_id)

        await self.db.set_cursor(device_id, cursor, now)
        log.debug("Прибор %s: записей в окне %s, открытых проверено %s, событий %s, курсор %s",
                  device_id, len(seen), len(stale), len(events), cursor)
        events.sort(key=lambda e: int(e[0]["start_dt"] if e[1] == "start" else e[0]["end_dt"]))
        return events

    async def _seed(self, token: str, device_id: int, now: int) -> None:
        """Первый опрос: запоминаем текущее состояние журнала, ничего не рассылая."""
        recs = await self.owen.log_backward(token, device_id, now + OVERLAP, SEED_LIMIT)
        for r in recs:
            await self.db.add_record(r["id"], device_id, r["start_dt"], r.get("end_dt") is not None)
        cursor = max((int(r["start_dt"]) for r in recs), default=now)
        await self.db.set_cursor(device_id, cursor, now)
        log.info("Прибор %s: начальная загрузка журнала, записей %s", device_id, len(recs))

    async def dispatch(self, token: str, device_id: int, subs: list, events: list[tuple[dict, str]]) -> None:
        try:
            device = await self.svc.device_info(token, device_id)
        except OwenError as e:
            log.warning("Прибор %s: нет описания (%s), уведомления без имён параметров", device_id, e)
            device = None
        for sub in subs:
            if sub["connection_id"] in self._broken_now:
                continue
            company = sub["company"] if sub["chat_connections"] > 1 else None
            for rec, phase in events:
                if not matches(sub, rec):
                    continue
                text = event_message(rec, phase, device, company)
                markup = read_button(sub["connection_id"], rec["id"]) if rec.get("read_dt") is None else None
                ok = await self.notifier.send(sub["chat_id"], text, markup)
                log.debug("Уведомление в чат %s: запись %s, %s — %s", sub["chat_id"], rec["id"], phase,
                          "отправлено" if ok else "НЕ отправлено")
