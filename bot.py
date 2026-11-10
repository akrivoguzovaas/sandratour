#!/usr/bin/env python3
"""
Монитор дешёвых авиабилетов: Travelpayouts (Aviasales) -> Telegram.

Запускается по расписанию (GitHub Actions). Каждый запуск:
  1. берёт цены туда-обратно из Астаны по всем направлениям на декабрь;
  2. считает «рыночную» цену (медиану) по каждому направлению;
  3. присылает в Telegram варианты, которые
       - дешевле медианы на deal_discount_percent% (🔥 находка), или
       - укладываются в бюджет на человека (✅ в бюджете);
  4. раз в день (digest_hour_utc) присылает сводку лучших цен по всем направлениям.

Секреты берутся из переменных окружения:
  TP_TOKEN    — токен Travelpayouts
  TG_TOKEN    — токен Telegram-бота
  TG_CHAT_ID  — (необязательно) id чата; если не задан, бот возьмёт его
                из последнего сообщения, которое вы ему написали.
Без внешних зависимостей — только стандартная библиотека Python.
"""
import json
import os
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "state.json")

TP_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
TG_URL = "https://api.telegram.org/bot{token}/{method}"


# ---------------------------------------------------------------- helpers
def http_get_json(url, params=None, headers=None):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def http_post_json(url, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"Telegram ответил {e.code}: {body}") from None


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def fmt_money(n):
    return f"{int(round(n)):,}".replace(",", " ") + " ₸"


def parse_day(s):
    # "2026-12-20T04:50:00+05:00" -> date(2026, 12, 20)
    return date.fromisoformat(s[:10])


# ---------------------------------------------------------------- data
def months_between(d1, d2):
    """Список месяцев 'YYYY-MM' от d1 до d2 включительно."""
    out, y, m = [], d1.year, d1.month
    while (y, m) <= (d2.year, d2.month):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def fetch_offers(tp_token, origin, dest, cfg, fetch=http_get_json):
    """Все закешированные варианты туда-обратно с вылетом в нужном месяце(ах)."""
    dep_from = date.fromisoformat(cfg["departure_from"])
    dep_to = date.fromisoformat(cfg["departure_to"])
    offers = []
    for month in months_between(dep_from, dep_to):
        params = {
            "origin": origin,
            "destination": dest,
            "departure_at": month,
            "one_way": "false",
            "direct": "false",
            "unique": "false",
            "sorting": "price",
            "currency": cfg.get("currency", "kzt"),
            "limit": 1000,
            "page": 1,
            "token": tp_token,
        }
        try:
            resp = fetch(TP_URL, params)
        except Exception as e:  # сеть/лимиты — просто пропускаем направление
            print(f"[warn] {origin}-{dest} {month}: {e}", file=sys.stderr)
            continue
        if not resp.get("success", True):
            print(f"[warn] {origin}-{dest} {month}: {resp.get('error')}", file=sys.stderr)
            continue
        offers.extend(resp.get("data") or [])
    return offers


def analyse(offers, cfg):
    """Возвращает (рыночная_медиана, подходящие_варианты_по_нашим_датам)."""
    if not offers:
        return None, []
    # «Рынок» — медиана по всем вариантам месяца (не только по нашим датам),
    # чтобы было с чем сравнивать.
    market = statistics.median(o["price"] for o in offers if o.get("price"))

    dep_from = date.fromisoformat(cfg["departure_from"])
    dep_to = date.fromisoformat(cfg["departure_to"])
    fits = []
    for o in offers:
        if not o.get("departure_at") or not o.get("return_at"):
            continue
        dep, ret = parse_day(o["departure_at"]), parse_day(o["return_at"])
        nights = (ret - dep).days
        if dep_from <= dep <= dep_to and cfg["nights_min"] <= nights <= cfg["nights_max"]:
            o = dict(o, _dep=dep.isoformat(), _ret=ret.isoformat(), _nights=nights)
            fits.append(o)
    fits.sort(key=lambda o: o["price"])
    return market, fits


def classify(offer, market, cfg):
    price = offer["price"]  # Travelpayouts отдаёт цену за 1 пассажира
    discount = (1 - price / market) * 100 if market else 0
    is_deal = discount >= cfg["deal_discount_percent"]
    in_budget = price <= cfg["budget_per_person_kzt"]
    return is_deal, in_budget, discount


def offer_key(dest, o):
    return f"{dest}|{o['_dep']}|{o['_ret']}|{o.get('airline','')}|{o.get('flight_number','')}"


def offer_text(dest_name, o, market, discount, cfg, tag):
    pax = cfg["passengers"]
    transfers = o.get("transfers", 0)
    back_tr = o.get("return_transfers", transfers)
    def tr(n):
        return "прямой" if n == 0 else f"{n} перес."
    dep = datetime.fromisoformat(o["_dep"]).strftime("%d.%m")
    ret = datetime.fromisoformat(o["_ret"]).strftime("%d.%m")
    link = "https://www.aviasales.kz" + o["link"] if o.get("link") else None
    lines = [
        f"{tag} <b>{dest_name}</b>",
        f"{dep} → {ret} ({o['_nights']} ноч.), {o.get('airline','')} · туда {tr(transfers)}, обратно {tr(back_tr)}",
        f"<b>{fmt_money(o['price'])}</b> / чел · {fmt_money(o['price'] * pax)} на {pax}",
        f"рынок ≈ {fmt_money(market)} / чел ({'-' if discount >= 0 else '+'}{abs(discount):.0f}%)",
    ]
    if link:
        lines.append(f'<a href="{link}">Проверить и купить</a>')
    return "\n".join(lines)


