#!/usr/bin/env python3
"""
Шлёт уведомление в Telegram, когда по ИЗВЕСТНОМУ (табличному) расписанию
скоро отправляется нужный автобус.

История: изначально скрипт пытался брать данные в реальном времени с
Яндекс.Карт, но их неофициальный API оказался заблокирован для запросов с
серверов вроде GitHub Actions (и вообще для "нечеловеческих" запросов) —
подробности в README. Поэтому теперь бот работает проще и надёжнее: просто
знает расписание автобуса (из config.yaml) и напоминает заранее. Это не
учитывает реальные опоздания автобуса, зато ничего не может сломаться на
стороне Яндекса.

Задуман для запуска по расписанию (GitHub Actions / cron): при каждом
запуске делает одну проверку по каждому "watch" из конфига и завершается.
Состояние (какие рейсы уже показывали сегодня) хранится в state.json рядом
со скриптом, чтобы не слать одно и то же уведомление на каждом прогоне.
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
import yaml

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python <3.9 fallback, не должно понадобиться
    from backports.zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"
LOCAL_CONFIG_PATH = BASE_DIR / "config.local.yaml"  # необязательный, для локальных секретов
STATE_PATH = BASE_DIR / "state.json"

WEEKDAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    if LOCAL_CONFIG_PATH.exists():
        with open(LOCAL_CONFIG_PATH, "r", encoding="utf-8") as f:
            local = yaml.safe_load(f) or {}
        for key, value in local.items():
            if isinstance(value, dict) and isinstance(config.get(key), dict):
                config[key].update(value)
            else:
                config[key] = value

    return config


def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def prune_state(state, dedup_window_minutes):
    """Удаляет записи старше dedup_window_minutes, чтобы файл не рос бесконечно."""
    cutoff = time.time() - dedup_window_minutes * 60
    for watch_name in list(state.keys()):
        entries = state[watch_name]
        state[watch_name] = {k: v for k, v in entries.items() if v.get("notified_at", 0) >= cutoff}
        if not state[watch_name]:
            del state[watch_name]
    return state


def parse_weekdays(weekdays: str):
    weekdays = weekdays.strip().lower()
    if "-" in weekdays and "," not in weekdays:
        start, end = weekdays.split("-")
        start_i, end_i = WEEKDAY_NAMES.index(start), WEEKDAY_NAMES.index(end)
        return set(WEEKDAY_NAMES[start_i : end_i + 1])
    return {d.strip() for d in weekdays.split(",")}


def send_telegram_message(bot_token: str, chat_id: str, text: str):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    resp = requests.post(
        url,
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
        timeout=15,
    )
    if resp.status_code != 200:
        print(f"Ошибка отправки в Telegram: {resp.status_code} {resp.text}", file=sys.stderr)
    resp.raise_for_status()


def main():
    dry_run = "--dry-run" in sys.argv  # проверить логику, не слать в Telegram

    config = load_config()
    tz = ZoneInfo(config.get("timezone", "Asia/Novosibirsk"))
    now_local = datetime.now(tz)
    today = now_local.date()

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or config.get("telegram", {}).get("bot_token")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or config.get("telegram", {}).get("chat_id")

    if not dry_run and (not bot_token or not chat_id):
        print(
            "Не заданы TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (ни в переменных окружения, "
            "ни в config.yaml/config.local.yaml). См. README.",
            file=sys.stderr,
        )
        sys.exit(1)

    state = load_state()
    dedup_window = config.get("dedup_window_minutes", 600)
    state = prune_state(state, dedup_window)

    for watch in config.get("watches", []):
        name = watch["name"]

        active_weekdays = parse_weekdays(watch.get("active_weekdays", "mon-sun"))
        if WEEKDAY_NAMES[now_local.weekday()] not in active_weekdays:
            continue

        threshold_min = watch.get("notify_before_minutes", 10)
        watch_state = state.setdefault(name, {})

        for dep_str in watch.get("departures", []):
            hour, minute = (int(x) for x in dep_str.strip().split(":"))
            dep_dt = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)

            minutes_left = (dep_dt - now_local).total_seconds() / 60
            if minutes_left < -1 or minutes_left > threshold_min:
                continue  # рейс уже уехал или ещё не скоро

            dedup_key = f"{today.isoformat()}:{dep_str}"
            if dedup_key in watch_state:
                continue  # про этот рейс уже напоминали сегодня

            text = (
                f"🚌 <b>{name}</b>\n"
                f"Автобус по расписанию в {dep_str} — через ~{max(0, round(minutes_left))} мин."
            )
            print(text.replace("\n", " | "))

            if not dry_run:
                send_telegram_message(bot_token, chat_id, text)

            watch_state[dedup_key] = {"notified_at": time.time()}

    save_state(state)


if __name__ == "__main__":
    main()
