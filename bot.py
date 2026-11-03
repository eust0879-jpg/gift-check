#!/usr/bin/env python3
"""Gift radar: daily floor prices of Telegram gift collections -> plain signals -> a Telegram channel.

Free by design: runs on GitHub Actions, reads open data (TG Gifts, CC BY 4.0), posts via the Telegram Bot API.
Modes (env BOT_MODE): preview (default, sends nothing), diag, chats, say, run.
Nothing is sent to Telegram unless ALLOW_SEND=yes. The bot token comes only from the environment and is never printed.
"""

from __future__ import annotations

import collections
import dataclasses
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "giftradar/0.1 (free daily gift floor signals)"
API = "https://api.telegram.org"
BARS = "▁▂▃▄▅▆▇█"
HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULTS = {
    "data_url": "https://tggifts.com/data/floors-daily.json",
    "data_fallback_url": "https://raw.githubusercontent.com/w3radar/telegram-gift-floors/main/data/floors-daily.json",
    "unit": "GRAM",
    "min_floor": 30,  # cheaper collections are skipped: one price step already looks like a big move
    "min_sales": 10,  # recent sales visible on the collection page (the source caps it at 60)
    "min_days": 8,  # points of history needed
    "min_abs_move": 3,  # and the move must be at least this many units
    "up_pct": 15,
    "down_pct": 20,
    "ref_days": 7,
    "breakout_days": 14,
    "max_cards": 8,
    "cooldown_days": 3,
    "digest_top": 5,
    "stale_days": 2,
    "attribution_name": "TG Gifts",
    "attribution_url": "https://tggifts.com",
}


# ---------------------------------------------------------------- small helpers
def mask(text: str, token: str = "") -> str:
    if token:
        text = text.replace(token, "***")
    return re.sub(r"(?<!\d)\d{6,}:[A-Za-z0-9_-]{20,}", "***", text)


def fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.2f}".rstrip("0").rstrip(".")


def esc(text: str) -> str:
    return html.escape(str(text), quote=False)


def spark(values: list) -> str:
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return BARS[3] * len(values)
    return "".join(BARS[min(7, int((v - lo) / (hi - lo) * 7.999))] for v in values)


def host_of(url: str) -> str:
    return urllib.parse.urlparse(url).hostname or url[:40]


def load_config(path: str) -> tuple:
    cfg = dict(DEFAULTS)
    problems: list = []
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return cfg, problems
    except (ValueError, UnicodeDecodeError) as exc:
        return cfg, [f"config.json не читается ({exc}), взяты стандартные значения"]
    if not isinstance(data, dict):
        return cfg, ["config.json: ожидался словарь, взяты стандартные значения"]
    for key, default in DEFAULTS.items():
        if key not in data:
            continue
        value = data[key]
        if isinstance(default, str):
            ok = isinstance(value, str) and (value.strip() or key == "data_fallback_url")
        else:
            ok = isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
        if ok:
            cfg[key] = value.strip() if isinstance(value, str) else value
        else:
            problems.append(f"config.json: значение «{key}» не подходит, оставлено {default!r}")
    return cfg, problems


# ---------------------------------------------------------------- data and signals
def parse_collections(data) -> dict:
    """{slug: {"name", "days": [(date, floor, sales)]}} from the TG Gifts daily file. Bad rows are skipped."""
    out: dict = {}
    cols = data.get("collections") if isinstance(data, dict) else None
    if not isinstance(cols, dict):
        return out
    for slug, item in cols.items():
        if not isinstance(item, dict):
            continue
        by_day: dict = {}
        for row in item.get("days") or []:
            try:
                day = dt.date.fromisoformat(str(row[0]))
                floor = float(row[1])
                sales = int(row[2]) if len(row) > 2 and row[2] is not None else 0
            except (ValueError, TypeError, IndexError):
                continue
            if floor > 0:
                by_day[day] = (day, floor, sales)
        if by_day:
            out[str(slug)] = {"name": str(item.get("name") or slug), "days": [by_day[d] for d in sorted(by_day)]}
    return out


