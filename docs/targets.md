# Backup-Ziele

Ein **Ziel** ist die Backup-Lösung, an die Moby Disk seine fertigen Sicherungen übergibt und von der es sie zurückholt.
Aktiv sind die Ziele aus der Umgebungsvariable `TARGETS`; mehrere gleichzeitig sind möglich.

Zwei Arten:

- **wird abgeholt (pull):** Moby Disk legt die Archive unter `BACKUP_ROOT` ab, stößt die Lösung an und wartet. Die Lösung sichert
  den Ordner selbst (Commvault, Veeam).
- **schreibt selbst (push):** Moby Disk überträgt jede Sicherung direkt ins Ziel (Proxmox Backup Server, restic).
  Der Befehl-Typ kann beides.

## Überblick

| | Commvault | Veeam | Proxmox Backup Server | restic | Befehl/Skript |
| --- | --- | --- | --- | --- | --- |
| Art | pull | pull | push | push | beides |
| Lauf anstoßen und überwachen | ✓ | ✓ | ✓ | ✓ | ✓ |
| Stände auflisten | ✓ | – ¹ | ✓ | ✓ | wenn `COMMAND_LIST` |
| Restore aus dem Ziel | ✓ | – ² | ✓ | ✓ | wenn `COMMAND_FETCH` |
| Aufbewahrung im Ziel steuerbar | Storage Policy | Veeam-Job | `PBS_KEEP` | `RESTIC_KEEP` | eigene Skripte |
| Direkt gesicherte Pfade | ✓ | – | – | – | – |
| Abbruch erreicht das Ziel | ✓ | ✓ | ✓ | ✓ | ✓ |
| Geprüft | produktiv | Nachbau des Servers | echter PBS (Test-Image) | echtes Repository | Skript-Tests |

¹ Die Veeam-REST-API liefert keine Ordnerliste, siehe unten. ² Dateien in Veeam zurückholen, dann als lokale Sicherung nutzen.

## Mehrere Ziele und Aufbewahrung

- Im Backup-Dialog und im Zeitplan werden die Ziele angekreuzt. Nach dem lokalen Backup übergibt Moby Disk nacheinander an
  jedes Ziel. Meldet ein Ziel keinen Erfolg, schlägt der Auftrag fehl – die lokale Sicherung bleibt.
- Die **lokale Aufbewahrung** („letzte *N* Sicherungen“, „höchstens *X* Tage“) löscht nur Ordner, die in **allen gewählten
  Zielen** bestätigt sind (Option „Lokale Kopien nur löschen, wenn sie in allen gewählten Zielen bestätigt sind“, in
  neuen Zeitplänen vorangekreuzt). Ist ein Ziel nicht erreichbar, wird nichts gelöscht. Die neueste Sicherung bleibt immer.
- Moby Disk merkt sich je Ziel, welche Sicherungen dort vorliegen. Im Tab **Backups** erscheinen lokal gelöschte, aber im Ziel
  vorhandene Sicherungen als „nur in …“; **Mit Zielen abgleichen** liest die Stände neu ein und entfernt Vermerke, die das Ziel
  nicht mehr hat (z. B. nach abgelaufener Aufbewahrung). Das passiert auch nach jedem Lauf automatisch.
- In **Einstellungen → Backup-Ziele** stehen alle bekannten Ziele, auch nicht aktivierte. **Rot:** nicht eingerichtet.
  **Gelb:** das Ziel ist eingerichtet, aber nicht aktiv (Schalter „Dieses Ziel verwenden“ bzw. `TARGETS`). **Grün:** aktiv und bereit.

## Commvault

Moby Disk legt die Archive unter `BACKUP_ROOT` ab, ein Commvault-Subclient sichert diesen Ordner. Moby Disk startet den Lauf per
REST-API, wartet bis zum Ende und liest die Stände aus Commvaults Browse wieder ein.

```
TARGETS=commvault
CV_URL=https://commserve.example.com
CV_USERNAME=…
CV_PASSWORD=…
# CV_WEBSERVICE_PATH=/SearchSvc/CVWebService.svc
# CV_VERIFY_TLS=false
```

Einrichtung in der Oberfläche: **Einstellungen → Commvault**. „Verbindung testen“, „Diesen Host erkennen“ (oder Client und Subclient
wählen) und „Auswahl speichern“. Der gewählte Subclient muss den Backup-Ordner im Inhalt haben.

