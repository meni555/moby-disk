# Bedienung

Die Oberfläche hat sechs Bereiche; der aktive steht in der Seitenleiste markiert.

| Bereich | Wofür |
| --- | --- |
| **Container** | alle Container des Hosts mit Schutzstatus, Daten, lokalen Sicherungen; Backup, Versionen, Regeln und Einstellungen je Container |
| **Jobs** | laufende und beendete Aufträge mit Protokoll; pausieren, fortsetzen, abbrechen |
| **Backups** | alle Sicherungen lokal und in den Zielen; filtern, löschen, abgleichen |
| **Kapazität** | Belegung des Backup-Ordners und Prognose |
| **Zeitpläne** | automatische Backups |
| **Einstellungen** | Benachrichtigungen, Backup-Umfang, Backup-Ziele (je Ziel eine Rubrik) |

## Container

- **Spalten:** Status des Containers, **Schutz** (Geschützt / Veraltet / Nie gesichert / Daten extern / Direkt über CV / Ausgenommen), Daten (Volumes, Bind-Mounts,
  übersprungene und direkt gesicherte Mounts), Anzahl und Größe lokaler Sicherungen, dazu wie viele Sicherungen in Zielen vermerkt sind.
- **Schutz** wird gegen das Schutzziel (Standard 36 Stunden, einstellbar) berechnet. Sicherungen zählen mit, die nur noch in einem Ziel liegen („nur im Ziel“).
- **Filter:** Suche, Status (Alle/Läuft/Gestoppt), „Nur mit Daten“, „Ohne Sicherung“, „Ungeschützt“, Compose-Stack, Sortierung. Die Einstellung bleibt im Browser gemerkt.
- **Aufgeklappte Zeile:** zeigt jeden Mount mit der Entscheidung (wird gesichert / übersprungen und warum) und Schaltflächen **Ausschließen**, **Trotzdem sichern**,
  **Über CV sichern** (nur mit Commvault). Darunter die [Einstellungen je Container](scope-and-settings.md#einstellungen-pro-container).
- **Mehrfachauswahl:** mehrere Container ankreuzen (Umschalt-Klick für einen Bereich, Klick auf den Stack-Namen für einen ganzen Stack) und gemeinsam sichern.

## Stacks

Der Reiter **Stacks** zeigt jedes Compose-Projekt als Karte: seine Container (grün = läuft), den letzten gemeinsamen Backup-Lauf und in welchen Quellen er vollständig
vorliegt (lokal und je Ziel – ein Haken heißt: alle Container des Laufs sind dort vorhanden).

- **Stack sichern** öffnet den Backup-Dialog mit allen Containern des Stacks; sie stehen für den gemeinsamen Zeitpunkt zusammen still (Stopp in umgekehrter, Start in
  Abhängigkeitsreihenfolge).
- **Wiederherstellen …** listet die Backup-Läufe des Stacks, neueste zuerst. Je Lauf gibt es einen Knopf pro Quelle (Lokal, Commvault, Veeam, …), nur für Quellen, in denen
  der **ganze** Lauf vorhanden ist. Der Restore-Dialog ist mit „Ganzen Stack zurücksetzen“ vorbelegt: erst werden alle Sicherungen geholt, dann alle Container gestoppt, zurückgespielt und
  in der richtigen Reihenfolge gestartet. Fehlende Container werden neu angelegt.
- Ein Hinweis zeigt Container, die erst nach dem Lauf in den Stack kamen – sie sind in diesem Stand nicht enthalten. Stacks, die es auf dem Host nicht mehr gibt, bleiben für den Restore sichtbar.
- Einen Lauf gibt es nur, wenn der Stack **gemeinsam** gesichert wurde (Option „Container eines Stacks gemeinsam stoppen“, standardmäßig an).

## Ein Backup starten

Container-Zeile → **Backup** (oder mehrere auswählen → Sammel-Backup). Im Dialog:

- **Konsistenz:** *Live* (der Container läuft weiter, bei Datenbanken evtl. inkonsistent) oder *Konsistent* (der Container wird kurz gestoppt und danach
  wieder gestartet). Pre-/Post-Hooks und Datenbank-Dumps laufen in beiden Fällen.
- **Docker-Images:** nur selbst gebaute (Standard), keine oder alle.
- **Archivformat:** `tar` (Standard) oder `tar.gz`. Unkomprimierte tar-Dateien deduplizieren deutlich besser; `tar.gz` ändert sich durch den gzip-Zeitstempel bei jedem Lauf.
- **Lokal behalten:** die letzten *N* Sicherungen und/oder höchstens *X* Tage alt. Die neueste bleibt immer.
- **Danach an Backup-Ziele übergeben:** ein oder mehrere Ziele ankreuzen (Commvault mit Stufe Inkrementell/Voll). Ohne Ziel bleibt die Sicherung lokal.
- **Lokale Kopien nur löschen, wenn bestätigt:** siehe [Mehrere Ziele und Aufbewahrung](targets.md#mehrere-ziele-und-aufbewahrung).
- **Ganze Compose-Stacks sichern:** ergänzt fehlende Mitglieder der gewählten Stacks.

Moby Disk sichert Container **nacheinander**; die Mitglieder eines Compose-Stacks ruhen gemeinsam (Stopp in umgekehrter, Start in Abhängigkeitsreihenfolge).
Pro Container läuft immer nur ein Backup oder Restore.

## Jobs

Jeder Backup-, Restore- und Abgleichvorgang ist ein Auftrag mit Fortschritt und Protokoll. Aufträge und Protokolle liegen in `helper.db` und überstehen Neustarts.
Ein Auftrag, der beim Neustart noch lief, steht danach als „Fehlgeschlagen – unterbrochen“ im Verlauf.

**Pausieren, fortsetzen, abbrechen:** Schaltflächen am Auftrag, auch über `POST /api/jobs/<id>/pause|resume|cancel`.

- Gewirkt wird nur an **sicheren Stellen**: zwischen zwei Containern oder Stacks, vor und zwischen den Zielen, beim Warten auf ein Ziel, beim Abholen eines
  Restores. Ein laufender Container wird nie in halbem Zustand angehalten – eine Pause greift deshalb erst nach dem aktuellen Container, bis dahin steht
  „Pause angefordert …“. Pausiert Moby Disk, laufen bereits gestartete Läufe im Ziel (z. B. ein Commvault-Job) weiter.
- **Abbrechen** beendet eine laufende Archivierung (geprüft alle 200 Dateien und beim Image-Export), entfernt die unvollständige Sicherung und startet den Container
  wieder. Bereits fertig gesicherte Container bleiben lokal erhalten, werden aber nicht mehr an die Ziele übergeben. Ein bereits laufender Lauf im Ziel wird mit
  gestoppt (soweit das Ziel das kann). Der Auftrag endet als **„Abgebrochen“** (kein Fehler, keine Benachrichtigung).
- Ab dem Moment, in dem ein **Restore** Daten zurückschreibt, ist der Auftrag **gesperrt** („nicht mehr unterbrechbar“) – ein halber Restore wäre gefährlicher als ein vollständiger.
- Sehr lange Einzelschritte (ein Datenbank-Dump, eine einzelne riesige Datei) lassen sich erst danach unterbrechen.
- Pausierte Aufträge zählen als aktiv: der Zeitplan startet keinen zweiten Lauf, und Sicherungen lassen sich währenddessen nicht löschen.

## Backups

Alle Sicherungen auf einen Blick, aus den Spalten **Container, Erstellt, Größe, Inhalt, Backup-Ziele**.

- **Filter:** *Alle*, *Lokal*, *Nur in Zielen*; Suche; Sortierung.
- **„nur in …“:** die lokale Kopie ist gelöscht, die Sicherung liegt aber in einem Ziel. Der Knopf **Versionen** führt zum Restore.
- **Löschen:** Sicherungen ankreuzen → „Auswahl löschen“. Kopien in den Zielen bleiben unberührt; der Vermerk bleibt als „nur in …“ stehen. Nicht
  bestätigte Sicherungen werden vor dem Löschen eigens markiert.
- **Mit Zielen abgleichen:** liest die Stände aller aktiven Ziele neu ein (ein Auftrag je Ziel). Das findet auch ältere Sicherungen, die diese Installation
  nicht selbst vermerkt hat, und entfernt Vermerke, die das Ziel nicht mehr hat.

## Zeitpläne

- **Wann:** Wochentage und Uhrzeit (Zeitzone aus `TZ`). Verpasste Läufe (Moby Disk war aus) werden bis zu 4 Stunden nachgeholt; ältere gelten als „verpasst“ und lösen eine Benachrichtigung aus.
- **Was:** alle Container mit Daten (auch später neu hinzugekommene) oder eine feste Auswahl. Container mit `mobydisk.backup=false` und Klone sind nie dabei.
- **Wie:** Konsistenz, Images, gemeinsames Stoppen von Stacks, Archivformat, lokale Aufbewahrung, Ziele mit Stufe.
- **Ablauf:** lokal sichern → Aufbewahrung anwenden → an die Ziele übergeben. „Jetzt ausführen“ startet einen Plan sofort.

Die Zeitpläne liegen in `DATA_DIR/schedules.json`.

## Kapazität

Größentrend je Container (aus den Manifesten), Läufe aus den aktiven Zeitplänen und Aufbewahrung ergeben die Belegung in 30/90/365 Tagen und die Zeit bis „Speicher voll“
(abzüglich einer Reserve für einen Restore). Mit „Was wäre wenn“ lassen sich Aufbewahrung und Häufigkeit durchspielen. Unvollständige Sicherungsordner früherer Abbrüche lassen sich hier aufräumen.

## Benachrichtigungen

Unter **Einstellungen → Benachrichtigungen**: Ereignisse (fehlgeschlagen, erfolgreich, Zeitplan verpasst, Tagesbericht bei Auffälligkeiten/immer), Schutzziel und
Warnschwellen für den Speicher, und Kanäle:

- **Webhook** (JSON), **ntfy**, **E-Mail (SMTP)**,
- **Heartbeat** (Uptime Kuma, Healthchecks.io): wird nach Zeitplan-Läufen aufgerufen, bei Fehlern mit `/fail`.

Abgebrochene Aufträge lösen keine Benachrichtigung aus. Passwörter und Tokens der Kanäle werden gespeichert, aber nie zurück in die Oberfläche geschickt
(leer lassen = unverändert).

## Logs

Der Reiter **Logs** zeigt, was Moby Disk tut – ausführlicher als das Protokoll eines einzelnen Auftrags:

- **Was protokolliert wird:** Aufträge (Start, Ende, Fehler mit Traceback), jede Änderung per API mit Statuscode und Dauer, Anmeldungen, Zeitpläne, Aufrufe an Commvault und Veeam,
  Docker (Container stoppen/starten), Benachrichtigungen und Änderungen an den Einstellungen (nur die **Namen** der geänderten Werte).
- **Filter:** Mindeststufe (Debug, Info, Warnung, Fehler), Bereich, Textsuche. Ein Klick auf eine Auftrags-ID `[a1b2c3d4e5f6]` zeigt nur diesen Auftrag. **Live** holt alle 3 Sekunden Neues.
- **Aufzeichnung ab …** legt fest, wie ausführlich überhaupt mitgeschrieben wird (Standard Info; gilt sofort und bleibt gespeichert). **Debug** ergänzt jede Anfrage – auch die Abfragen
  an die Backup-Ziele, wenn ein Ziel Probleme macht. Wieder auf Info zurückstellen, wenn der Fehler gefunden ist.
- **Dateien:** `data/logs/mobydisk.jsonl` (eine JSON-Zeile je Eintrag), rotierend 5 × 5 MB, übersteht Neustarts. **Herunterladen** liefert alles als Text; dasselbe steht auch in `docker logs`.
- **Zugangsdaten** (Passwörter, Tokens, Schlüssel aus der Umgebung) werden vor dem Schreiben durch `***` ersetzt – im Text und im Traceback.

## Einstellungen

Links eine Unternavigation: **Allgemein** (Benachrichtigungen, Backup-Umfang) und **Backup-Ziele** (Übersicht und je Ziel eine Rubrik mit den eingestellten Werten,
Verbindungstest, Formular zur **Einrichtung** und Hinweisen; die Ziele stehen immer in der Reihenfolge Commvault, Veeam, Proxmox Backup Server, restic, Befehl/Skript). Änderungen im Formular gelten sofort, ohne Neustart, und werden nicht gespeichert, solange ein Auftrag läuft. Zugangsdaten werden nirgends angezeigt (ein leeres Passwortfeld lässt den gespeicherten Wert unverändert). Beim Veeam-Job lädt „Jobs laden“ die vorhandenen Jobs aus Veeam in eine Auswahlliste. Werte in der `.env` gelten weiter; ein Wert im Formular hat Vorrang.

## Design

Hell und dunkel (Schalter in der Seitenleiste, Standard nach Systemeinstellung). Auf schmalen Bildschirmen wandert die Navigation nach oben. Ein vor einem Update
geöffneter Browser-Tab zeigt „Moby Disk wurde aktualisiert – Neu laden“.
