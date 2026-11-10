# Fehlersuche

Die ersten Anlaufstellen: das **Protokoll** des Auftrags (Jobs → Protokoll aufklappen), **Einstellungen → Backup-Ziele → Verbindung testen** und
`docker logs moby-disk`.

## Allgemein

| Symptom | Ursache und Abhilfe |
| --- | --- |
| Ein Auftrag schlägt ohne erkennbaren Grund fehl | Reiter **Logs**, Filter auf die Auftrags-ID: Fehler stehen mit Typ und Traceback darin. Bei Bedarf die Aufzeichnung auf Debug stellen. |
| Nach jedem Neustart wieder Anmeldung | `SESSION_SECRET` in der `.env` setzen (sonst zufällig pro Start). |
| „Docker nicht erreichbar“ | Docker-Socket nicht eingebunden oder falscher Pfad (`DOCKER_SOCK`). |
| Ein Container wird nicht gesichert („Daten extern“, „Ausgenommen“, „⚠ übersprungen“) | Mounts auf Netzwerkspeicher, Label `mobydisk.backup=false`, Größenlimit oder Host-Pfad nicht eingehängt. Die aufgeklappte Zeile nennt je Mount den Grund. |
| „Host-Pfad ist nicht freigegeben“ | Den übergeordneten Ordner `:ro` und unter demselben Pfad in Moby Disk einhängen. |
| „… wird gerade gesichert oder wiederhergestellt – bitte warten“ | Pro Container läuft nur ein Auftrag; ein anderer hält ihn noch (auch ein pausierter). |
| Backup bricht mit „Speicherplatz“ ab | Weniger als 512 MB frei oder zu wenig Platz für die Größe der letzten Sicherung. Lokale Aufbewahrung senken, Platz schaffen, Tab Kapazität. |
| Der Container steht lange, bevor das Backup beginnt | Er ignoriert SIGTERM (Stop-Timeout 120 s). Pro Container ein kleineres Stop-Timeout setzen oder im Container `init: true`. |
| Auftrag steht als „Unterbrochen“ | Moby Disk wurde während des Auftrags neu gestartet. Vor Updates prüfen, dass nichts läuft. |
| Pause greift nicht sofort | Sie wirkt erst an der nächsten sicheren Stelle (nach dem aktuellen Container). Siehe [Jobs](usage.md#jobs). |
| Der Restore lässt sich nicht mehr abbrechen | Ab dem Zurückschreiben der Daten ist der Auftrag gesperrt. |
| Nach Umzug keine Stände sichtbar | `BACKUP_ROOT` stimmt nicht mit dem Pfad überein, den die Backup-Lösung sichert; danach **Backups → Mit Zielen abgleichen**. |

## Commvault

- **„TLS-Fehler“** → `CV_VERIFY_TLS=false` oder ein gültiges Zertifikat.
- **„Kein Commvault-Client mit Hostname gefunden“** → der Client heißt in Commvault anders als der Hostname; Client und Subclient unter Einstellungen → Commvault wählen.
- **„Keine Versionen in Commvault gefunden“** → enthält der Subclient `BACKUP_ROOT`? Lief bereits ein Backup, und stimmt der Pfad (Container-Ordner unter dem Pfad im Subclient)?
- **Der Job bleibt bei „Waiting“** → Commvault-Konsole prüfen (Medien, Storage Policy, Pending Reasons stehen im Protokoll).
- **Aufbewahrung greift nie** → ohne regelmäßige Fulls schließt Commvault keinen Zyklus ab; der Synthetic Full nach dem Lauf muss aktiviert sein (Standard 7 Tage).
- **Der API-Benutzer darf keine Storage Policies ändern** → das ist so gedacht; Aufbewahrung in Commvault einstellen.

## Veeam

- **„Veeam-Anmeldung fehlgeschlagen“** → Benutzer, Passwort und `VEEAM_URL` (Port 9419, HTTPS) prüfen. `VEEAM_USERNAME` braucht meist die Domäne bzw. den Rechnernamen (`DOMÄNE\Benutzer`, `Benutzer@domäne` oder `RECHNERNAME\Benutzer`). Nach einer abgelehnten Anmeldung probiert Moby Disk 5 Minuten lang nicht erneut, weil Veeam das Konto nach wenigen Fehlversuchen sperrt („Too many attempts“) – nach einer Korrektur der Zugangsdaten gilt die Pause nicht mehr. Moby Disk wählt die API-Version selbst (höchste, die der Server anbietet); nur bei Problemen `VEEAM_API_VERSION` fest setzen (z. B. `1.3-rev1`). **„Job nicht gefunden“ bei einem Datei- oder Agent-Job:** Mit API 1.1 sind nur VM-Jobs sichtbar – die Version muss mindestens 1.3 sein (nicht `VEEAM_API_VERSION=1.1-rev0` setzen).
- **„Veeam-Job … nicht gefunden“** → `VEEAM_JOB` nennt Name oder ID; die Fehlermeldung listet ähnliche Namen.
- **Die Sicherung gilt nicht als bestätigt** → es braucht einen erfolgreichen Lauf nach Entstehung des Ordners; „Warning“ zählt als Erfolg.
- **Rohfehler der API** erscheinen im Protokoll – bitte mit melden, die Anbindung ist noch nicht gegen echtes Veeam geprüft.

## Proxmox Backup Server

- **„certificate validation failed“** → `PBS_FINGERPRINT` setzen (PBS: Dashboard → Zertifikate).
- **„Operation not supported (os error 95)“** beim Restore → `DATA_DIR` liegt auf einem Dateisystem ohne `O_TMPFILE` (Overlay, NFS). Ein Host-Ordner auf ext4/xfs verwenden.
- **„ENOENT“ mit Namespace** → der Namespace in `PBS_NAMESPACE` existiert im Datastore nicht.
- **403/Permission** → dem Token fehlen `DatastoreBackup`/`DatastoreReader` (Prune: `DatastorePrune`).
- **„proxmox-backup-client ist nicht installiert“** → das Image wurde für eine Architektur gebaut, für die Proxmox kein Paket anbietet.
- **Der Speicher wird nicht frei** → die Garbage Collection des PBS-Datastores einplanen.

## restic

- **„Is there a repository at the following location?“** → `RESTIC_REPOSITORY` falsch, oder das Repository ist neu: `RESTIC_AUTO_INIT=true` setzen oder `restic init` ausführen.
- **„wrong password“** → `RESTIC_PASSWORD` passt nicht zum Repository.
- **Repository gesperrt** → ein abgebrochener Lauf hat eine Sperre hinterlassen: `restic unlock`.

## Befehl/Skript

- **„Programm … nicht gefunden“** → das Skript ist im Container nicht eingebunden (`./scripts:/scripts:ro`) oder nicht ausführbar.
- **Das Ziel zeigt keine Stände / keinen Restore** → `COMMAND_LIST` bzw. `COMMAND_FETCH` fehlen; die Fähigkeiten ergeben sich aus den gesetzten Befehlen.
- **Zeitüberschreitung** → `COMMAND_TIMEOUT` (Sekunden je Aufruf) erhöhen.

## Recreate beim Restore

„Image fehlt“ → Backup mit „Images: alle/nur selbst gebaute“ erstellen oder das Image vorher `docker pull`en. Öffentliche Images lädt Moby Disk beim Restore exakt per Digest
aus dem Manifest nach.