- **Backup-Stufe:** im Dialog „Inkrementell“ oder „Voll“. Moby Disk übergibt die Stufe als Parameter der REST-API.
- **Synthetic Full:** Ohne regelmäßige Fulls schließt Commvault keinen Zyklus ab, und die Aufbewahrung greift nie. Nach einem Lauf startet
  Moby Disk deshalb einen Synthetic Full, wenn der letzte Full älter als *n* Tage ist (Standard 7, einstellbar unter
  Commvault → „Direkt über Commvault gesicherte Pfade“, 0 = aus).
- **Direkt über Commvault gesicherte Pfade:** Große, sich wenig ändernde Daten (Fotos, Modelle) sichert Commvault besser selbst,
  dateiweise mit echten Inkrementals. Pfade lassen sich unter Commvault → „Direkt über Commvault gesicherte Pfade“ eintragen oder in
  der aufgeklappten Container-Zeile per **Über CV sichern** am Mount. Moby Disk lässt sie in den Archiven weg und trägt sie zusammen mit
  `BACKUP_ROOT` in den Subclient ein; andere Einträge und Filter des Subclients bleiben unangetastet. **Zurück zum Helper** macht es rückgängig.
  Der Restore weist darauf hin, dass diese Daten nicht im Archiv stecken. Netzwerkfreigaben (CIFS/NFS) gehören nicht hierher.
- **Abbrechen** beendet den Commvault-Job (`Job/<id>/action/kill`).
- **Rechte:** Der API-Benutzer braucht Leserechte auf Client, Subclient und Browse sowie das Recht, Backups und Restores zu starten.
  Storage Policies kann Moby Disk nicht ändern – die Aufbewahrung stellst du in Commvault ein.

## Veeam Backup & Replication

Ein Job in Veeam Backup & Replication (z. B. ein Agent-Job für Linux-Dateien) sichert `BACKUP_ROOT`. Moby Disk startet ihn über die REST-API
(Standard-Port 9419), wartet auf das Ende der Sitzung und vermerkt die Sicherung als bestätigt.

```
TARGETS=veeam
VEEAM_URL=https://vbr.example.com:9419
VEEAM_USERNAME=svc-mobydisk
VEEAM_PASSWORD=…
VEEAM_JOB=Docker-Backups            # Name oder ID
# VEEAM_VERIFY_TLS=false            # Standard false (selbstsigniertes Zertifikat)
# VEEAM_API_VERSION=               # leer = automatisch die höchste Version des Servers (Datei-/Agent-Jobs gibt es erst ab 1.3)
# VEEAM_POLL_SECONDS=10
```

- **Stufe:** „Inkrementell“ startet den Job normal, „Voll“ als aktiven Full.
- **Keine Ordnerliste:** Die REST-API verrät nicht, welche Container-Ordner in einem Restore-Punkt stecken. Ein Ordner gilt als gesichert,
  wenn danach ein erfolgreicher Lauf stattfand; der Vermerk verfällt, wenn kein Restore-Punkt mehr aus der Zeit zwischen Entstehung und lokalem
  Löschen des Ordners existiert.
- **Restore:** In Veeam die Dateien in den Backup-Ordner zurückholen (ursprünglicher Ort). Danach erscheint die Sicherung als lokal und
  lässt sich wie jede andere wiederherstellen.
- **Abbrechen** hält die Veeam-Sitzung an (`sessions/<id>/stop`).
- **Stand der Prüfung:** gegen einen nachgebildeten Server getestet. Stimmen die Felder der echten API nicht, zeigen Verbindungstest und Protokoll
  den Rohfehler – bitte melden.

## Proxmox Backup Server

Jede Sicherung wird ein **Snapshot** der Gruppe `host/<container>` in einem Datastore. Moby Disk ruft den offiziellen `proxmox-backup-client` auf
(im Image, aus dem Proxmox-Repository `pbs-client`; der Signaturschlüssel wird beim Bau gegen eine feste Prüfsumme geprüft).

```
TARGETS=pbs
PBS_REPOSITORY=backup@pbs!moby@pbs.example.com:8007:docker    # Benutzer@Realm!Token@Server:Port:Datastore
PBS_PASSWORD=<Geheimnis des API-Tokens>
PBS_FINGERPRINT=ff:9a:…               # bei selbstsigniertem Zertifikat nötig (PBS: Dashboard → Zertifikate)
# PBS_NAMESPACE=
# PBS_KEYFILE=/data/pbs.key           # clientseitige Verschlüsselung (+ PBS_ENCRYPTION_PASSWORD, falls der Schlüssel eine Passphrase hat)
# PBS_KEEP="--keep-last 14 --keep-monthly 6"
```

