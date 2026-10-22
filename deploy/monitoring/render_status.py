#!/usr/bin/env python3
"""The status page for the iPhone Duo watchers: one HTML page built from Prometheus.

Standard library only. It listens on 127.0.0.1 and is meant to be opened
through an SSH tunnel, not published. It shows numbers and states only: no
chat ids, handles, tokens or message texts.
"""
import datetime as dt
import html
import http.server
import json
import math
import os
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request

PROM = os.environ.get("PROM", "http://127.0.0.1:9090")
LISTEN = os.environ.get("LISTEN", "127.0.0.1:8080")
CACHE_SECONDS = 20
DEADMAN_SILENT_SECONDS = 900
CPU_LIMIT = 85
RAM_LIMIT = 85
DISK_LIMIT = 85
DAY = 24 * 3600
WEEK = 7 * DAY


class PromError(RuntimeError):
    pass


def _get_json(url, timeout):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = json.loads(r.read())
    except (OSError, ValueError) as e:
        raise PromError(f"Prometheus недоступен: {e}") from e
    if data.get("status") != "success":
        raise PromError(f"Prometheus вернул ошибку: {data.get('error', '?')}")
    return data


def _number(text):
    value = float(text)
    return None if math.isnan(value) else value


def query(expr):
    url = f"{PROM}/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    return _get_json(url, 10)["data"]["result"]


def scalar(expr, default=None):
    rows = query(expr)
    if not rows:
        return default
    value = _number(rows[0]["value"][1])
    return default if value is None else value


def by_label(expr, label):
    out = {}
    for row in query(expr):
        value = _number(row["value"][1])
        if value is not None:
            out[row["metric"].get(label, "")] = value
    return out


def series(expr, span, step):
    end = time.time()
    url = f"{PROM}/api/v1/query_range?" + urllib.parse.urlencode(
        {"query": expr, "start": end - span, "end": end, "step": step})
    result = _get_json(url, 20)["data"]["result"]
    if not result:
        return []
    points = []
    for stamp, text in result[0]["values"]:
        value = _number(text)
        if value is not None:
            points.append((float(stamp), value))
    return points


def sku_rows():
    ship = by_label("duo_sku_ship_timestamp_seconds", "pn")
    buyable = by_label("duo_sku_buyable", "pn")
    pickup = by_label("duo_sku_pickup_stores", "pn")
    rows = []
    for row in query("duo_sku_price_aed"):
        pn = row["metric"].get("pn", "")
        rows.append({
            "pn": pn,
            "name": row["metric"].get("name", pn),
            "price": _number(row["value"][1]),
            "ship": ship.get(pn),
            "buyable": buyable.get(pn),
            "pickup": pickup.get(pn),
        })
    return sorted(rows, key=lambda r: r["price"] or 0)


def gather():
    d = {
        "node_up": scalar('up{job="node"}'),
        "cpu": scalar('100 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100'),
        "ram": scalar("100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)"),
        "disk": scalar('100 * (1 - node_filesystem_avail_bytes{mountpoint="/"}'
                       ' / node_filesystem_size_bytes{mountpoint="/"})'),
        "load1": scalar("node_load1"),
        "cores": scalar('count(node_cpu_seconds_total{mode="idle"})'),
        "uptime": scalar("time() - node_boot_time_seconds"),
        "hb_age": scalar("duo_heartbeat_age_seconds"),
        "hb_skus": scalar("duo_heartbeat_skus"),
        "hb_earliest": scalar("duo_heartbeat_earliest_timestamp_seconds"),
        "deadman": scalar("duo_deadman_incident", 0),
        "standby_running": scalar('duo_container_running{name="standby"}'),
        "standby_primary": scalar("duo_standby_container_primary"),
        "amnezia_running": scalar('duo_container_running{name="amnezia"}'),
        "standby_mode": scalar("duo_standby_mode", -1),
        "standby_checked_age": scalar("duo_standby_checked_age_seconds"),
        "fail": by_label("duo_standby_health_fail_streak", "subject"),
        "tcp": by_label("duo_host_tcp_up", "name"),
        "services": by_label("duo_service_up", "name"),
        "telegram": scalar("duo_telegram_reachable"),
        "events": by_label("duo_standby_log_events_total", "event"),
        "skus": sku_rows(),
    }
    d["series"] = {
        "hb_age": series("duo_heartbeat_age_seconds", DAY, 300),
        "cpu": series('100 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100', DAY, 300),
        "ram": series("100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)",
                      DAY, 300),
        "earliest": series("min(duo_sku_ship_timestamp_seconds)", WEEK, 3600),
        "polls": series('rate(duo_standby_log_events_total{event="ok_poll"}[1h]) * 3600',
                        DAY, 900),
    }
    return d


