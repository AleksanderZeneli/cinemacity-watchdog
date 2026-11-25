
#!/usr/bin/env python3
"""Monitorowanie seansów Diuny w IMAX 70 mm w Cinema City."""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

SITE_ID = "10101"
BASE = f"https://www.cinemacity.cz/cz/data-api-service/v1/quickbook/{SITE_ID}"
LANG = "cs_CZ"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

FILM_PATTERN = os.environ.get("FILM_PATTERN", "duna").lower()
AUDITORIUM_PATTERN = os.environ.get("AUDITORIUM_PATTERN", "imax").lower()
HORIZON_DAYS = int(os.environ.get("HORIZON_DAYS", "180"))
HINT_ATTR = os.environ.get("HINT_ATTR", "70-mm")
DELAY = float(os.environ.get("REQUEST_DELAY", "0.25"))

REQUIRED_ATTR = "70-mm"

# Interesujący nas zakres dat
PREFERRED_START = "2026-12-29"
PREFERRED_END = "2026-12-31"

PL_DAYS = ["pon", "wt", "śr", "czw", "pt", "sob", "niedz"]

CINEMA_TZ = ZoneInfo("Europe/Prague")


def now():
    return datetime.now(CINEMA_TZ).replace(tzinfo=None)


def api(path):
    """Pobiera dane z API Cinema City."""
    url = f"{BASE}{path}"
    last = None

    for attempt in range(4):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": UA}
            )

            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(
                    resp.read().decode("utf-8")
                )["body"]

        except (
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError
        ) as exc:
            last = exc
            time.sleep(2 ** attempt)

    raise SystemExit(
        f"Błąd API po 4 próbach: {url}\n{last}"
    )


def horizon():
    return (
        now().date() + timedelta(days=HORIZON_DAYS)
    ).isoformat()


def fetch_cinemas():
    body = api(
        f"/cinemas/with-event/until/{horizon()}?attr=&lang={LANG}"
    )

    return {
        c["id"]: c for c in body["cinemas"]
    }


def fetch_dates(cinema_id):
    time.sleep(DELAY)

    return api(
        f"/dates/in-cinema/{cinema_id}/until/"
        f"{horizon()}?attr=&lang={LANG}"
    )["dates"]


def fetch_day(cinema_id, day):
    time.sleep(DELAY)

    body = api(
        f"/film-events/in-cinema/{cinema_id}/"
        f"at-date/{day}?attr=&lang={LANG}"
    )

    films = {
        f["id"]: f for f in body.get("films", [])
    }

    return films, body.get("events", [])


def hint_cinema_ids():
    """Wyszukuje kina oferujące projekcje 70 mm."""

    if not HINT_ATTR:
        return set()

    body = api(
        f"/cinemas/with-event/until/{horizon()}"
        f"?attr={HINT_ATTR}&lang={LANG}"
    )

    return {
        c["id"] for c in body["cinemas"]
    }


def is_target_hall(event):
    return AUDITORIUM_PATTERN in (
        event.get("auditorium") or ""
    ).lower()


def is_70mm(event):
    return REQUIRED_ATTR in (
        event.get("attributeIds") or []
    )


def is_preferred_date(event):
    day = event["datetime"][:10]

    return PREFERRED_START <= day <= PREFERRED_END


def collect():
    """Wyszukuje seanse spełniające wszystkie wymagania."""

    cinemas = fetch_cinemas()

    dates_by_cinema = {
        cid: fetch_dates(cid) for cid in cinemas
    }

    candidates = hint_cinema_ids() & set(cinemas)

    day_cache = {}

    for cid, days in dates_by_cinema.items():
        if not days:
            continue

        probe = days[0]
        day_cache[(cid, probe)] = fetch_day(cid, probe)

        if any(
            is_target_hall(e)
            for e in day_cache[(cid, probe)][1]
        ):
            candidates.add(cid)

    found = {}

    for cid in sorted(candidates):
        for day in dates_by_cinema.get(cid, []):

            films, events = (
                day_cache.get((cid, day))
                or fetch_day(cid, day)
            )

            for e in events:
                film = films.get(e["filmId"], {})

                # Sprawdzenie nazwy filmu
                if FILM_PATTERN not in film.get("name", "").lower():
                    continue

                # Sprawdzenie sali IMAX
                if not is_target_hall(e):
                    continue

                # Odrzucenie projekcji innych niż 70 mm
                if not is_70mm(e):
                    continue

                found[e["id"]] = {
                    "id": e["id"],
                    "film": film.get("name", e["filmId"]),
                    "filmLink": film.get("link"),
                    "cinema": cinemas[cid]["displayName"],
                    "cinemaId": cid,
                    "datetime": e["eventDateTime"],
                    "auditorium": e.get("auditorium"),
                    "attrs": e.get("attributeIds") or [],
                    "booking": (
                        "https://tickets.cinemacity.cz/order/"
                        f"{e.get('presentationCode') or e['id']}"
                    ),
                    "soldOut": bool(e.get("soldOut")),
                }

    return found


