# Installation und Betrieb

## Voraussetzungen

- Ein Linux-Host mit **Docker** und dem Compose-Plugin. Moby Disk läuft selbst als Container.
- Zugriff auf den Docker-Socket und auf `/var/lib/docker/volumes` (beides wird eingebunden).
- Ein Ordner für die lokalen Sicherungen (Standard `/opt/docker-backups`) mit genug Platz. Vor jedem Backup prüft
  Moby Disk, ob der freie Platz für die Größe der letzten Sicherung plus 512 MB Puffer reicht; beim Packen bricht es ab,
  sobald weniger als 512 MB frei sind.
- Mindestens ein Backup-Ziel – sonst bleiben die Sicherungen lokal (siehe [Backup-Ziele](targets.md)).
- Für **Commvault** zusätzlich: ein File-System-Agent auf dem Host, der den Backup-Ordner in einem Subclient sichert,
  und ein REST-Benutzer. Den Unterordner `.staging` im Subclient ausnehmen (nur kurzlebige Restore-Daten).

## Schnellstart

```bash
git clone https://github.com/meni555/moby-disk.git moby-disk
cd moby-disk

sudo mkdir -p /opt/docker-backups
cp .env.example .env
chmod 600 .env
nano .env                    # APP_PASSWORD, TARGETS und die Zugangsdaten der Ziele eintragen

docker compose up -d --build
```

Die Oberfläche läuft danach auf `http://<host>:8484` (die mitgelieferte Compose-Datei bindet sie nur an
`127.0.0.1`). Für Zugriff von anderen Rechnern einen Reverse-Proxy mit TLS davorsetzen (siehe unten) – nicht den Port
einfach freigeben.

Beim ersten Öffnen: mit `APP_PASSWORD` anmelden, unter **Einstellungen → Backup-Ziele** den Verbindungstest des
gewünschten Ziels ausführen und ein erstes Backup eines Test-Containers starten.

## Die Compose-Datei

```yaml
services:
  moby-disk:
    build: .
    image: moby-disk:latest
    container_name: moby-disk
    restart: unless-stopped
    env_file: .env
    environment:
      DOCKER_SOCK: /var/run/docker.sock
      VOLUMES_ROOT: /var/lib/docker/volumes
      BACKUP_ROOT: /opt/docker-backups
      DATA_DIR: /data
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - /var/lib/docker/volumes:/var/lib/docker/volumes
      - /opt/docker-backups:/opt/docker-backups
      - ./data:/data
      # Host-Pfade anderer Container – nur lesend und unter demselben Pfad:
      # - /home/<user>:/home/<user>:ro
      # - /opt/stacks:/opt/stacks:ro
      # Eigene Skripte für das Ziel „Befehl/Skript“:
      # - ./scripts:/scripts:ro
    ports:
      - "127.0.0.1:8484:8000"
```

Wichtig:

- **`BACKUP_ROOT` muss auf dem Host und im Container derselbe Pfad sein.** Moby Disk schreibt dort, und die Backup-Lösung
  sichert genau diesen Host-Pfad. Bei Commvault sucht Moby Disk die Stände unter diesem Pfad – ein anderer Pfad findet
  nichts (typischer Fehler nach einem Umzug).
- **`DATA_DIR` (`./data`)** enthält alles, was Moby Disk sich merkt (siehe unten). Auf einem Dateisystem mit
  `O_TMPFILE`-Unterstützung (ext4, xfs, tmpfs – nicht NFS) ablegen, sonst scheitert der Proxmox-Backup-Client beim Restore.
- `/var/lib/docker/volumes` wird **schreibend** eingebunden, weil Restores dort Daten zurückspielen.
- Läuft Docker mit einem anderen Datenverzeichnis (`data-root`), `VOLUMES_ROOT` und den Mount entsprechend anpassen.

## Umgebungsvariablen

Die Ziele lassen sich bequem in der Oberfläche einrichten (**Einstellungen → Backup-Ziele → [Ziel] → Einrichtung**); das ist der empfohlene Weg. Die Werte liegen dann in `data/target-config.json` (Rechte 600), gelten sofort ohne Neustart und überstehen jedes Neuerstellen des Containers. Alternativ oder zusätzlich lassen sie sich in der `.env` festlegen – ein Wert aus der Oberfläche hat Vorrang, ein geleertes Feld fällt wieder auf die `.env` zurück. Zugangsdaten zeigt die Oberfläche nie an.

