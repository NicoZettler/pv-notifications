#!/usr/bin/env python3
"""
Monitor für das Qualifizierungsprogramm des Promotionsverbands BW.

Prüft bei jedem Aufruf
  1. die Programmseite https://www.promotionsverband-bw.de/qualifizierungsprogramm
  2. den eveeno-Veranstaltungskalender des Verbands (Anmeldeplattform)
und schickt eine Push-Nachricht über ntfy, wenn
  - eine neue Einführungsveranstaltung auftaucht (Abschnitt "Einführungsveranstaltungen"
    auf der Website oder Titel mit "Getting started", "Einführungsseminar" o. ä.),
  - bei einer ausgebuchten Einführungsveranstaltung wieder Plätze frei werden,
  - optional: irgendeine andere neue Veranstaltung erscheint (niedrige Priorität),
  - die Seite dauerhaft nicht erreichbar ist oder ihre Struktur sich geändert hat.

Der Zustand (bekannte Veranstaltungen) liegt in state.json. Beim allerersten Lauf
wird nur der Ist-Stand erfasst und eine Testnachricht geschickt.

Aufruf:
  python monitor.py              normaler Lauf
  python monitor.py --dry-run    nur anzeigen, was gefunden wird; nichts senden, nichts speichern
  python monitor.py --test       nur eine Testnachricht an den ntfy-Topic senden
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup, Comment, NavigableString, Tag

PAGE_URL = "https://www.promotionsverband-bw.de/qualifizierungsprogramm"
STATE_FILE = Path(os.environ.get("STATE_FILE") or Path(__file__).with_name("state.json"))
NTFY_SERVER = (os.environ.get("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = (os.environ.get("NTFY_TOPIC") or "").strip()
NOTIFY_OTHER_EVENTS = (os.environ.get("NOTIFY_OTHER_EVENTS") or "1") == "1"
FAIL_ALERT_AFTER = int(os.environ.get("FAIL_ALERT_AFTER") or "6")  # Fehlversuche in Folge bis zur Warnung
PRUNE_AFTER_DAYS = 120

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}
TIMEOUT = 30

# Hinweise auf das Einführungsseminar. Laut eveeno-Ausschreibung heißt es "Getting started".
INTRO_RE = re.compile(
    r"getting[\s\-]*started"
    r"|einf(?:ü|ue)hrungs(?:seminar|veranstaltung|workshop|kurs)"
    r"|introductory\s+(?:seminar|session|workshop|course)"
    r"|einstieg\s+in\s+die\s+promotion",
    re.I,
)
INTRO_HEADING = "einführungsveranstaltung"
EVEENO_RE = re.compile(r"^https?://(?:www\.)?eveeno\.com/(\d{6,})/?(?:[?#].*)?$", re.I)
REG_LINK_TEXT_RE = re.compile(r"ausgebucht|anmeld|warteliste|registr|verfügbar|available|book", re.I)
CAL_STATUS_RE = re.compile(
    r"^(?:\d+\s*)?(?:plätze\s+)?(?:verfügbar|frei|warteliste|ausgebucht|geschlossen|abgesagt"
    r"|available|waiting\s*list|sold\s*out|booked\s*out|fully\s*booked)$",
    re.I,
)
CLOSED_RE = re.compile(r"ausgebucht|warteliste|geschlossen|abgesagt|nicht\s+verfügbar|booked|sold\s*out|waiting", re.I)
OPEN_RE = re.compile(r"verfügbar|frei|anmeldung|register|available", re.I)
DATE_RE = re.compile(r"\b\d{1,2}\.\d{1,2}\.\d{4}\b")
ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿"), None)
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


# --------------------------------------------------------------------------- Hilfen

def clean(text: str) -> str:
    text = text.translate(ZERO_WIDTH).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def today() -> str:
    return dt.date.today().isoformat()


def classify(status_text: str) -> str:
    if not status_text:
        return "unknown"
    if CLOSED_RE.search(status_text):
        return "closed"
    if OPEN_RE.search(status_text):
        return "open"
    return "unknown"


def fetch(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = r.encoding or "utf-8"
    return r.text


# --------------------------------------------------------------------------- Programmseite

def tokenize(html: str) -> list[tuple]:
    """Zerlegt die Seite in eine geordnete Folge aus Überschriften, Texten und Links."""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "svg", "template", "head"]):
        t.decompose()
    root = soup.body or soup
    tokens: list[tuple] = []
    for node in root.descendants:
        if isinstance(node, Tag):
            if node.name in HEADING_TAGS:
                text = clean(node.get_text(" "))
                if text:
                    tokens.append(("heading", text, None, int(node.name[1])))
            elif node.name == "a" and node.get("href") and not node.find_parent(HEADING_TAGS):
                tokens.append(("link", clean(node.get_text(" ")), node["href"].strip(), 0))
        elif isinstance(node, NavigableString) and not isinstance(node, Comment):
            if node.find_parent("a") is not None or node.find_parent(HEADING_TAGS) is not None:
                continue
            text = clean(str(node))
            if text:
                tokens.append(("text", text, None, 0))
    return tokens


def _next_link_text(tokens, i):
    for kind, text, _, _ in tokens[i + 1:]:
        if kind == "link":
            return text.lower()
    return ""


def find_intro_section(tokens):
    """Liefert (start, ende, ebene) des Abschnitts 'Einführungsveranstaltungen' oder None."""
    candidates = [
        (lvl, i) for i, (kind, text, _, lvl) in enumerate(tokens)
        if kind == "heading" and INTRO_HEADING in text.lower()
        and not _next_link_text(tokens, i).startswith("zu den")
    ]
    if candidates:
        level, start = min(candidates)  # höchste Gliederungsebene, bei Gleichstand die erste
        end = next(
            (j for j in range(start + 1, len(tokens)) if tokens[j][0] == "heading" and tokens[j][3] <= level),
            len(tokens),
        )
        return start, end, level

    # Rückfallebene ohne Überschriften-Tags: reine Textsuche
    markers = ("überfachliche qualifizierungen", "forschungskolloquien", "lunchlabs", "bw-car rise", "anrechnung")
    for i, (kind, text, _, _) in enumerate(tokens):
        if kind == "text" and text.lower() == "einführungsveranstaltungen" \
                and not _next_link_text(tokens, i).startswith("zu den"):
            end = next(
                (j for j in range(i + 1, len(tokens))
                 if tokens[j][0] == "text" and tokens[j][1].lower().startswith(markers)),
                len(tokens),
            )
            return i, end, 2
    return None


def is_registration_link(text: str, href: str) -> bool:
    low_href, low_text = href.lower(), text.lower()
    if "cleverreach" in low_href or "newsletter" in low_text or "login" in low_text:
        return False
    return bool(EVEENO_RE.match(href)) or bool(REG_LINK_TEXT_RE.search(text))


def event_key(href: str) -> str:
    m = EVEENO_RE.match(href)
    return f"eveeno:{m.group(1)}" if m else f"url:{href.split('#')[0]}"


def parse_page(html: str):
    """Liefert (Veranstaltungen, Abschnitt gefunden?, Anzahl eveeno-Links)."""
    tokens = tokenize(html)
    section = find_intro_section(tokens)
    level = section[2] if section else 2
    events: dict[str, dict] = {}
    buf: list[str] = []
    eveeno_links = 0
    for idx, (kind, text, href, lvl) in enumerate(tokens):
        if kind == "heading":
            if lvl <= level:
                buf = []
            else:
                buf.append(text)
            continue
        if kind == "text":
            buf = (buf + [text])[-40:]
            continue
        if not is_registration_link(text, href):
            if text:
                buf.append(text)
            continue
        if EVEENO_RE.match(href):
            eveeno_links += 1
        key = event_key(href)
        in_section = bool(section) and section[0] < idx < section[1]
        block = " · ".join(buf)
        ev = events.get(key)
        if ev is None:
            events[key] = {
                "title": buf[0] if buf else text,
                "text": block,
                "url": href,
                "dates": sorted(set(DATE_RE.findall(block)), key=_date_sort),
                "in_section": in_section,
                "status_text": {"web": text},
            }
        else:  # dieselbe Veranstaltung taucht mehrfach auf (z. B. LunchLab in zwei Abschnitten)
            ev["in_section"] = ev["in_section"] or in_section
        buf = []
    return events, section is not None, eveeno_links


def _date_sort(d: str):
    try:
        day, month, year = map(int, d.split("."))
        return (year, month, day)
    except ValueError:
        return (0, 0, 0)


# --------------------------------------------------------------------------- eveeno-Kalender

def parse_calendar(html: str) -> dict[str, dict]:
    """Liest den Veranstaltungskalender, den jede eveeno-Eventseite des Verbands mitliefert."""
    soup = BeautifulSoup(html, "html.parser")
    grouped: dict[str, list[str]] = {}
    for a in soup.find_all("a", href=True):
        m = EVEENO_RE.match(a["href"].strip())
        if not m:
            continue
        text = clean(a.get_text(" "))
        if text and text.lower() != "banner image":
            grouped.setdefault(m.group(1), []).append(text)
    events = {}
    for eid, texts in grouped.items():
        status = next((t for t in texts if CAL_STATUS_RE.match(t)), "")
        titles = [t for t in texts if t != status]
        if not status or not titles:
            continue
        title = max(titles, key=len)
        events[f"eveeno:{eid}"] = {
            "title": title,
            "text": title,
            "url": f"https://eveeno.com/{eid}",
            "dates": DATE_RE.findall(title),
            "in_section": False,
            "status_text": {"eveeno": status},
        }
    return events


# --------------------------------------------------------------------------- Benachrichtigung

class Notifier:
    def __init__(self, dry_run: bool):
        self.dry_run = dry_run

    def send(self, title: str, message: str, priority: int = 3, tags=None, click: str | None = None) -> bool:
        if self.dry_run:
            print(f"\n[NACHRICHT p{priority}] {title}\n{message}\n{('Link: ' + click) if click else ''}")
            return True
        payload = {"topic": NTFY_TOPIC, "title": title, "message": message[:3500], "priority": priority}
        if tags:
            payload["tags"] = tags
        if click:
            payload["click"] = click
            payload["actions"] = [{"action": "view", "label": "Zur Anmeldung", "url": click}]
        try:
            r = requests.post(NTFY_SERVER, json=payload, timeout=TIMEOUT)
            r.raise_for_status()
            print(f"Gesendet: {title}")
            return True
        except requests.RequestException as e:
            print(f"Senden fehlgeschlagen ({title}): {e}", file=sys.stderr)
            return False


def describe(ev: dict) -> str:
    lines = [ev["title"]]
    if ev.get("dates"):
        lines.append("Termin: " + ", ".join(ev["dates"]))
    status = ev.get("status_text", {})
    if status:
        lines.append("Status: " + " | ".join(f"{'Website' if k == 'web' else 'eveeno'}: {v}" for k, v in status.items()))
    if ev.get("text") and ev["text"] != ev["title"]:
        lines.append("")
        lines.append(ev["text"][:600])
    return "\n".join(lines)


# --------------------------------------------------------------------------- Ablauf

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"initialized": False, "events": {}, "fail_count": 0, "alerted": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def merge(target: dict, ev: dict) -> None:
    if target is ev:
        return
    target["status_text"].update(ev["status_text"])
    target["in_section"] = target["in_section"] or ev["in_section"]
    if not target.get("dates") and ev.get("dates"):
        target["dates"] = ev["dates"]
    if "eveeno" in ev["status_text"]:
        target["title"] = ev["title"]  # eveeno-Titel ist sauberer als der Textblock der Website


def collect(state: dict) -> tuple[dict, list[str], bool]:
    """Holt beide Quellen. Liefert (Veranstaltungen, Probleme, Website ok?)."""
    current: dict[str, dict] = {}
    problems: list[str] = []
    web_ok = False
    eveeno_ids: list[str] = []

    try:
        web_events, section_found, eveeno_count = parse_page(fetch(PAGE_URL))
        web_ok = True
        current.update(web_events)
        eveeno_ids = [k.split(":")[1] for k in web_events if k.startswith("eveeno:")]
        if not section_found:
            problems.append("structure:Abschnitt 'Einführungsveranstaltungen' nicht gefunden")
        if eveeno_count == 0:
            problems.append("structure:Keine eveeno-Anmeldelinks auf der Seite gefunden")
        print(f"Website: {len(web_events)} Veranstaltungen, Einführungsabschnitt gefunden: {section_found}")
    except Exception as e:  # noqa: BLE001
        problems.append(f"fetch:{e}")
        print(f"Website nicht abrufbar: {e}", file=sys.stderr)

    # Kalender von einer beliebigen eveeno-Seite des Verbands
    candidates = eveeno_ids + ([state["calendar_source"]] if state.get("calendar_source") else [])
    for eid in list(dict.fromkeys(candidates))[:4]:
        try:
            cal = parse_calendar(fetch(f"https://eveeno.com/{eid}"))
        except Exception as e:  # noqa: BLE001
            print(f"eveeno {eid} nicht abrufbar: {e}", file=sys.stderr)
            continue
        if cal:
            state["calendar_source"] = eid
            print(f"eveeno-Kalender: {len(cal)} Veranstaltungen (Quelle {eid})")
            for key, ev in cal.items():
                if key in current:
                    merge(current[key], ev)
                else:
                    current[key] = ev
            break
    else:
        print("eveeno-Kalender nicht auswertbar", file=sys.stderr)

    for ev in current.values():
        ev["intro"] = ev["in_section"] or bool(INTRO_RE.search(ev["title"] + " " + ev.get("text", "")))
        ev["status"] = {src: classify(t) for src, t in ev["status_text"].items()}
    return current, problems, web_ok


def run(dry_run: bool) -> int:
    if not dry_run and not NTFY_TOPIC:
        print("NTFY_TOPIC ist nicht gesetzt.", file=sys.stderr)
        return 2
    state = load_state()
    notifier = Notifier(dry_run)
    baseline = not state.get("initialized")
    current, problems, web_ok = collect(state)
    known = state.setdefault("events", {})
    alerted = state.setdefault("alerted", {})
    new_others = []

    for key, ev in current.items():
        old = known.get(key)
        is_new = old is None
        entry = dict(old) if old else {"first_seen": today(), "notified_intro": False, "status": {}}
        entry_status = dict(entry.get("status", {}))
        ok = True

        if ev["intro"] and not entry.get("notified_intro"):
            if baseline:
                entry["notified_intro"] = True
            else:
                booked = any(s == "closed" for s in ev["status"].values())
                ok = notifier.send(
                    "Neue Einführungsveranstaltung!" + (" (schon ausgebucht)" if booked else ""),
                    describe(ev) + ("\n\nWarteliste prüfen." if booked else "\n\nJetzt anmelden."),
                    priority=5, tags=["rotating_light"], click=ev["url"],
                )
                entry["notified_intro"] = ok
        elif ev["intro"] and not baseline:
            reopened = [src for src, st in ev["status"].items() if entry_status.get(src) == "closed" and st == "open"]
            if reopened:
                ok = notifier.send("Einführungsveranstaltung: Platz frei!", describe(ev),
                                   priority=5, tags=["white_check_mark"], click=ev["url"])
        elif is_new and not baseline and NOTIFY_OTHER_EVENTS:
            new_others.append(ev)

        if ok:  # Status nur übernehmen, wenn eine fällige Nachricht auch rausging
            entry["status"] = {**entry_status, **ev["status"]}
        entry.update(title=ev["title"], url=ev["url"], dates=ev.get("dates", []), intro=ev["intro"], last_seen=today())
        known[key] = entry

    if new_others:
        body = "\n\n".join(f"{e['title']}\n{', '.join(e.get('dates', []))}\n{e['url']}" for e in new_others)
        notifier.send(f"{len(new_others)} neue Veranstaltung(en) im Qualifizierungsprogramm", body, priority=2, tags=["calendar"])

    # Störungen: jeweils nur einmal melden, bis sie behoben sind
    state["fail_count"] = 0 if web_ok else state.get("fail_count", 0) + 1
    if state["fail_count"] >= FAIL_ALERT_AFTER and not alerted.get("fetch"):
        msg = next((p.split(":", 1)[1] for p in problems if p.startswith("fetch:")), "")
        alerted["fetch"] = notifier.send("Monitor: Seite nicht erreichbar",
                                         f"{state['fail_count']} Fehlversuche in Folge.\n{msg}", priority=3, tags=["warning"])
    elif web_ok:
        alerted.pop("fetch", None)

    structure = [p.split(":", 1)[1] for p in problems if p.startswith("structure:")]
    if web_ok and structure and not alerted.get("structure"):
        alerted["structure"] = notifier.send(
            "Monitor: Seitenstruktur geändert",
            "\n".join(structure) + "\nStichwortsuche und eveeno-Kalender laufen weiter, Skript bitte prüfen.\n" + PAGE_URL,
            priority=3, tags=["warning"], click=PAGE_URL)
    elif web_ok and not structure:
        alerted.pop("structure", None)

    if baseline:
        intro_now = [e for e in current.values() if e["intro"]]
        lines = [f"Überwachung läuft. {len(current)} Veranstaltungen erfasst."]
        lines.append("Aktuell als Einführungsveranstaltung erkannt:" if intro_now else "Aktuell keine Einführungsveranstaltung gelistet.")
        for e in intro_now:
            status = ", ".join(e["status_text"].values())
            lines.append(f"• {e['title'][:120]} ({', '.join(e.get('dates', [])) or 'ohne Datum'}) – {status}")
        if structure:
            lines.append("\nAchtung: " + "; ".join(structure))
        if notifier.send("Einführungsseminar-Monitor aktiv", "\n".join(lines), priority=3, tags=["eyes"]):
            state["initialized"] = True

    # Alte Einträge entfernen
    cutoff = (dt.date.today() - dt.timedelta(days=PRUNE_AFTER_DAYS)).isoformat()
    for key in [k for k, v in known.items() if v.get("last_seen", "") < cutoff]:
        del known[key]

    state["checked_on"] = today()  # sorgt für einen Commit pro Tag, damit GitHub den Zeitplan nicht pausiert
    if dry_run:
        print("\n--- Erkannte Veranstaltungen ---")
        for key, ev in sorted(current.items(), key=lambda kv: not kv[1]["intro"]):
            mark = "EINFÜHRUNG" if ev["intro"] else "          "
            print(f"{mark}  {ev['title'][:90]:<90}  {ev.get('dates')}  {ev['status_text']}  [{key}]")
        if problems:
            print("\nProbleme:", problems)
    else:
        save_state(state)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="nur anzeigen, nichts senden oder speichern")
    ap.add_argument("--test", action="store_true", help="Testnachricht an den ntfy-Topic schicken")
    args = ap.parse_args()
    if args.test:
        if not NTFY_TOPIC:
            print("NTFY_TOPIC ist nicht gesetzt.", file=sys.stderr)
            return 2
        ok = Notifier(False).send("Testnachricht", "Der Einführungsseminar-Monitor kann dich erreichen.",
                                  priority=4, tags=["bell"], click=PAGE_URL)
        return 0 if ok else 1
    return run(args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