def assess(d):
    """Return ('green' | 'yellow' | 'red', reasons). Red means nobody is delivering."""
    red, yellow = [], []
    if d["node_up"] != 1:
        red.append("сервер в Германии не отдаёт метрики")
    hb = d["hb_age"]
    silent = hb is None or hb < 0 or hb > DEADMAN_SILENT_SECONDS
    if d["standby_primary"] == 1:
        # The primary is the container here, so no other watcher stands by to take over.
        if silent:
            red.append("основной в Германии молчит: никто не ведёт доставку")
    elif silent and d["standby_mode"] != 1:
        red.append("основной молчит, и резерв не ведёт доставку")
    elif silent:
        yellow.append("основной молчит: доставку ведёт резерв в Германии")
    if d["deadman"] == 1:
        yellow.append("деадман: открыт инцидент «наблюдатель молчит»")
    if d["standby_running"] != 1:
        yellow.append("контейнер наблюдателя в Германии не запущен")
    if d["fail"].get("apple", 0) > 0:
        yellow.append("наблюдатель в Германии: неудач опроса Apple подряд "
                      f"{int(d['fail']['apple'])}")
    for name, value in sorted(d["tcp"].items()):
        if not value:
            yellow.append(f"{name}: порт недоступен")
    for name, value in sorted(d["services"].items()):
        if not value:
            yellow.append(f"служба {name} не отвечает")
    if d["telegram"] == 0:
        yellow.append("Telegram не отвечает с сервера")
    for label, value, limit in (("ЦП", d["cpu"], CPU_LIMIT),
                                ("ОЗУ", d["ram"], RAM_LIMIT),
                                ("диск", d["disk"], DISK_LIMIT)):
        if value is not None and value > limit:
            yellow.append(f"{label} загружен на {value:.0f}%")
    if red:
        return "red", red + yellow
    if yellow:
        return "yellow", yellow
    return "green", []


LEVEL = {"green": ("всё работает", "ok"),
         "yellow": ("есть вопросы", "warn"),
         "red": ("проблема", "bad")}


def num(value, fmt="{:.0f}", unit=""):
    return "—" if value is None else fmt.format(value) + unit


def mark(ok):
    return '<span class="ok">●</span>' if ok else '<span class="bad">●</span>'


def on(value):
    return '<span class="muted">—</span>' if value is None else mark(bool(value))


def ago(seconds):
    if seconds is None or seconds < 0:
        return "нет данных"
    if seconds < 90:
        return f"{seconds:.0f} с назад"
    if seconds < 90 * 60:
        return f"{seconds / 60:.0f} мин назад"
    if seconds < 48 * 3600:
        return f"{seconds / 3600:.0f} ч назад"
    return f"{seconds / 86400:.0f} дн назад"


def duration(seconds):
    if seconds is None:
        return "—"
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    return f"{days} д {hours} ч" if days else f"{hours} ч {rest // 60} мин"


def ts_text(stamp):
    if stamp is None:
        return "—"
    return dt.datetime.fromtimestamp(stamp, dt.timezone.utc).strftime("%Y-%m-%d")


def card(title, lines):
    items = "".join(f"<div>{html.escape(line)}</div>" for line in lines if line)
    return f'<div class="card"><h3>{html.escape(title)}</h3>{items}</div>'


def spark(points, unit="", fmt="{:.0f}"):
    if len(points) < 2:
        return '<div class="muted">данные копятся: график появится через несколько минут</div>'
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    if y1 - y0 < 1e-9:
        y0, y1 = y0 - 1, y1 + 1
    width, height = 360, 70
    coords = " ".join(
        f"{(x - x0) / (x1 - x0) * width:.1f},"
        f"{height - 3 - (y - y0) / (y1 - y0) * (height - 6):.1f}"
        for x, y in points)
    return (f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" class="spark">'
            f'<polyline points="{coords}"/></svg>'
            f'<div class="muted">мин {fmt.format(min(ys))}{unit} · макс {fmt.format(max(ys))}{unit}'
            f' · сейчас {fmt.format(ys[-1])}{unit}</div>')