# ---------------------------------------------------------------- telegram
def tg_chat_id(tg_token, state):
    cid = os.environ.get("TG_CHAT_ID") or state.get("chat_id")
    if cid:
        return cid
    try:
        upd = http_get_json(TG_URL.format(token=tg_token, method="getUpdates"))
    except Exception as e:
        raise SystemExit(
            f"Не получилось узнать ваш chat id ({e}). Скорее всего бот подключён к конструктору "
            "(BotHelp и т.п.). Узнайте свой id у @userinfobot и добавьте его в секрет TG_CHAT_ID."
        )
    for u in reversed(upd.get("result", [])):
        msg = u.get("message") or u.get("edited_message")
        if msg:
            state["chat_id"] = msg["chat"]["id"]
            return state["chat_id"]
    raise SystemExit("Не знаю, куда писать: напишите боту любое сообщение в Telegram и перезапустите.")


def tg_send(tg_token, chat_id, text):
    # Telegram ограничивает сообщение 4096 символами — режем по блокам.
    chunks, cur = [], ""
    for block in text.split("\n\n"):
        if len(cur) + len(block) + 2 > 3900:
            chunks.append(cur)
            cur = ""
        cur = (cur + "\n\n" + block) if cur else block
    if cur:
        chunks.append(cur)
    for c in chunks:
        http_post_json(TG_URL.format(token=tg_token, method="sendMessage"), {
            "chat_id": chat_id, "text": c, "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })


# ---------------------------------------------------------------- main
def run(cfg, state, tp_token, fetch=http_get_json, now=None):
    """Возвращает список сообщений для отправки; обновляет state."""
    now = now or datetime.now(timezone.utc)
    notified = state.setdefault("notified", {})
    alerts, digest = [], []

    for dest, name in cfg["destinations"].items():
        offers = fetch_offers(tp_token, cfg["origin"], dest, cfg, fetch)
        market, fits = analyse(offers, cfg)
        if not fits:
            digest.append(f"• {name}: на ваши даты в кэше пока нет цен")
            continue

        best = fits[0]
        _, _, best_disc = classify(best, market, cfg)
        digest.append(
            f"• {name}: от <b>{fmt_money(best['price'])}</b>/чел "
            f"({datetime.fromisoformat(best['_dep']).strftime('%d.%m')}–"
            f"{datetime.fromisoformat(best['_ret']).strftime('%d.%m')}), "
            f"рынок ≈ {fmt_money(market)}"
        )

        sent_here = 0
        for o in fits[:20]:
            if sent_here >= cfg.get("max_alerts_per_destination", 3):
                break
            is_deal, in_budget, disc = classify(o, market, cfg)
            if not (is_deal or in_budget):
                continue
            key = offer_key(dest, o)
            prev = notified.get(key)
            if prev is not None and o["price"] >= prev:
                continue  # уже присылали по такой же или меньшей цене
            notified[key] = o["price"]
            sent_here += 1
            tag = "🔥 Ниже рынка" if is_deal else "✅ В бюджете"
            if prev is not None:
                tag = "📉 Подешевело! " + tag
            alerts.append(offer_text(name, o, market, disc, cfg, tag))

    messages = []
    if alerts:
        messages.append("✈️ <b>АВИАБИЛЕТЫ · новые выгодные варианты</b>\n\n" + "\n\n".join(alerts))

    today = now.date().isoformat()
    if now.hour == cfg.get("digest_hour_utc", 4) and state.get("last_digest") != today:
        state["last_digest"] = today
        messages.append(
            f"✈️📊 <b>АВИАБИЛЕТЫ · сводка на {now.strftime('%d.%m')}</b> "
            f"(вылет {cfg['departure_from'][8:10]}.12–{cfg['departure_to'][8:10]}.12, "
            f"{cfg['nights_min']}–{cfg['nights_max']} ночей, цена туда-обратно)\n"
            + "\n".join(digest)
        )
    return messages


INBOX_DIR = os.path.join(HERE, "inbox")


def forward_inbox(tg_token, chat_id):
    """Пересылает в Telegram находки по турам из папки inbox/ и удаляет их."""
    if not os.path.isdir(INBOX_DIR):
        return
    for name in sorted(os.listdir(INBOX_DIR)):
        if name.startswith(".") or not name.endswith(".md"):
            continue
        path = os.path.join(INBOX_DIR, name)
        with open(path, encoding="utf-8") as f:
            text = f.read().strip()
        if text:
            # Файл пишется простым текстом; экранируем для HTML-режима Telegram.
            safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            tg_send(tg_token, chat_id, "🏝 <b>ТУРЫ</b>\n\n" + safe)
        os.remove(path)


def main():
    tp_token = (os.environ.get("TP_TOKEN") or "").strip()
    tg_token = (os.environ.get("TG_TOKEN") or "").strip()
    if not tp_token or not tg_token:
        raise SystemExit("Нужны переменные окружения TP_TOKEN и TG_TOKEN.")
    cfg = load_json(CONFIG_PATH, None)
    state = load_json(STATE_PATH, {})
    chat_id = tg_chat_id(tg_token, state)

    if not state.get("hello_sent"):
        tg_send(tg_token, chat_id, "👋 Бот подключён! Буду присылать дешёвые билеты из Астаны и ежедневную сводку.")
        state["hello_sent"] = True

    force_digest = os.environ.get("FORCE_DIGEST") == "1"
    now = datetime.now(timezone.utc)
    if force_digest:
        now = now.replace(hour=cfg.get("digest_hour_utc", 4))
        state.pop("last_digest", None)

    forward_inbox(tg_token, chat_id)
    for msg in run(cfg, state, tp_token, now=now):
        tg_send(tg_token, chat_id, msg)
    save_json(STATE_PATH, state)


if __name__ == "__main__":
    main()