def load_state(path):
    """Wczytuje wcześniej wykryte seanse."""

    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    except FileNotFoundError:
        return {
            "updated": None,
            "events": {}
        }


def save_state(path, events):
    """Zapisuje stan, jeżeli lista seansów się zmieniła."""

    old_events = load_state(path).get("events", {})

    if set(events) == set(old_events):
        return False

    os.makedirs(
        os.path.dirname(path) or ".",
        exist_ok=True
    )

    payload = {
        "updated": now().replace(
            microsecond=0
        ).isoformat(),
        "events": dict(
            sorted(
                events.items(),
                key=lambda kv: kv[1]["datetime"]
            )
        ),
    }

    with open(path, "w", encoding="utf-8") as fh:
        json.dump(
            payload,
            fh,
            ensure_ascii=False,
            indent=2
        )

        fh.write("\n")

    return True


def prune_past(events):
    """Usuwa stare seanse z zapamiętanego stanu."""

    cutoff = (
        now() - timedelta(days=1)
    ).isoformat()

    return {
        k: v
        for k, v in events.items()
        if v["datetime"] >= cutoff
    }


def fmt_dt(iso):
    dt = datetime.fromisoformat(iso)

    return (
        f"{PL_DAYS[dt.weekday()]} "
        f"{dt:%d.%m.%Y}, godz. {dt:%H:%M}"
    )


def fmt_short(iso):
    dt = datetime.fromisoformat(iso)
    return f"{dt:%d.%m}"


def group_by_cinema(events):
    order = {}

    for e in sorted(
        events,
        key=lambda x: (
            x["cinema"],
            x["datetime"]
        )
    ):
        order.setdefault(
            e["cinema"], []
        ).append(e)

    return order.items()


def render(new_events, gone_events):
    """Tworzy polskojęzyczny raport."""

    lines = []

    lines.append("# Diuna – IMAX 70 mm")
    lines.append("")

    lines.append(
        f"**Data sprawdzenia:** {now():%d.%m.%Y, %H:%M:%S}"
    )

    lines.append("")

    preferred = [
        e for e in new_events
        if is_preferred_date(e)
    ]

    if preferred:
        lines.append(
            f"> ⭐ WYKRYTO {len(preferred)} SEANSÓW "
            "W DNIACH 29–31 GRUDNIA!"
        )
        lines.append("")

    if new_events:
        lines.append(
            f"## Nowe seanse ({len(new_events)})"
        )

        lines.append("")

        for cinema, group in group_by_cinema(new_events):

            lines.append(f"### {cinema}")
            lines.append("")

            for e in group:
                flags = ["IMAX 70 mm"]

                if "subbed" in e["attrs"]:
                    flags.append("napisy")

                if "dubbed" in e["attrs"]:
                    flags.append("dubbing")

                if e["soldOut"]:
                    flags.append("WYPRZEDANE")

                else:
                    flags.append("niewyprzedane")

                suffix = " | ".join(flags)

                star = (
                    "⭐ " if is_preferred_date(e) else ""
                )

                lines.append(
                    f"- {star}**{fmt_dt(e['datetime'])}**"
                )

                lines.append(
                    f"  - Sala: {e['auditorium']}"
                )

                lines.append(
                    f"  - Format: {suffix}"
                )

                if e["booking"]:
                    lines.append(
                        f"  - [Kup bilet]({e['booking']})"
                    )

            lines.append("")

    if gone_events:
        lines.append(
            f"## Seanse usunięte z repertuaru "
            f"({len(gone_events)})"
        )

        lines.append("")

        for cinema, group in group_by_cinema(gone_events):

            lines.append(f"### {cinema}")
            lines.append("")

            for e in group:
                lines.append(
                    f"- {fmt_dt(e['datetime'])} "
                    f"| {e['auditorium']}"
                )

            lines.append("")

    film_link = next(
        (
            e["filmLink"]
            for e in list(new_events) + list(gone_events)
            if e.get("filmLink")
        ),
        None
    )

    if film_link:
        lines.append(
            f"[Strona filmu w Cinema City]({film_link})"
        )

    lines.append("")
    lines.append("---")
    lines.append(
        f"Automatyczne sprawdzenie: "
        f"{now():%d.%m.%Y, %H:%M:%S}"
    )

    lines.append(
        "Strefa czasowa: Europe/Prague"
    )

    lines.append(
        f"Filtr: {FILM_PATTERN} | "
        f"{AUDITORIUM_PATTERN} | 70 mm"
    )

    return "\n".join(lines)


