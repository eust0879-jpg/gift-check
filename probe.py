#!/usr/bin/env python3
"""One-off check of FREE data sources, run from a GitHub runner.

It asks each source once (GET only), and writes a plain-text report: what answered, how fast, how big, how fresh.
Nothing is sent anywhere except to the addresses listed in SOURCES. The only secret it can use is GIFTASSET_TOKEN
(from the environment), it goes to api.giftasset.dev and nowhere else, and it is never written to the report.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "giftprobe/0.1 (one-off check of free data sources)"
TIMEOUT = 25
MAX_BYTES = 5_000_000
SAMPLE_CHARS = 280
PAUSE_S = 1.0

GIFTASSET_HOST = "api.giftasset.dev"
TOKEN_ENV = "GIFTASSET_TOKEN"

# key, title, url, statuses that count as "as expected" besides 2xx, note for the report
SOURCES = [
    ("floors_live", "Открытые дневные цены (tggifts.com)", "https://tggifts.com/data/floors-daily.json", (), ""),
    (
        "floors_raw",
        "Те же данные на GitHub",
        "https://raw.githubusercontent.com/w3radar/telegram-gift-floors/main/data/floors-daily.json",
        (),
        "",
    ),
    ("fragment", "Fragment: публичная страница подарков", "https://fragment.com/gifts", (), "без входа в аккаунт"),
    ("tonapi", "TonAPI: курс TON без ключа", "https://tonapi.io/v2/rates?tokens=ton&currencies=usd,rub", (), ""),
    ("toncenter", "TON Center без ключа", "https://toncenter.com/api/v2/getMasterchainInfo", (), ""),
    (
        "coingecko",
        "CoinGecko: курс TON в рублях",
        "https://api.coingecko.com/api/v3/simple/price?ids=the-open-network&vs_currencies=usd,rub",
        (),
        "",
    ),
    (
        "telegram",
        "Telegram Bot API (только доступность)",
        "https://api.telegram.org/bot0:probe/getMe",
        (401, 404),
        "ответ 401/404 на выдуманный токен значит, что сервер Telegram достижим",
    ),
    (
        "giftasset",
        "Gift Asset (ваш ключ, если он задан)",
        "https://api.giftasset.dev/api/v1/gifts/get_gifts_price_list?models=false&premarket=false",
        (401, 403),
        "без ключа ответ 401 тоже значит, что сервер достижим",
    ),
]

DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A request that carries the key must not follow a redirect to some other host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


_open_without_redirects = urllib.request.build_opener(_NoRedirect).open


def redact(text: str, secrets: list) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def squeeze(text: str, limit: int = SAMPLE_CHARS) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit] + "…"


def describe_body(raw: bytes, content_type: str) -> str:
    """A short, safe description of what came back: shape of JSON, title of HTML, freshest date seen."""
    text = raw.decode("utf-8", errors="replace")
    parts: list = []
    parsed = None
    if "json" in (content_type or "").lower() or text.lstrip()[:1] in "[{":
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
    if isinstance(parsed, dict):
        keys = list(parsed.keys())
        parts.append(f"JSON-словарь, ключей: {len(keys)}, первые: {', '.join(str(k) for k in keys[:5])}")
    elif isinstance(parsed, list):
        parts.append(f"JSON-список, элементов: {len(parsed)}")
    elif "html" in (content_type or "").lower() or "<html" in text[:500].lower():
        match = TITLE_RE.search(text)
        parts.append("HTML-страница" + (f", заголовок: {squeeze(match.group(1), 80)}" if match else ""))
    else:
        parts.append("текст")
    dates = DATE_RE.findall(text)
    if dates:
        parts.append(f"дат в данных: {len(set(dates))}, самая свежая: {max(dates)}")
    return "; ".join(parts)


def fetch(url: str, headers: dict, opener=urllib.request.urlopen, timeout: int = TIMEOUT):
    """GET once. Returns (status, content_type, body, seconds, error_text). Never raises."""
    request = urllib.request.Request(url, headers=headers, method="GET")
    started = time.monotonic()
    try:
        with opener(request, timeout=timeout) as response:
            body = response.read(MAX_BYTES)
            status = getattr(response, "status", None) or response.getcode()
            ctype = response.headers.get("Content-Type", "") if response.headers else ""
            return int(status), ctype, body, time.monotonic() - started, ""
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(MAX_BYTES) or b""
        except Exception:  # noqa: BLE001 - a broken error body must not stop the check
            body = b""
        ctype = exc.headers.get("Content-Type", "") if exc.headers else ""
        return int(exc.code), ctype, body, time.monotonic() - started, ""
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", None) or exc
        return None, "", b"", time.monotonic() - started, f"{type(exc).__name__}: {reason}"


def check_source(source, env: dict, opener=urllib.request.urlopen) -> list:
    key, title, url, expected, note = source
    headers = {"User-Agent": UA, "Accept": "application/json, text/html;q=0.8, */*;q=0.5"}
    token = env.get(TOKEN_ENV, "").strip()
    host = urllib.parse.urlparse(url).hostname or ""
    secrets: list = []
    token_state = ""
    if host == GIFTASSET_HOST:
        if token:
            headers["X-API-Token"] = token  # the only place the token is ever used
            secrets.append(token)
            token_state = "ключ передан"
            if opener is urllib.request.urlopen:
                opener = _open_without_redirects
        else:
            token_state = "ключ не задан"
    status, ctype, body, seconds, error = fetch(url, headers, opener)
    lines = [f"### {title}", f"Адрес: {urllib.parse.urlunparse(urllib.parse.urlparse(url)._replace(query=''))}"]
    if status is None:
        lines.append(f"Итог: НЕТ СВЯЗИ ({redact(error, secrets)}), {seconds:.1f} с")
    else:
        if 200 <= status < 300:
            verdict = "ОК"
        elif status in expected:
            verdict = "ДОСТУПЕН (ответ как и ожидался)"
        else:
            verdict = "ОШИБКА"
        size = f"{len(body):,}".replace(",", " ")
        lines.append(f"Итог: {verdict}, код {status}, {size} байт, {seconds:.1f} с")
        if body:
            lines.append("Что внутри: " + redact(describe_body(body, ctype), secrets))
            lines.append("Начало ответа: " + redact(squeeze(body.decode("utf-8", errors="replace")), secrets))
    if token_state:
        lines.append(f"Ключ Gift Asset: {token_state}")
    if note:
        lines.append(f"Заметка: {note}")
    return lines


def run_probe(env: dict | None = None, opener=urllib.request.urlopen, sleep=time.sleep, sources=None) -> str:
    env = dict(os.environ if env is None else env)
    now = datetime.datetime.now(datetime.timezone.utc)
    report = [
        "ПРОВЕРКА БЕСПЛАТНЫХ ИСТОЧНИКОВ",
        f"Время запуска (UTC): {now:%Y-%m-%d %H:%M:%S}; Python {sys.version.split()[0]}",
        "Только запросы на чтение (GET). Секреты в отчёт не попадают.",
        "",
    ]
    for index, source in enumerate(sources or SOURCES):
        if index:
            sleep(PAUSE_S)
        report += check_source(source, env, opener)
        report.append("")
    return "\n".join(report)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    report = run_probe()
    print(report)
    with open("probe_report.txt", "w", encoding="utf-8", newline="\n") as fh:
        fh.write(report + "\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("```text\n" + report + "\n```\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
