# Änderungen

## Unveröffentlicht

Moby Disk ist der Nachfolger des „CV Docker Helper“ (Commvault-Anbindung) und wird zur Veröffentlichung vorbereitet.

### Neu

- **Reiter „Logs“ und zentrales Logging:** Aufträge, API-Zugriffe, Anmeldungen, Zeitpläne, Commvault/Veeam, Docker und Benachrichtigungen werden mit Stufe und Bereich protokolliert (Datei `data/logs/mobydisk.jsonl`, rotierend). Filter, Suche, Live-Ansicht, Download, einstellbare Ausführlichkeit; Zugangsdaten werden maskiert. Fehler tragen jetzt immer Typ und Traceback – auch Ausnahmen ohne Meldung.
- **Reiter „Stacks“:** je Compose-Stack Container, letzter gemeinsamer Backup-Lauf und Verfügbarkeit je Quelle; „Stack sichern“ und „Wiederherstellen“ aus einer Liste der Läufe (lokal oder aus einem Ziel), ganzer Stack gemeinsam.
- **Ziele im Webinterface einrichten:** Formular je Ziel (auch Commvault) mit Aktiv-Schalter; Werte gelten sofort, liegen in `data/target-config.json` und überstehen jedes Neuerstellen des Containers. Vorrang vor der `.env`; Zugangsdaten werden nie angezeigt. Veeam-Job als Auswahlliste („Jobs laden“).
- **Feste Reihenfolge der Ziele:** Commvault, Veeam, Proxmox Backup Server, restic, Befehl/Skript.
- Anmeldeseite mit großem Logo; Inhalt rückt näher an die Navigation.
- **Veeam-API-Version automatisch:** höchste vom Server angebotene Version (Datei- und Agent-Jobs gibt es erst ab API 1.3); Jobs werden auch über die Zustandsliste gefunden.
- **Veeam-Anmeldung:** Hinweis auf das Benutzernamen-Format (Domäne/Rechnername) bei abgelehnter Anmeldung; keine automatischen Wiederholungen, damit Veeam das Konto nicht sperrt.
- **Backup-Ziele als Schnittstelle:** Commvault, Veeam (REST), Proxmox Backup Server, restic und „Befehl/Skript“; mehrere Ziele pro Lauf; lokale Aufbewahrung
  löscht nur, was in allen gewählten Zielen bestätigt ist.
- **Aufträge pausieren und abbrechen**, an sicheren Stellen; abgebrochene Archivierungen räumen auf und starten den Container wieder; laufende Läufe im Ziel werden mitgestoppt.
- **Einstellungen neu gegliedert:** Unternavigation, eine Rubrik je Ziel mit Status (auch für nicht aktivierte Ziele), Verbindungstest und `.env`-Beispiel.
- **Neues Erscheinungsbild** (hell/dunkel, Seitenleiste), Logo, Schriften IBM Plex.
- **Sicherungen „nur in Ziel“:** lokal gelöschte Sicherungen bleiben sichtbar und lassen sich daraus wiederherstellen; Abgleich mit den Zielen.
- Datenbank-Dumps, Hooks und Einstellungen je Container, Compose-Stacks, Restore als Klon, Host-Freigaben mit Restore über Hilfs-Container, nur selbst gebaute Images
  (öffentliche exakt per Digest), Direkt-Pfade in Commvault, Synthetic Full.
- Label-Präfix `mobydisk.*` (ältere `cvhelper.*` gelten weiter).

### Behoben

- **Veeam: Auftrag schlug nach erfolgreichem Lauf mit „FEHLER: “ fehl.** Beim Aufräumen fragte Moby Disk bei Veeam eine Ordnerliste ab, die es dort nicht gibt. Für Ziele ohne Ordnerliste gelten nun die Vermerke erfolgreicher Läufe als Bestätigung. Fehler bei Aufbewahrung und Abgleich lassen einen sonst erfolgreichen Auftrag nicht mehr fehlschlagen (Warnung im Protokoll).
- Die eigene Datenbank wird beim Selbst-Backup als SQLite-Snapshot gesichert statt als laufende Datei.
- Nach einem Neuladen der Seite erscheint bei gesetztem `APP_PASSWORD` nicht mehr erneut die Anmeldung.
- Der Abgleich entfernt Vermerke auch dann, wenn ein Ziel für einen Container keinen Stand mehr kennt.
- Meldet ein Ziel keinen Erfolg, schlägt der Auftrag fehl (vorher nur eine Warnung im Protokoll).
- Die Commvault-Backup-Stufe (Voll/Inkrementell) wird jetzt wirksam übergeben.
- Zeitpläne der Vorgängerversion zeigen ihren Commvault-Schritt als Ziel.

### Umstieg

Siehe [Umstieg vom CV Docker Helper](docs/installation.md#umstieg-vom-cv-docker-helper).