def title_for(new_events):
    """Tworzy tytuł powiadomienia."""

    days = sorted({
        e["datetime"][:10]
        for e in new_events
    })

    span = fmt_short(days[0])

    if len(days) > 1:
        span += f"–{fmt_short(days[-1])}"

    n = len(new_events)

    preferred = any(
        is_preferred_date(e) for e in new_events
    )

    prefix = "⭐" if preferred else "🎬"

    return (
        f"{prefix} Diuna IMAX 70 mm: "
        f"nowe seanse ({n}) | {span}"
    )


def gh_output(**kwargs):
    """Przekazuje wynik do GitHub Actions."""

    path = os.environ.get("GITHUB_OUTPUT")

    if not path:
        return

    with open(path, "a", encoding="utf-8") as fh:
        for key, value in kwargs.items():
            fh.write(f"{key}={value}\n")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__
    )

    ap.add_argument(
        "--state",
        default="state/seen.json",
        help="Plik z zapamiętanymi seansami"
    )

    ap.add_argument(
        "--seed",
        action="store_true",
        help="Zapisz stan bez wysyłania powiadomień"
    )

    ap.add_argument(
        "--force-report",
        action="store_true",
        help="Wygeneruj raport ze wszystkich seansów"
    )

    ap.add_argument(
        "--report",
        default="report.md",
        help="Plik raportu"
    )

    ap.add_argument(
        "--title",
        default="title.txt",
        help="Plik tytułu powiadomienia"
    )

    args = ap.parse_args()

    print(
        f"Rozpoczęcie sprawdzania: "
        f"{now():%d.%m.%Y %H:%M:%S}"
    )

    current = collect()

    state = load_state(args.state)
    known = state.get("events", {})

    print(
        f"Znaleziono {len(current)} seansów IMAX 70 mm."
    )

    print(
        f"Wcześniej zapamiętane seanse: {len(known)}."
    )

    if args.seed:
        save_state(
            args.state,
            prune_past(current)
        )

        print(
            "Zapisano stan bez wysyłania powiadomienia."
        )

        gh_output(has_news="false")
        return

    if args.force_report:

        new_events = sorted(
            current.values(),
            key=lambda e: e["datetime"]
        )

        gone = []

    else:

        new_events = sorted(
            (
                v for k, v in current.items()
                if k not in known
            ),
            key=lambda e: e["datetime"]
        )

        future = now().isoformat()

        gone = sorted(
            (
                v for k, v in known.items()
                if k not in current
                and v["datetime"] > future
            ),
            key=lambda e: e["datetime"]
        )

    save_state(
        args.state,
        prune_past(current)
    )

    if not new_events and not gone:

        print("Brak nowych seansów.")
        gh_output(has_news="false")

        return

    body = render(new_events, gone)

    if new_events:
        title = title_for(new_events)

    else:
        title = (
            "🎬 Diuna IMAX 70 mm: "
            "seanse usunięte z repertuaru"
        )

    with open(
        args.report, "w", encoding="utf-8"
    ) as fh:
        fh.write(body + "\n")

    with open(
        args.title, "w", encoding="utf-8"
    ) as fh:
        fh.write(title + "\n")

    print(f"\n{title}\n")
    print(body)

    gh_output(has_news="true")


if __name__ == "__main__":
    sys.exit(main())
