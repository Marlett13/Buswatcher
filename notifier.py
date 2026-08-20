#!/usr/bin/env python3
"""
Проверяет расписание автобусов (через неофициальный API Яндекс.Карт, библиотека
ya_ma) для остановок из config.yaml и шлёт уведомление в Telegram, когда нужный
автобус приближается.

Задуман для запуска по расписанию (GitHub Actions / cron), а не как постоянно
работающий процесс: при каждом запуске делает ОДНУ проверку по каждому "watch"
из конфига и завершается.

Состояние (какие автобусы уже показывали) хранится в state.json рядом со
скриптом, чтобы не слать одно и то же уведомление на каждом прогоне.
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
import yaml

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python <3.9 fallback, не должно понадобиться
    from backports.zoneinfo import ZoneInfo

try:
    from ya_ma import YandexMapsRequester
except ImportError:
    print(
        "Не найден пакет ya_ma. Установите зависимости:\n"
        "    pip install -r requirements.txt\n",
        file=sys.stderr,
    )
    raise

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
        # Простое поверхностное слияние — достаточно для секции telegram.
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


def parse_hours_range(hours_range: str):
    start_s, end_s = hours_range.split("-")
    start_h, start_m = (int(x) for x in start_s.strip().split(":"))
    end_h, end_m = (int(x) for x in end_s.strip().split(":"))
    return (start_h, start_m), (end_h, end_m)


def parse_weekdays(weekdays: str):
    weekdays = weekdays.strip().lower()
    if "-" in weekdays and "," not in weekdays:
        start, end = weekdays.split("-")
        start_i, end_i = WEEKDAY_NAMES.index(start), WEEKDAY_NAMES.index(end)
        return set(WEEKDAY_NAMES[start_i : end_i + 1])
    return {d.strip() for d in weekdays.split(",")}


def is_active_now(watch, now_local: datetime) -> bool:
    active_weekdays = parse_weekdays(watch.get("active_weekdays", "mon-sun"))
    if WEEKDAY_NAMES[now_local.weekday()] not in active_weekdays:
        return False

    hours_range = watch.get("active_hours")
    if not hours_range:
        return True

    (start_h, start_m), (end_h, end_m) = parse_hours_range(hours_range)
    start = now_local.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    end = now_local.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    return start <= now_local <= end


def fetch_stop_events(requester: YandexMapsRequester, stop_id: int, debug: bool = False):
    """
    Возвращает список (route_name, eta_epoch_seconds, vehicle_id_or_None).
    """
    raw = requester.get_stop_info(stop_id)

    if debug:
        print(json.dumps(raw, ensure_ascii=False, indent=2)[:4000])

    if not raw or "data" not in raw:
        raise RuntimeError(f"Неожиданный ответ от Яндекса для stop_id={stop_id}: {raw}")

    try:
        transport_list = raw["data"]["properties"]["StopMetaData"]["Transport"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError(
            f"Не нашёл ожидаемых полей в ответе Яндекса (формат мог поменяться): {exc}\n"
            f"Запустите с --debug, чтобы увидеть сырой JSON."
        )

    events = []
    for route in transport_list:
        route_name = str(route.get("name", "")).strip()
        schedule_events = (route.get("BriefSchedule") or {}).get("Events") or []
        for ev in schedule_events:
            slot = ev.get("Estimated") or ev.get("Scheduled")
            if not slot or "value" not in slot:
                continue
            events.append((route_name, slot["value"], slot.get("vehicleId")))

    return events


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
    debug = "--debug" in sys.argv
    dry_run = "--dry-run" in sys.argv

    config = load_config()
    tz = ZoneInfo(config.get("timezone", "Asia/Novosibirsk"))
    now_local = datetime.now(tz)

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
    dedup_window = config.get("dedup_window_minutes", 90)
    state = prune_state(state, dedup_window)

    requester = YandexMapsRequester()

    any_active = False
    for watch in config.get("watches", []):
        name = watch["name"]

        if not is_active_now(watch, now_local):
            continue
        any_active = True

        stop_id = watch["stop_id"]
        if not stop_id or stop_id == 0:
            print(f"[{name}] stop_id не заполнен в config.yaml — пропускаю.")
            continue

        wanted_routes = {str(r) for r in watch.get("routes") or []}
        threshold_min = watch.get("notify_before_minutes", 7)

        try:
            events = fetch_stop_events(requester, stop_id, debug=debug)
        except Exception as exc:  # noqa: BLE001
            print(f"[{name}] Ошибка при запросе к Яндексу: {exc}", file=sys.stderr)
            continue

        watch_state = state.setdefault(name, {})

        for route_name, eta_epoch, vehicle_id in events:
            if wanted_routes and route_name not in wanted_routes:
                continue

            minutes_left = (eta_epoch - time.time()) / 60
            if minutes_left < -1 or minutes_left > threshold_min:
                continue

            dedup_key = f"{route_name}:{vehicle_id or round(eta_epoch / 60)}"
            if dedup_key in watch_state:
                continue

            eta_text = (datetime.fromtimestamp(eta_epoch, tz)).strftime("%H:%M")
            text = (
                f"🚌 <b>{name}</b>\n"
                f"Автобус <b>{route_name}</b> будет через ~{max(0, round(minutes_left))} мин "
                f"(≈{eta_text})"
            )
            print(text.replace("\n", " | "))

            if not dry_run:
                send_telegram_message(bot_token, chat_id, text)

            watch_state[dedup_key] = {"notified_at": time.time()}

    if not any_active and debug:
        print("Сейчас нет ни одного активного watch (вне заданных active_hours/active_weekdays).")

    save_state(state)


if __name__ == "__main__":
    main()