def span_hours(points):
    if len(points) < 2:
        return 0.0
    return (points[-1][0] - points[0][0]) / 3600


def hours_text(points):
    hours = span_hours(points)
    if hours < 1:
        return "меньше часа: данные копятся"
    return f"{hours:.0f} ч"


def polls_text(avg, points):
    if span_hours(points) < 1 or avg is None:
        return "данные копятся"
    return f"{avg:.0f}"


def uptime_percent(points):
    if not points:
        return None
    alive = sum(1 for _, age in points if 0 <= age <= DEADMAN_SILENT_SECONDS)
    return 100.0 * alive / len(points)


def servers_section(d):
    cores = d["cores"] or 1
    rows = [
        "<tr><td>" + mark(d["node_up"] == 1) + " <b>Сервер в Германии</b>"
        "<div class='muted'>VPS, 188.213.0.80</div></td>"
        f"<td>ЦП {num(d['cpu'], unit='%')} · ОЗУ {num(d['ram'], unit='%')}"
        f" · диск {num(d['disk'], unit='%')}"
        f"<div class='muted'>нагрузка {num(d['load1'], '{:.2f}')} на {int(cores)} ядро"
        f" · аптайм {duration(d['uptime'])}</div></td>"
        f"<td>наблюдатель {on(d['standby_running'] == 1)} · amnezia {on(d['amnezia_running'] == 1)}"
        f"<div class='muted'>sink {on(d['services'].get('sink'))}"
        f" · tinyproxy {on(d['services'].get('tinyproxy'))}"
        f" · sshd {on(d['services'].get('sshd'))}"
        f" · Telegram {on(d['telegram'])}</div></td></tr>",
    ]
    # The heartbeat does not say which machine sent it, so an open SSH port
    # proves the host is up, not that a watcher runs on it.
    for label, key, note in (("NanoPi, дом", "nanopi_ssh",
                              "машина на связи; работает ли на ней наблюдатель, по heartbeat не видно"),
                             ("ПК pupu-pu, дом", "pc_ssh", "наблюдатель: см. блок ниже")):
        up = d["tcp"].get(key)
        rows.append(
            f"<tr><td>{on(up)} <b>{label}</b><div class='muted'>{note}</div></td>"
            f"<td class='muted'>ресурсы не видны снаружи дома</td>"
            f"<td>порт SSH {'открыт' if up else 'недоступен'}</td></tr>")
    return ("<h2>Серверы</h2><div class='table-wrap'><table><tr><th>Сервер</th>"
            "<th>Ресурсы</th><th>Службы</th></tr>" + "".join(rows) + "</table></div>")


def watchers_section(d):
    hb = d["hb_age"]
    silent = hb is None or hb < 0 or hb > DEADMAN_SILENT_SECONDS
    ev = {k: int(v) for k, v in d["events"].items()}
    in_germany = d["standby_primary"] == 1
    source = ("источник удара: последний удачный опрос Apple в контейнере" if in_germany
              else "источник удара: не различается, все пишут в один канал")
    apple_line = f"последний опрос Apple: {ago(d['standby_checked_age'])}"
    fails_line = (f"неудач подряд: Apple {int(d['fail'].get('apple', 0))},"
                  f" Telegram {int(d['fail'].get('telegram', 0))}")
    if in_germany:
        own_card = card("Контейнер в Германии", [
            "роль: основной, резервного наблюдателя сейчас нет",
            apple_line,
            fails_line,
        ])
    else:
        mode = {1: "перехват — ведёт доставку", 0: "тихий — ждёт", -1: "неизвестно"}.get(
            int(d["standby_mode"]), "неизвестно")
        own_card = card("Резерв в Германии", [f"режим: {mode}", apple_line, fails_line])
    cards = [
        card("Основной наблюдатель", [
            f"heartbeat: {ago(hb) if hb is not None and hb >= 0 else 'удара не было'}",
            "состояние: " + ("молчит" if silent else "работает"),
            source,
            f"в ударе: {int(d['hb_skus'])} конфигураций" if d["hb_skus"] is not None else "",
            f"самый ранний срок: {ts_text(d['hb_earliest'])}" if d["hb_earliest"] else "",
        ]),
        own_card,
        card("Деадман", [
            "инцидент открыт" if d["deadman"] == 1 else "инцидентов нет",
        ]),
        card("Журнал контейнера в Германии (текущий файл лога)", [
            f"опросов Apple (ok): {ev.get('ok_poll', 0)}",
            f"конфликтов 409: {ev.get('conflict', 0)}",
            f"находок: {ev.get('finding', 0)}",
            f"команд обработано: {ev.get('command', 0)}",
            f"переходов в перехват: {ev.get('takeover', 0)}, в тишину: {ev.get('quiet', 0)}",
            f"ошибок отправки: {ev.get('send_failed', 0)}, ошибок цикла: {ev.get('error', 0)}",
        ]),
    ]
    return "<h2>Наблюдатели</h2><div class='grid'>" + "".join(cards) + "</div>"