@dataclasses.dataclass
class Metrics:
    slug: str
    name: str
    day: dt.date
    last: float
    ref: float
    pct: float
    sales: int
    n_days: int
    spark: str
    kind: str  # "up", "down", "breakout" or "" (no signal)
    breakout: bool

    @property
    def key(self) -> str:
        return f"{self.slug}:{self.kind}"


def evaluate(slug: str, item: dict, cfg: dict) -> tuple:
    """Returns (Metrics, "") or (None, reason it was left out)."""
    days = item["days"]
    day, last, sales = days[-1]
    if len(days) < cfg["min_days"]:
        return None, "мало истории"
    if last < cfg["min_floor"]:
        return None, "слишком дешёвая"
    if sales < cfg["min_sales"]:
        return None, "мало продаж"
    limit = day - dt.timedelta(days=int(cfg["ref_days"]))
    ref = None
    for d, floor, _ in days:
        if d <= limit:
            ref = floor
    if ref is None:
        return None, "нет точки для сравнения"
    pct = (last / ref - 1) * 100
    big_enough = abs(last - ref) >= cfg["min_abs_move"]
    kind = ""
    if big_enough and pct >= cfg["up_pct"]:
        kind = "up"
    elif big_enough and pct <= -cfg["down_pct"]:
        kind = "down"
    window = [f for d, f, _ in days if day - dt.timedelta(days=int(cfg["breakout_days"])) <= d < day]
    breakout = len(window) >= 3 and last - max(window) >= cfg["min_abs_move"] and last > max(window)
    if not kind and breakout:
        kind = "breakout"
    tail = [f for _, f, _ in days[-14:]]
    return Metrics(slug, item["name"], day, last, ref, pct, sales, len(days), spark(tail), kind, breakout), ""


def evaluate_all(series: dict, cfg: dict) -> tuple:
    metrics: list = []
    reasons: collections.Counter = collections.Counter()
    for slug, item in series.items():
        m, reason = evaluate(slug, item, cfg)
        if m is None:
            reasons[reason] += 1
        else:
            metrics.append(m)
    return metrics, reasons


def pick_signals(metrics: list, state: dict, cfg: dict, data_day: dt.date) -> list:
    sent = state.get("sent", {})
    fresh = []
    for m in metrics:
        if not m.kind:
            continue
        when = sent.get(m.key)
        if when:
            try:
                if (data_day - dt.date.fromisoformat(when)).days < cfg["cooldown_days"]:
                    continue
            except ValueError:
                pass
        fresh.append(m)
    fresh.sort(key=lambda m: -abs(m.pct))
    return fresh[: int(cfg["max_cards"])]


# ---------------------------------------------------------------- message texts
def footer(cfg: dict) -> str:
    return (
        "<i>Наблюдение по открытым данным Fragment, не инвестиционный совет.</i>\n"
        f'Данные: <a href="{esc(cfg["attribution_url"])}">{esc(cfg["attribution_name"])}</a>, CC BY 4.0'
    )


def card_text(m: Metrics, cfg: dict) -> str:
    unit = cfg["unit"]
    days = int(cfg["ref_days"])
    if m.kind == "up":
        head = f"📈 <b>{esc(m.name)}</b>: рост {m.pct:.1f}% за {days} дн."
    elif m.kind == "down":
        head = f"📉 <b>{esc(m.name)}</b>: падение {abs(m.pct):.1f}% за {days} дн."
    else:
        head = f"🔺 <b>{esc(m.name)}</b>: новый максимум за {int(cfg['breakout_days'])} дн."
    sales = f"{m.sales}+" if m.sales >= 60 else str(m.sales)
    lines = [
        head,
        f"Нижняя цена: <b>{fmt(m.last)} {esc(unit)}</b> ({days} дн. назад: {fmt(m.ref)})",
        m.spark,
        f"Недавних продаж на Fragment: {sales} · история: {m.n_days} дн. · данные на {m.day:%d.%m.%Y}",
    ]
    if m.breakout and m.kind != "breakout":
        lines.append(f"🔺 Это и новый максимум за {int(cfg['breakout_days'])} дн.")
    lines += ["", footer(cfg)]
    return "\n".join(lines)