- **Token-Rechte:** auf dem Datastore `DatastoreBackup` und `DatastoreReader`, für `PBS_KEEP` zusätzlich `DatastorePrune`.
- **Snapshots:** Zeitpunkt = Zeitpunkt des Sicherungsordners; der Ordnername steckt im Archivnamen (`<ordner>.pxar`).
  Der Server dedupliziert mit inhaltsbasierten Blöcken, tägliche tar-Archive kosten daher nur ihre Änderungen.
- **Aufbewahrung:** `PBS_KEEP` löscht je Container alte Snapshots (`prune`). Speicher gibt der Server über seine Garbage Collection frei – dort einplanen.
- **Verschlüsselung:** Schlüssel anlegen mit `proxmox-backup-client key create /data/pbs.key` und **sicher aufbewahren** – ohne ihn sind die
  Snapshots nicht lesbar.
- **Cache:** Der Client legt Index-Dateien mit `O_TMPFILE` an; Moby Disk legt Cache und Temp unter `DATA_DIR` ab (ext4/xfs/tmpfs, nicht NFS).
- **Architektur:** Das Proxmox-Paket gibt es nicht für jede Architektur; fehlt es, überspringt der Bau den Client und das Ziel meldet „nicht installiert“.

## restic

Jede Sicherung wird ein verschlüsselter, deduplizierter **Snapshot** in einem restic-Repository (lokal, SFTP, S3/MinIO, Backblaze B2, rest-server …).
Das Programm steckt im Image; extern ist nur das Repository.

```
TARGETS=restic
RESTIC_REPOSITORY=s3:https://minio.example.com/backups
RESTIC_PASSWORD=…                     # oder RESTIC_PASSWORD_FILE / RESTIC_PASSWORD_COMMAND
AWS_ACCESS_KEY_ID=…                   # Zugangsdaten des Speichers, wie restic sie kennt
AWS_SECRET_ACCESS_KEY=…
# RESTIC_AUTO_INIT=true               # fehlendes Repository beim ersten Lauf anlegen
# RESTIC_KEEP="--keep-daily 14 --keep-monthly 6"
```

- **Snapshots:** Host = Container-Name, Tag `folder=<Zeitstempel>` (und `moby-disk`). Aufbewahrung `RESTIC_KEEP` per `forget --prune`, je Container gruppiert.
- **Restore:** holt den Snapshot ins Staging. Das Repository regelmäßig mit `restic check` prüfen.
- **Passwort:** sicher aufbewahren – ohne es ist das Repository nicht lesbar.

## Befehl/Skript

Bindet jede andere Lösung (Borg, Kopia, rclone, Bacula, ein eigenes Skript) über Befehle an. Sie laufen **ohne Shell**; Platzhalter `{container}`,
`{folder}`, `{dest}` werden als einzelne Argumente ersetzt, es gibt keine Befehlseinschleusung über Container-Namen. Die Programme müssen im Container liegen
(z. B. `./scripts:/scripts:ro` in der Compose-Datei).

```
TARGETS=command
COMMAND_LABEL=Mein Ziel
COMMAND_START=/scripts/start.sh
# COMMAND_STATUS=/scripts/status.sh
# COMMAND_LIST=/scripts/list.sh {container}
# COMMAND_FETCH=/scripts/fetch.sh {container} {folder} {dest}
# COMMAND_TIMEOUT=21600  COMMAND_POLL_SECONDS=30
```

| Befehl | Aufruf und Erwartung |
| --- | --- |
| `COMMAND_START` | Pflicht. Umgebung: `HELPER_ITEMS` (JSON-Liste mit `container`, `folder`, `path`) und `BACKUP_ROOT`. Exit 0 = Erfolg. |
| `COMMAND_STATUS` | Optional, wird nach dem Start wiederholt aufgerufen: 0 = fertig, 10 = läuft noch, sonst Fehler. Ohne Status gilt der Start als beendet. |
| `COMMAND_LIST` | Optional. Gibt JSON aus: `{"<ordner>": {"time": <epoch>, "size": <bytes>}}`. |
| `COMMAND_FETCH` | Optional. Legt den Stand unter `{dest}/<container>/<ordner>/` ab. |

Welche Fähigkeiten das Ziel hat, ergibt sich daraus, welche Befehle gesetzt sind.
