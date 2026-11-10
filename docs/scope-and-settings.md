# Backup-Umfang und Einstellungen pro Container

## Was gesichert wird

Je Container sichert Moby Disk **Volumes**, **Bind-Mounts** (Ordner und einzelne Dateien) und die **Konfiguration** (`container.json`: Image, Umgebung, Ports, Netzwerke,
Restart-Policy, Labels …, genug für ein Neuanlegen). Optional das **Image** und ein **Datenbank-Dump**.

Aufbau einer Sicherung unter `BACKUP_ROOT`:

```
<BACKUP_ROOT>/<container>/<zeitstempel>/
├── container.json        # Konfiguration des Containers (Recreate-fähig)
├── manifest.json         # Metadaten, Größen, SHA256-Prüfsummen, übersprungene Mounts, Dumps, Hooks
└── data/
    ├── volume-<name>.tar
    ├── bindmount-<n>.tar
    ├── dump-mariadb.sql  # bzw. dump-postgres.sql, dump-mongo.archive – falls ein Dump erstellt wurde
    └── image.tar         # falls das Image gesichert wurde
```

Der Zeitstempel (UTC, z. B. `2026-10-29T20-17-23-824692Z`) ist zugleich der Name, unter dem Ziele die Sicherung kennen.

## Was *nicht* gesichert wird

Schutz davor, dass z. B. ein Jellyfin-Container die Mediensammlung vom NAS mitpackt, in mehreren Schichten:

1. **Netzwerkspeicher** (CIFS/SMB, NFS, FUSE und Volumes mit Netzwerk-Treiber) wird übersprungen; abschaltbar.
2. **Muster** für Volume-Namen und Zielpfade (global, Einstellungen → Backup-Umfang) sowie **Regeln pro Container** (aufgeklappte Zeile: „Ausschließen“ / „Trotzdem sichern“).
3. **Labels** am Container (siehe unten).
4. **Größenlimit pro Mount** (Standard 100 GB, höchstens 80 % des freien Platzes): wird beim Packen erzwungen; ein zu großes Archiv wird abgebrochen, gelöscht und als „übersprungen“ vermerkt.
5. **Datei-/Ordnermuster** (`cache`, `*.tmp`, `logs/**`) innerhalb der Volumes, global oder je Container.
6. **Direkt von der Backup-Lösung gesicherte Pfade** (siehe [Commvault](targets.md#commvault)).

Immer übersprungen: Systempfade des Hosts (`/etc`, `/run`, Docker-Socket …), der Backup-Ordner selbst, Ordner, die alle Docker-Volumes oder eine ganze Host-Freigabe
von Moby Disk enthalten. Die Liste zeigt „⚠ n übersprungen“, der Backup-Dialog nennt vorab, was nicht gesichert wird, und jedes Manifest vermerkt übersprungene Mounts.
Container, deren Daten komplett extern liegen, erscheinen als „Daten extern“ statt „ungeschützt“.

## Labels

Labels am Container steuern das Verhalten; die Oberfläche hat bei gleichen Einstellungen Vorrang. Das Präfix ist `mobydisk.`; **ältere `cvhelper.*`-Labels gelten weiter**
(`mobydisk.*` hat Vorrang).

| Label | Wirkung |
| --- | --- |
| `mobydisk.backup=false` | Container nie sichern (auch nicht von „alle mit Daten“) |
| `mobydisk.skip=/pfad,…` oder `all` | Mounts mit diesen Zielpfaden überspringen (`all` = nichts sichern) |
| `mobydisk.include=/pfad` | Mount trotz Netzwerkspeicher/Limit sichern (ohne Größenlimit) |
| `mobydisk.exclude-glob=cache,*.tmp` | Dateien/Ordner in den Volumes auslassen |
| `mobydisk.mode=live\|stop` | Konsistenz erzwingen |
| `mobydisk.stop-timeout=30` | Sekunden bis zum erzwungenen Stopp (Standard 120) |
| `mobydisk.dump=auto\|off\|mariadb\|postgres\|redis\|mongo` | Datenbank-Dump (automatisch, aus oder fester Typ) |
| `mobydisk.image=always\|never` | Image immer/nie sichern |
| `mobydisk.pre`, `mobydisk.post` | Hooks (`sh -c` im Container) |

Intern verwendet Moby Disk außerdem `mobydisk.task`, `mobydisk.clone`, `mobydisk.clone-of`, `mobydisk.clone-source` für Hilfs-Container und Klone; sie tragen beide Präfixe,
damit eine ältere Installation auf demselben Host sie ebenfalls ignoriert.

## Bind-Mounts anderer Container (Host-Freigaben)

Moby Disk sieht Host-Pfade nur, wenn sie bei ihm eingehängt sind. Damit z. B. `/home/<user>/gitea` oder `/opt/stacks/…` gesichert werden, die übergeordneten Ordner
**nur lesend und unter demselben Pfad** einhängen:

```yaml
    volumes:
      - /home/user:/home/user:ro
      - /opt/stacks:/opt/stacks:ro
      - /docker/appdata:/docker/appdata:ro
```

- **Auflösung:** Moby Disk ordnet jeden Host-Pfad über seine eigenen Mounts zu (längster Präfix). Einzelne Dateien (z. B. `acquis.yaml`) werden ebenfalls gesichert.
- **Nicht eingehängt:** Der Mount wird übersprungen („Host-Pfad ist nicht freigegeben“) – Moby Disk hat dann keinen Zugriff.
- **Restore:** Moby Disk schreibt nie selbst in diese Ordner. Es startet einen kurzlebigen Hilfs-Container aus seinem Image, in dem genau der Zielordner beschreibbar ist
  (`python -m app.worker`); Besitzer und Rechte bleiben erhalten (numerische UID/GID).
- **Übersicht:** Einstellungen → Backup-Umfang zeigt die aktiven Freigaben.

## Images

Auswahl pro Backup bzw. Zeitplan: **nur selbst gebaute** (Standard), keine oder alle.

- **Selbst gebaut** heißt: das Image hat keinen Registry-Digest (lokal gebaut) oder stammt aus einer eigenen Registry (IP-Adresse, Host mit Port, `localhost`, `*.local`/`*.lan`
  oder unter Einstellungen → Backup-Umfang → „Eigene Registries“ eingetragen).
- **Öffentliche Images** (Docker Hub, ghcr.io, lscr.io …) werden nicht gesichert. Fehlen sie beim Restore, lädt Moby Disk sie **exakt per Digest** aus dem Manifest nach – dieselbe
  Version, nicht `latest`.
- **Mehrfach genutzte Images:** Ein Image, das mehrere Container nutzen oder das sich seit dem letzten Lauf nicht geändert hat, wird nur einmal exportiert; die übrigen Sicherungen
  erhalten einen Hardlink (kein Zusatzplatz, keine Exportzeit). Der Export läuft **nach** dem Neustart, damit der Container nicht länger steht.
- **Pro Container:** in der aufgeklappten Zeile oder per Label `mobydisk.image=always|never`.

## Einstellungen pro Container

In der aufgeklappten Zeile der Container-Liste (oder per Label, siehe oben):

- **Modus:** immer live oder immer stop, unabhängig vom Auftrag.
- **Stop-Timeout:** spart Zeit bei Containern, die SIGTERM ignorieren (Standard 120 s; ein Hinweis im Protokoll nennt langsame Stopps).
- **Datenbank-Dump:** automatisch, aus oder fest.
- **Image:** wie der Auftrag, immer oder nie.
- **Pre-/Post-Hook:** Befehle für `sh -c` im Container. Der Pre-Hook läuft vor Dump und Stopp, der Post-Hook nach dem Start – auch nach Fehlern. Beispiel:
  `php occ maintenance:mode --on` / `--off`. Schlägt der Pre-Hook fehl, wird der Container nicht gesichert.

### Datenbank-Dumps

MariaDB/MySQL, PostgreSQL und MongoDB werden am Image erkannt, Redis/Valkey ebenfalls. Vor dem Backup schreibt `mariadb-dump --single-transaction`, `pg_dumpall`, `mongodump`
bzw. `SAVE` (Redis) einen konsistenten Dump in die Sicherung.

- Die Zugangsdaten stammen aus den **Umgebungsvariablen des Datenbank-Containers** (`MARIADB_ROOT_PASSWORD`, `POSTGRES_USER` …); Moby Disk speichert keine Passwörter.
- Schlägt ein Dump fehl, liegt die Sicherung trotzdem vor, der Auftrag meldet aber einen Fehler.
- **Neuaufbau beim Restore:** „Datenbank aus dem Dump neu aufbauen“ leert das Datenverzeichnis, lässt den Container es frisch initialisieren und spielt den Dump ein. Für Sicherungen im Live-Modus empfohlen.

## Compose-Stacks

Container mit demselben Label `com.docker.compose.project` bilden einen Stack. Ein Klick auf den Stack-Namen wählt den ganzen Stack aus.

- **Backup:** Stack-Mitglieder werden gemeinsam gestoppt (Option „gemeinsam stoppen“, Standard), damit App und Datenbank denselben Stand haben. Gestoppt wird in umgekehrter,
  gestartet in Abhängigkeitsreihenfolge (`depends_on`, sonst Datenbanken zuerst). „Ganze Compose-Stacks sichern“ ergänzt fehlende Mitglieder.
- **Restore:** „Ganzen Stack auf diesen Lauf zurücksetzen“ holt alle Sicherungen desselben Laufs, stoppt alle Container, spielt zurück und startet in Reihenfolge – mit Dump-Import vor dem Start der Apps.

## Moby Disk sichert sich selbst

Der Moby-Disk-Container erscheint in der Liste (Markierung „Moby Disk“) und kann gesichert werden – aber **immer live**, sonst würde er sich selbst stoppen. Gesichert werden nur
seine eigenen Daten (`/data`; die Datenbank als konsistenter Snapshot); `BACKUP_ROOT`, `/var/lib/docker/volumes` und seine Host-Freigaben werden übersprungen. Ein Restore von
Moby Disk über Moby Disk selbst ist gesperrt.