def digest_text(metrics: list, n_total: int, cfg: dict, data_day: dt.date) -> str:
    top = int(cfg["digest_top"])
    ups = sorted([m for m in metrics if m.pct > 0], key=lambda m: -m.pct)[:top]
    downs = sorted([m for m in metrics if m.pct < 0], key=lambda m: m.pct)[:top]

    def rows(items: list) -> list:
        if not items:
            return ["• нет"]
        return [f"• {esc(m.name)}: {m.pct:+.1f}% ({fmt(m.ref)} → {fmt(m.last)} {esc(cfg['unit'])})" for m in items]

    days = int(cfg["ref_days"])
    return "\n".join(
        [
            f"📊 <b>Итог дня</b> · данные на {data_day:%d.%m.%Y}",
            f"В наблюдении {len(metrics)} из {n_total} коллекций. Остальные отсеяны: слишком дешёвые, редкие в продажах или с короткой историей.",
            "",
            f"Сильнее всего выросли за {days} дн.:",
            *rows(ups),
            "",
            f"Сильнее всего упали за {days} дн.:",
            *rows(downs),
            "",
            footer(cfg),
        ]
    )


# ---------------------------------------------------------------- state
def load_state(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, UnicodeDecodeError):
        return {}


def save_state(path: str, state: dict, data_day: dt.date | None) -> None:
    if data_day:
        cutoff = data_day - dt.timedelta(days=60)
        kept = {}
        for key, when in state.get("sent", {}).items():
            try:
                if dt.date.fromisoformat(when) >= cutoff:
                    kept[key] = when
            except ValueError:
                pass
        state["sent"] = kept
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- Telegram
class TgError(Exception):
    pass