| Variable | Bedeutung | Standard |
| --- | --- | --- |
| `APP_PASSWORD` | Passwort der Oberfläche. Leer = keine Anmeldung (nur im abgeschotteten Netz sinnvoll). | – |
| `SESSION_SECRET` | Schlüssel für die Anmelde-Cookies. Ohne Angabe zufällig pro Start: nach jedem Neustart ist eine neue Anmeldung nötig. | zufällig |
| `TZ` | Zeitzone für Zeitpläne und Anzeige. | `Europe/Berlin` |
| `TARGETS` | Aktive Backup-Ziele, kommagetrennt: `commvault`, `veeam`, `pbs`, `restic`, `command`. | `commvault` |
| `BACKUP_ROOT` | Ordner der lokalen Sicherungen. | `/opt/docker-backups` |
| `DATA_DIR` | Einstellungen, Verlauf, Zeitpläne, Caches. | `/data` |
| `DOCKER_SOCK` | Docker-Socket im Container. | `/var/run/docker.sock` |
| `VOLUMES_ROOT` | Docker-Volumes im Container. | `/var/lib/docker/volumes` |
| `DISABLE_SCHEDULER` | `1` schaltet die Zeitpläne ab (nützlich für Testinstanzen). | – |

Die Variablen der einzelnen Ziele stehen bei [Backup-Ziele](targets.md).

## Was in `DATA_DIR` liegt

| Datei | Inhalt |
| --- | --- |
| `helper.db` | SQLite: Auftragsverlauf mit Protokollen, welche Sicherungen in welchem Ziel bestätigt sind, Läufe von Stacks. |
| `settings.json` | Einstellungen aus der Oberfläche (gewählter Commvault-Client/Subclient, Benachrichtigungen, Backup-Umfang, Regeln je Container). |
| `schedules.json` | Zeitpläne. |
| `restic-cache/`, `pbs-cache/` | Caches der Programme (nur wenn das Ziel genutzt wird). |

Beim Sichern von Moby Disk selbst wird `helper.db` als konsistenter SQLite-Snapshot gesichert, nicht als laufende Datei.

## Reverse-Proxy mit TLS

Moby Disk spricht nur HTTP und besitzt über den Docker-Socket Root-Rechte auf dem Host. Zugriff aus dem Netz deshalb nur
über einen Reverse-Proxy mit TLS, am besten zusätzlich nur aus dem internen Netz. Beispiel Caddy:

```
backup.example.com {
    reverse_proxy 127.0.0.1:8484
}
```

Beispiel nginx: `proxy_pass http://127.0.0.1:8484;` mit `proxy_set_header Host $host;` und den üblichen TLS-Einstellungen.
Ist `ports` in der Compose-Datei auf `127.0.0.1` gebunden, erreicht man die Oberfläche nur über den Proxy.

## Aktualisieren

1. In der Oberfläche unter **Jobs** prüfen, dass **kein Auftrag läuft**. Ein Neustart beendet laufende Aufträge
   (sie stehen danach als „Unterbrochen“ im Verlauf). Nicht kurz vor einem Zeitplan neu starten.
2. Neue Quellen holen (`git pull`) und neu bauen: `docker compose up -d --build`.
3. Die eigene `docker-compose.yml` und die `.env` bleiben dabei unverändert – nicht durch die Dateien aus dem Repo ersetzen,
   wenn sie angepasst wurden (Host-Freigaben, Ports, Netzwerk).

Datenbank-Änderungen werden beim Start automatisch migriert. Ein Zurückgehen auf eine ältere Version ist danach nicht
vorgesehen; vorher `DATA_DIR` kopieren, wenn das möglich bleiben soll.

## Umstieg vom CV Docker Helper

Moby Disk ist der Nachfolger des „CV Docker Helper“ und übernimmt dessen Daten:

1. Alten Container **stoppen** (nicht löschen, solange der Umstieg nicht geprüft ist).
2. Neues Projektverzeichnis anlegen, `.env` des alten Helpers kopieren und `TARGETS=commvault` ergänzen.
3. Den alten `data`-Ordner **kopieren** (`cp -a alt/data/. neu/data/`). Dabei wird die Datenbank (`cvstate` → `target_state`)
   und der Zeitplan automatisch übernommen; ein Zeitplan mit Commvault-Schritt zeigt ihn weiter an.
4. Dieselbe Compose-Datei mit **demselben `BACKUP_ROOT`** verwenden. Danach unter **Backups → Mit Zielen abgleichen** ausführen:
   die vorhandenen Commvault-Stände erscheinen wieder.
5. Labels `cvhelper.*` an Containern gelten weiter; neue Labels heißen `mobydisk.*`.

Zwei Helper dürfen **nie gleichzeitig denselben `BACKUP_ROOT`** benutzen.
