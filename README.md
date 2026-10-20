# Einführungsseminar-Monitor

Überwacht das Qualifizierungsprogramm des Promotionsverbands BW und schickt eine Push-Nachricht aufs Handy, sobald eine neue Einführungsveranstaltung erscheint.

Geprüft werden alle 10 Minuten zwei Quellen:

1. Die Programmseite https://www.promotionsverband-bw.de/qualifizierungsprogramm, Abschnitt "Einführungsveranstaltungen".
2. Der Veranstaltungskalender auf eveeno.com, über den der Verband die Anmeldungen abwickelt. Neue Termine können dort stehen, bevor die Website aktualisiert ist.

## Benachrichtigungen

| Ereignis | Priorität |
|---|---|
| Neue Veranstaltung im Abschnitt "Einführungsveranstaltungen" oder mit Titel wie "Getting started", "Einführungsseminar" | maximal, mit Link zur Anmeldung |
| Bei einer ausgebuchten Einführungsveranstaltung werden Plätze frei | maximal |
| Sonstige neue Veranstaltung im Programm | niedrig, ohne Ton (abschaltbar) |
| Seite 6 Läufe in Folge nicht erreichbar, oder Seitenstruktur geändert | normal, einmalig bis behoben |

Beim ersten Lauf wird nur der aktuelle Stand erfasst und eine Bestätigung geschickt, die auflistet, was gerade als Einführungsveranstaltung erkannt wird.

## Einrichtung

### 1. ntfy auf dem Handy

ntfy ist ein kostenloser Push-Dienst ohne Konto. Nachrichten gehen an einen "Topic", also einen frei gewählten Kanalnamen. Wer den Namen kennt, kann mitlesen, deshalb einen zufälligen Namen wählen.

1. App "ntfy" installieren (Android: Play Store oder F-Droid, iOS: App Store).
2. Topic-Namen erzeugen, z. B. `pv-bw-einfuehrung-` plus 12 zufällige Zeichen.
3. In der App diesen Topic abonnieren.
4. Für Benachrichtigungen mit maximaler Priorität in den App-Einstellungen erlauben, dass sie "Nicht stören" durchbrechen, falls gewünscht.

Weitere Personen, z. B. Lukas, abonnieren einfach denselben Topic.

### 2. GitHub-Repository

GitHub Actions ist der Automatisierungsdienst von GitHub. Ein Workflow läuft dort nach Zeitplan auf einem Server von GitHub, der eigene Rechner muss nicht laufen.

1. Neues **öffentliches** Repository anlegen. In öffentlichen Repositories sind Actions-Minuten unbegrenzt kostenlos. In privaten Repositories reichen die 2000 Freiminuten pro Monat nur für etwa einen Lauf pro halbe Stunde. Im Repository liegt nichts Vertrauliches, der Topic-Name steht nur im Secret.
2. Alle Dateien dieses Ordners hochladen, inklusive `.github/workflows/monitor.yml`.
3. Unter *Settings → Secrets and variables → Actions → New repository secret* ein Secret anlegen: Name `NTFY_TOPIC`, Wert der Topic-Name. Ein Secret ist ein verschlüsselt gespeicherter Wert, der im Log nicht angezeigt wird.
4. Unter *Settings → Actions → General → Workflow permissions* "Read and write permissions" aktivieren, damit der Workflow `state.json` zurückschreiben kann.
5. Unter *Actions → Einführungsseminar-Monitor → Run workflow* den ersten Lauf manuell starten. Danach sollte die Bestätigung auf dem Handy ankommen.

Im Log des ersten Laufs prüfen: Die Zeile `Einführungsabschnitt gefunden: True` und eine Zahl größer 0 bei `eveeno-Kalender` müssen erscheinen.

### Optionen

Im Workflow unter `env` setzbar:

* `NOTIFY_OTHER_EVENTS: "0"` schaltet die Meldungen zu sonstigen neuen Veranstaltungen ab.
* `FAIL_ALERT_AFTER` legt fest, nach wie vielen Fehlversuchen in Folge gewarnt wird (Standard 6).

## Lokal testen

```
pip install -r requirements.txt
python monitor.py --dry-run          # zeigt erkannte Veranstaltungen, sendet und speichert nichts
NTFY_TOPIC=<topic> python monitor.py --test   # Testnachricht
```

Unter Windows statt `NTFY_TOPIC=<topic> python ...` vorher `set NTFY_TOPIC=<topic>` ausführen.

## Grenzen

* GitHub startet geplante Läufe nicht exakt. Verzögerungen von 5 bis 20 Minuten sind üblich, zu Spitzenzeiten auch mehr.
* Erkannt wird, was auf der Website oder im eveeno-Kalender steht. Wird ein Termin zuerst nur über den Newsletter verschickt, kommt die Meldung erst mit der Veröffentlichung online.
* Ändert der Verband den Seitenaufbau grundlegend, meldet das Skript das einmalig. Die Stichwortsuche im eveeno-Kalender läuft unabhängig davon weiter.
* GitHub pausiert Zeitpläne in öffentlichen Repositories nach 60 Tagen ohne Aktivität. Das Skript schreibt deshalb täglich ein Datum in `state.json`, der Commit zählt als Aktivität.