class Bot:
    def __init__(self, cfg: dict, env: dict, opener=urllib.request.urlopen, sleep=time.sleep, now=None, state_path="state.json"):
        self.cfg = cfg
        self.env = env
        self.opener = opener
        self.sleep = sleep
        self.now = now or (lambda: dt.datetime.now(dt.timezone.utc))
        self.state_path = state_path
        self.token = env.get("TELEGRAM_TOKEN", "").strip()
        chat = env.get("TELEGRAM_CHAT_ID", "").strip()
        self.chat = int(chat) if re.fullmatch(r"-?\d+", chat) else chat
        thread = env.get("TELEGRAM_THREAD_ID", "").strip()
        self.thread = int(thread) if thread.isdigit() else None
        self.allow_send = env.get("ALLOW_SEND", "").strip().lower() == "yes"
        self.lines: list = []

    # ---- output
    def out(self, text: str = "") -> None:
        text = mask(text, self.token)
        print(text)
        self.lines.append(text)

    # ---- network
    def tg(self, method: str, payload: dict, tries: int = 3) -> dict:
        if not self.token:
            raise TgError("токен бота не задан (секрет TELEGRAM_TOKEN)")
        request = urllib.request.Request(
            f"{API}/bot{self.token}/{method}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": UA},
            method="POST",
        )
        for attempt in range(tries):
            try:
                with self.opener(request, timeout=30) as response:
                    data = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", errors="replace")
                try:
                    info = json.loads(raw)
                except ValueError:
                    info = {}
                if exc.code == 429 and attempt < tries - 1:
                    self.sleep(min(int((info.get("parameters") or {}).get("retry_after", 5)), 60))
                    continue
                raise TgError(mask(f"{method}: HTTP {exc.code}: {info.get('description') or raw[:200]}", self.token))
            except (urllib.error.URLError, OSError, ValueError) as exc:
                if attempt < tries - 1:
                    self.sleep(2)
                    continue
                raise TgError(mask(f"{method}: {type(exc).__name__}: {exc}", self.token))
            if not data.get("ok"):
                raise TgError(mask(f"{method}: {data.get('description') or data}", self.token))
            return data
        raise TgError(f"{method}: не удалось выполнить запрос")

    def send(self, text: str) -> int:
        if not self.allow_send:
            raise TgError("отправка не разрешена (ALLOW_SEND не yes)")
        if self.chat in ("", None):
            raise TgError("чат не задан (секрет TELEGRAM_CHAT_ID)")
        payload = {"chat_id": self.chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if self.thread:
            payload["message_thread_id"] = self.thread
        return int(self.tg("sendMessage", payload)["result"]["message_id"])

    def load_data(self) -> tuple:
        error = "нет адреса источника"
        for url in (self.cfg["data_url"], self.cfg["data_fallback_url"]):
            if not url:
                continue
            try:
                request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
                with self.opener(request, timeout=30) as response:
                    raw = response.read(10_000_000)
                series = parse_collections(json.loads(raw.decode("utf-8")))
                if series:
                    return series, url, ""
                error = "в ответе нет коллекций"
            except urllib.error.HTTPError as exc:
                error = f"HTTP {exc.code}"
            except (urllib.error.URLError, OSError, ValueError) as exc:
                error = f"{type(exc).__name__}: {exc}"
            self.out(f"Источник {host_of(url)} не подошёл: {error}")
        return {}, "", error

    # ---- modes
    def analysis(self, series: dict) -> tuple:
        metrics, reasons = evaluate_all(series, self.cfg)
        data_day = max(item["days"][-1][0] for item in series.values())
        return metrics, reasons, data_day

    def stats_lines(self, series, metrics, reasons, data_day, url) -> list:
        left = ", ".join(f"{k}: {v}" for k, v in reasons.most_common()) or "нет"
        age = (self.now().date() - data_day).days
        return [
            f"Источник: {host_of(url)}; коллекций с историей: {len(series)}; последняя дата в данных: {data_day} (отстаёт на {age} дн.)",
            f"В наблюдении: {len(metrics)}. Отсеяно: {left}",
        ]

    def mode_preview(self, announce_off: bool = False) -> int:
        series, url, error = self.load_data()
        if not series:
            self.out(f"Данные недоступны: {error}")
            return 1
        metrics, reasons, data_day = self.analysis(series)
        state = load_state(self.state_path)
        for line in self.stats_lines(series, metrics, reasons, data_day, url):
            self.out(line)
        if announce_off:
            self.out("Отправка выключена (ALLOW_SEND не yes): показываю только то, что было бы отправлено.")
        signals = pick_signals(metrics, state, self.cfg, data_day)
        self.out(f"\nСигналов к отправке: {len(signals)} (лимит {int(self.cfg['max_cards'])} за запуск)")
        for m in signals:
            self.out("\n--- карточка ---\n" + card_text(m, self.cfg))
        self.out("\n--- итог дня ---\n" + digest_text(metrics, len(series), self.cfg, data_day))
        return 0

    def warn_once(self, state: dict, text: str) -> None:
        today = self.now().date().isoformat()
        if state.get("warned") == today:
            return
        self.out("Предупреждение: " + text)
        if self.allow_send and self.chat:
            try:
                self.send("⚠️ " + esc(text))
                state["warned"] = today
            except TgError as exc:
                self.out(f"Предупреждение не отправлено: {exc}")

    def mode_run(self) -> int:
        if not self.allow_send:
            return self.mode_preview(announce_off=True)
        state = load_state(self.state_path)
        data_day = None
        code = 0
        try:
            series, url, error = self.load_data()
            if not series:
                self.warn_once(state, f"данные о ценах недоступны ({error}).")
                return 1
            metrics, reasons, data_day = self.analysis(series)
            for line in self.stats_lines(series, metrics, reasons, data_day, url):
                self.out(line)
            if (self.now().date() - data_day).days > int(self.cfg["stale_days"]):
                self.warn_once(state, f"данные о ценах не обновлялись с {data_day:%d.%m.%Y}.")
                return 0
            sent = state.setdefault("sent", {})
            for m in pick_signals(metrics, state, self.cfg, data_day):
                self.send(card_text(m, self.cfg))
                sent[m.key] = data_day.isoformat()
                self.out(f"Отправлено: {m.name} ({m.kind})")
                self.sleep(1.2)
            if state.get("digest_date") != data_day.isoformat():
                self.send(digest_text(metrics, len(series), self.cfg, data_day))
                state["digest_date"] = data_day.isoformat()
                self.out("Отправлен итог дня")
            return 0
        except TgError as exc:
            self.out(f"Ошибка Telegram: {exc}")
            code = 1
            return code
        finally:
            save_state(self.state_path, state, data_day)

    def mode_say(self) -> int:
        text = self.env.get("SAY_TEXT", "").strip()
        if not text:
            self.out("Нет текста для отправки.")
            return 1
        if not self.allow_send:
            self.out("Отправка выключена (ALLOW_SEND не yes). Было бы отправлено: " + text)
            return 0
        try:
            message_id = self.send(esc(text))
        except TgError as exc:
            self.out(f"Не отправлено: {exc}")
            return 1
        self.out(f"Отправлено, номер сообщения {message_id}.")
        return 0

    def mode_diag(self) -> int:
        self.out(f"Python {sys.version.split()[0]}")
        self.out(
            "Секреты: TELEGRAM_TOKEN: "
            + ("задан" if self.token else "НЕ задан")
            + "; TELEGRAM_CHAT_ID: "
            + ("задан" if self.chat not in ("", None) else "НЕ задан")
            + "; отправка разрешена: "
            + ("да" if self.allow_send else "нет")
        )
        series, url, error = self.load_data()
        if series:
            metrics, reasons, data_day = self.analysis(series)
            for line in self.stats_lines(series, metrics, reasons, data_day, url):
                self.out(line)
        else:
            self.out(f"Данные о ценах недоступны: {error}")
        if not self.token:
            return 0
        try:
            me = self.tg("getMe", {})["result"]
            self.out(f"Бот: @{me.get('username')}")
            if self.chat not in ("", None):
                chat = self.tg("getChat", {"chat_id": self.chat})["result"]
                kind = chat.get("type")
                title = chat.get("title") if kind != "private" else "(личный чат, имя скрыто)"
                self.out(f"Чат: тип {kind}, название: {title}")
                member = self.tg("getChatMember", {"chat_id": self.chat, "user_id": me["id"]})["result"]
                can_post = member.get("can_post_messages")
                self.out(f"Права бота в чате: {member.get('status')}; публикация: {'да' if can_post or member.get('status') == 'creator' or kind != 'channel' else 'нет'}")
        except TgError as exc:
            self.out(f"Telegram: {exc}")
        return 0

    def mode_chats(self) -> int:
        if not self.token:
            self.out("Токен бота не задан (секрет TELEGRAM_TOKEN).")
            return 1
        try:
            updates = self.tg("getUpdates", {"limit": 100, "timeout": 0, "allowed_updates": ["message", "channel_post", "my_chat_member"]})["result"]
        except TgError as exc:
            self.out(f"Не удалось получить список: {exc}")
            return 1
        seen: dict = {}
        private = 0
        for update in updates:
            for key in ("message", "channel_post", "my_chat_member"):
                chat = (update.get(key) or {}).get("chat")
                if not chat:
                    continue
                if chat.get("type") == "private":
                    private += 1
                else:
                    seen[chat["id"]] = (chat.get("type"), chat.get("title"))
        if not seen:
            self.out("Групп и каналов пока не видно. Добавьте бота в канал администратором, опубликуйте там любое сообщение и запустите снова.")
        for chat_id, (kind, title) in seen.items():
            self.out(f"Найден чат: {kind}; название: {title}; номер для секрета TELEGRAM_CHAT_ID: {chat_id}")
        if private:
            self.out(f"Личных чатов с ботом: {private} (номера скрыты: журнал публичный).")
        return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    cfg, problems = load_config(os.path.join(HERE, "config.json"))
    bot = Bot(cfg, dict(os.environ), state_path=os.path.join(HERE, "state.json"))
    for problem in problems:
        bot.out(problem)
    mode = (os.environ.get("BOT_MODE") or "preview").strip().lower()
    modes = {"preview": bot.mode_preview, "run": bot.mode_run, "say": bot.mode_say, "diag": bot.mode_diag, "chats": bot.mode_chats}
    if mode not in modes:
        bot.out(f"Неизвестный режим «{mode}». Допустимо: {', '.join(modes)}.")
        return 1
    bot.out(f"Режим: {mode}")
    code = modes[mode]()
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("```text\n" + "\n".join(bot.lines) + "\n```\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
