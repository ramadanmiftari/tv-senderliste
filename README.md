# TV-Senderliste

Eine sich selbst prüfende Senderliste (M3U) mit Programmführer (XMLTV) für deutsche, türkische, albanische,
kosovarische und internationale Sender. **Nur frei und legal empfangbare Streams** direkt von den Servern der
Sender, keine Weiterleitungen Dritter.

- Senderliste: [`playlist.m3u`](playlist.m3u)
- Programmführer: [`guide.xml.gz`](guide.xml.gz) (in der Liste schon verlinkt)
- Prüfbericht der letzten Nacht: [`report.md`](report.md)

## So funktioniert es

Jede Nacht um 03:17 UTC baut GitHub Actions ([`update.yml`](.github/workflows/update.yml)) die Liste neu:

1. Holt die Länderlisten von [Free-TV/IPTV](https://github.com/Free-TV/IPTV) (nur frei empfangbare Sender)
   und ausgewählte Einträge aus [iptv-org/iptv](https://github.com/iptv-org/iptv) ([`sources.json`](sources.json)).
2. Behält nur Streams, deren Server auf [`allowlist.txt`](allowlist.txt) steht.
3. Prüft jeden Stream. Ein Stream, der 3 Nächte in Folge ausfällt, fliegt raus, bis er wieder läuft.
   Geogesperrte Sender (nur in Deutschland/der Türkei sichtbar) werden nie wegen der Prüfung entfernt,
   weil die GitHub-Server in den USA stehen.
4. Nimmt pro Sender den besten Stream (höchste Auflösung, https) und vergibt feste Kanalnummern.
5. Baut den Programmführer aus [epgshare01](https://epgshare01.online) nur für diese Sender.

## Anpassen

- **Sender hinzufügen:** Quelle in `sources.json` eintragen, Server in `allowlist.txt` aufnehmen.
- **Sender entfernen:** Muster in `exclude_names` in `sources.json` eintragen.
- **Sofort neu bauen:** Actions > "Senderliste aktualisieren" > Run workflow.
- Lokal testen: `python curate.py` (nur Python 3, keine Zusatzpakete).