def sku_section(d):
    rows = []
    for row in d["skus"]:
        buy = row["buyable"]
        buy_text = "—" if buy is None else ('<span class="ok">да</span>' if buy
                                            else '<span class="bad">нет</span>')
        pick = row["pickup"]
        pick_text = "—" if pick is None else (f"{int(pick)}" if pick else "нет")
        rows.append(
            f"<tr><td>{html.escape(row['name'])}<div class='muted'>{html.escape(row['pn'])}</div></td>"
            f"<td>{num(row['price'])}</td><td>{ts_text(row['ship'])}</td>"
            f"<td>{buy_text}</td><td>{pick_text}</td></tr>")
    return ("<h2>Apple UAE: конфигурации</h2><div class='table-wrap'><table><tr>"
            "<th>Конфигурация</th><th>Цена, AED</th><th>Ранний срок</th>"
            "<th>Заказ сейчас</th><th>Самовывоз сейчас, магазинов</th></tr>"
            + "".join(rows) + "</table></div>")


def analytics_section(d):
    s = d["series"]
    uptime = uptime_percent(s["hb_age"])
    polls = [v for _, v in s["polls"]]
    avg_polls = sum(polls) / len(polls) if polls else None
    earliest = s["earliest"]
    shift = None
    if len(earliest) >= 2:
        shift = (earliest[-1][1] - earliest[0][1]) / 86400
    shift_text = ("—" if shift is None else
                  f"{shift:+.0f} дн за неделю" + (" (сроки стали раньше)" if shift < 0 else
                                                  " (сроки стали позже)" if shift > 0 else ""))
    head = ("<h2>Аналитика</h2><div class='grid'>"
            + card("Работа основного наблюдателя, 24 ч", [
                f"данные за {hours_text(s['hb_age'])}",
                f"доля времени с heartbeat моложе 15 мин: {num(uptime, '{:.1f}', '%')}",
                f"опросов Apple в час (среднее): {polls_text(avg_polls, s['polls'])}",
            ])
            + card("Сроки доставки, 7 дней", [f"данные за {hours_text(earliest)}", shift_text])
            + "</div>")
    charts = [
        ("Возраст heartbeat, 24 ч", spark([(t, v) for t, v in s["hb_age"]], " с", "{:.0f}")),
        ("Загрузка ЦП, 24 ч", spark(s["cpu"], " %", "{:.0f}")),
        ("Занятость ОЗУ, 24 ч", spark(s["ram"], " %", "{:.0f}")),
        ("Самый ранний срок доставки, 7 дней",
         spark([(t, v) for t, v in s["earliest"]], "", "{:.0f}")),
        ("Опросы Apple в час, 24 ч", spark(s["polls"], "", "{:.0f}")),
    ]
    grid = "".join(card_html(title, body) for title, body in charts)
    return head + "<div class='grid'>" + grid + "</div>"


def card_html(title, body):
    return f'<div class="card"><h3>{html.escape(title)}</h3>{body}</div>'


