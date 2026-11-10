# Entwicklung

## Aufbau

```
app/
  main.py            Flask-App: Oberfläche und JSON-API
  tasks.py           JobManager: Aufträge als Threads, Pausieren/Abbrechen, Übergabe an Ziele, Restore
  backup.py          Sicherung eines Containers (BackupRun), Aufbewahrung, Restore, Klone, Image-Handling
  rules.py           Backup-Umfang: was wird gesichert, Labels, Einstellungen pro Container, Direkt-Pfade
  docker_api.py      Zugriff auf Docker, Host-Pfad-Zuordnung, Hilfs-Container, Labels
  dbdump.py          Erkennung und Dumps von Datenbanken
  scheduler.py       Zeitpläne
  insights.py        Schutzstatus, Kapazitäts-Prognose
  notify.py          Benachrichtigungen
  store.py           SQLite: Aufträge, Vermerke je Ziel, Läufe
  config.py          Umgebung und settings.json
  worker.py          Hilfs-Container-Programm zum Zurückschreiben in Host-Ordner (nur Standardbibliothek)
  cancel.py          Ausnahme für abgebrochene Aufträge
  targets/           Backup-Ziele (base, commvault, veeam, pbs, restic, command)
  static/, templates/ Oberfläche (ein HTML, ein JS, ein CSS, Schriften IBM Plex, Symbole)
tests/               unittest
docs/                diese Dokumentation und die Bilder
```

## Tests

Die Tests laufen im Image, ohne Docker-Zugriff und ohne echte Backup-Lösung (restic mit einem echten lokalen Repository, Veeam gegen einen nachgebildeten Server, Skripte mit echter Shell):

```bash
docker build -t moby-disk:dev .
docker run --rm --network none --tmpfs /tmp:exec -v "$PWD/tests:/tests:ro" \
  --entrypoint python moby-disk:dev -m unittest discover -s /tests
```

`--tmpfs /tmp:exec` ist nötig, weil einige Tests Skripte ausführen und der Proxmox-Client `O_TMPFILE` braucht.

Zusätzliche Tests gegen einen **echten Proxmox Backup Server** laufen, wenn `PBS_TEST_REPOSITORY`, `PBS_TEST_PASSWORD` und `PBS_TEST_FINGERPRINT` gesetzt sind:

```bash
docker run -d --name pbs --tmpfs /run -p 8007:8007 dockurr/proxmox-backup
docker exec pbs proxmox-backup-manager user generate-token root@pam moby      # Geheimnis notieren
docker exec pbs proxmox-backup-manager acl update /datastore/data DatastoreAdmin --auth-id 'root@pam!moby'
docker exec pbs proxmox-backup-manager cert info | grep -i fingerprint
```

Das Test-Image muss den Server erreichen (gleiches Docker-Netz oder dessen IP).

Der Ende-zu-Ende-Ablauf (Backup eines Wegwerf-Containers, Restore als Klon, Pause und Abbruch) lässt sich mit einer zweiten Instanz auf anderem Port, eigenem `DATA_DIR`/`BACKUP_ROOT`
und `DISABLE_SCHEDULER=1` prüfen, ohne die produktive zu berühren.

## Ein neues Ziel hinzufügen

1. In `app/targets/<name>.py` von `Target` (`app/targets/base.py`) ableiten: `id`, `label`, `kind` (`pull` oder `push`), `capabilities`.
2. Mindestens implementieren: `configured()`, `test()`, `start(items, options, log_cb)` (gibt ein `Run` zurück) und `wait(run, progress_cb)` (gibt ein `RunResult`).
   Je nach Fähigkeit: `list_folders(container)`, `list_containers()`, `fetch(container, folder, dest, …)`, `validate_entries(entries)` (für Ziele ohne Ordnerliste), `cancel(run, log_cb)`,
   `after_success(run, log_cb)`, `info()` (angezeigte Werte – **nie Geheimnisse**).
3. Lange Aufrufe mit `run_process(..., cancel=options.get("cancel_check"))` starten, damit „Abbrechen“ sie beendet; in Schleifen `options["cancel_check"]()` aufrufen.
4. In `app/targets/__init__.py` in `REGISTRY` eintragen und in `app/static/app.js` (`TARGET_HELP`, `TARGET_DOCS`) Beschreibung und `.env`-Beispiel ergänzen.
5. Tests schreiben (`tests/`), die Variablen in `.env.example` und [Backup-Ziele](targets.md) dokumentieren.

`RunResult.done` erlaubt Teilerfolge (nur die übernommenen Container werden als bestätigt vermerkt); schlägt ein Ziel fehl, schlägt der Auftrag fehl, die lokale Sicherung bleibt.

## JSON-API (Auswahl)

Alle Routen außer der Anmeldung verlangen eine Sitzung (`POST /api/login` mit `{"password": …}`), falls `APP_PASSWORD` gesetzt ist.

| Route | Zweck |
| --- | --- |
| `GET /api/status` | Docker-, Ziele-Status (`targets` mit `enabled`, `ready`, `info`), Pfade, Host-Freigaben |
| `GET /api/containers` | Container mit Schutzstatus, Mount-Plan, Einstellungen, Stack |
| `GET /api/containers/<name>/versions` | lokale Kopien und Stände je Ziel |
| `GET /api/containers/<name>/plan?sizes=1` | Entscheidung je Mount, mit Größen |
| `POST /api/backup` | Backup starten (`containers`, `mode`, `image_mode`, `targets`, `retention`, `format`, `require_cv` …) |
| `POST /api/restore` | Restore starten (`container`, `folder`, `source`, `target` = `original`/`clone`, …) |
| `GET /api/jobs`, `GET /api/jobs/<id>` | Aufträge mit Protokoll |
| `POST /api/jobs/<id>/pause`, `/resume`, `/cancel` | Auftrag steuern (409, wenn nicht mehr möglich) |
| `GET /api/backups`, `POST /api/backups/delete` | Sicherungen lokal und in Zielen; löschen |
| `GET /api/targets`, `POST /api/targets/<id>/test`, `/sync` | Ziele, Verbindungstest, Abgleich (Auftrag) |
| `GET/POST/PUT/DELETE /api/schedules…` | Zeitpläne, `POST …/<id>/run` startet sofort |
| `GET/PUT /api/rules`, `PUT /api/rules/containers/<name>` | Backup-Umfang, Regeln und Einstellungen je Container |
| `GET/PUT /api/notify`, `POST /api/notify/test` | Benachrichtigungen |
| `GET /api/forecast` | Kapazitätsprognose |
| `GET/POST /api/cv/content…`, `/api/settings…` | Commvault-spezifisch: Subclient-Inhalt, Verbindung, Client/Subclient |

## Hinweise zum Betrieb von Testinstanzen

Eine Testinstanz neben der produktiven braucht eigenen Port, eigenes `DATA_DIR` und `BACKUP_ROOT` und das Label `mobydisk.backup=false`
(damit „alle mit Daten“ sie nicht sichert). Nie dasselbe `BACKUP_ROOT` und denselben Commvault-Subclient wie die produktive Instanz verwenden.