CSS = """
:root { color-scheme: light dark; --bg:#f6f7f9; --fg:#1d2330; --card:#fff; --line:#e3e6ec;
  --muted:#6b7385; --ok:#1f9d55; --warn:#d68a00; --bad:#d64545; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg:#12151c; --fg:#e7eaf0; --card:#1b2030; --line:#2a3145; --muted:#8b93a7; } }
body { margin:0; background:var(--bg); color:var(--fg);
  font:15px/1.45 -apple-system, system-ui, sans-serif; }
main { max-width:1100px; margin:0 auto; padding:20px 16px 48px; }
h1 { font-size:22px; margin:0 0 4px; } h2 { font-size:17px; margin:28px 0 10px; }
.sub { color:var(--muted); font-size:13px; }
.verdict { display:inline-block; padding:6px 12px; border-radius:999px; font-weight:600; color:#fff; }
/* These must win over the plain .ok/.warn/.bad text colours used elsewhere. */
.verdict.ok { background:var(--ok); color:#fff; }
.verdict.warn { background:var(--warn); color:#fff; }
.verdict.bad { background:var(--bad); color:#fff; }
.grid { display:grid; gap:12px; grid-template-columns:repeat(auto-fit, minmax(260px, 1fr)); }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px; }
.card h3 { font-size:15px; margin:0 0 8px; }
table { width:100%; border-collapse:collapse; background:var(--card);
  border:1px solid var(--line); border-radius:12px; }
th, td { padding:8px 10px; border-bottom:1px solid var(--line); text-align:left;
  font-size:14px; vertical-align:top; }
th { color:var(--muted); font-weight:600; font-size:12px; text-transform:uppercase; }
.ok { color:var(--ok); } .warn { color:var(--warn); } .bad { color:var(--bad); }
.muted { color:var(--muted); font-size:12px; }
.spark { width:100%; height:70px; display:block; color:var(--ok); }
.spark polyline { fill:none; stroke:currentColor; stroke-width:1.6; vector-effect:non-scaling-stroke; }
ul.reasons { margin:8px 0 0; padding-left:18px; }
.table-wrap { overflow-x:auto; }
"""


def page(d):
    level, reasons = assess(d)
    label, css = LEVEL[level]
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    parts = [
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta http-equiv="refresh" content="60">'
        "<title>iPhone Duo — состояние</title>"
        f"<style>{CSS}</style></head><body><main>",
        "<h1>iPhone Duo — состояние инфраструктуры</h1>",
        f'<div class="sub">обновлено {now} · страница сама обновляется раз в минуту'
        " · данные Prometheus</div>",
        f'<p><span class="verdict {css}">{label}</span></p>',
    ]
    if reasons:
        parts.append('<ul class="reasons">'
                     + "".join(f"<li>{html.escape(r)}</li>" for r in reasons) + "</ul>")
    parts += [servers_section(d), watchers_section(d), sku_section(d), analytics_section(d),
              '<p class="muted">Страница открывается только через SSH-туннель: '
              "<code>ssh -L 8080:127.0.0.1:8080 root@188.213.0.80</code>, затем "
              "http://127.0.0.1:8080. Здесь только числа и состояния: без адресов чатов, "
              "имён и токенов.</p>",
              "</main></body></html>"]
    return "".join(parts)


def error_page(message):
    return ('<!doctype html><html lang="ru"><head><meta charset="utf-8">'
            '<meta http-equiv="refresh" content="30"><title>iPhone Duo — ошибка</title>'
            f"<style>{CSS}</style></head><body><main><h1>Страница не может собрать данные</h1>"
            f"<p>{html.escape(message)}</p>"
            '<p class="muted">Повторю через 30 с. Если не проходит, проверьте '
            "контейнер prometheus и fleet-exporter.</p></main></body></html>")


class Cache:
    def __init__(self):
        self.lock = threading.Lock()
        self.body = None
        self.at = 0.0

    def get(self):
        with self.lock:
            if self.body is None or time.time() - self.at > CACHE_SECONDS:
                try:
                    self.body = page(gather())
                except PromError as e:
                    self.body = error_page(str(e))
                except Exception:
                    traceback.print_exc(file=sys.stderr)
                    self.body = error_page("внутренняя ошибка, см. журнал контейнера")
                self.at = time.time()
            return self.body


def make_handler(cache):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?", 1)[0] not in ("/", "/index.html"):
                self.send_error(404)
                return
            body = cache.get().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            """One line per page view would only add noise to the container log."""

    return Handler


def main():
    host, port = LISTEN.rsplit(":", 1)
    http.server.ThreadingHTTPServer((host, int(port)), make_handler(Cache())).serve_forever()


if __name__ == "__main__":
    main()
