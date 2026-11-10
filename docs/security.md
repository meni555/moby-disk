# Sicherheit

- **Root-Rechte auf dem Host:** Der Zugriff auf `/var/run/docker.sock` entspricht Root auf dem Docker-Host. Wer die Oberfläche bedienen kann, kann Container stoppen, anlegen
  und Daten überschreiben. Die Oberfläche deshalb **nie ungeschützt ins Netz** stellen: `APP_PASSWORD` setzen, Zugriff per Reverse-Proxy mit TLS, am besten nur aus dem internen Netz.
- **Anmeldung:** ein gemeinsames Passwort (`APP_PASSWORD`); die Sitzung läuft über ein signiertes Cookie (`SESSION_SECRET`). Ohne `APP_PASSWORD` ist die Oberfläche offen.
  Es gibt keine Benutzerverwaltung und keine Sperre nach Fehlversuchen – das gehört in den Proxy.
- **Zugangsdaten** liegen in der `.env` und/oder – wenn im Webinterface eingegeben – in `data/target-config.json` (beide Rechte 600, nicht ins Repository, nicht in der Datenbank und damit nicht
  in deren Sicherungen; die Datei ist **nicht verschlüsselt**, der Datenordner gehört dem Host). Die Oberfläche zeigt sie nie an:
  Verbindungsangaben erscheinen als „gesetzt“, URLs mit Passwort werden maskiert. Gespeichert werden in `settings.json` nur die Passwörter der Benachrichtigungskanäle
  (SMTP, ntfy-Token); sie werden nie zurückgeliefert.
- **Protokolle:** Fehlermeldungen der Ziele landen im Auftragsprotokoll. Geheimnisse werden dort maskiert, trotzdem keine Passwörter in Hooks oder Befehle schreiben.
- **Backup-Daten:** Die Sicherungen enthalten alles, was im Container liegt – auch Passwörter und Schlüssel in Volumes und Umgebungsvariablen (`container.json`). `BACKUP_ROOT`
  und die Ziele entsprechend schützen. restic und PBS verschlüsseln auf Wunsch (Schlüssel und Passwörter separat und sicher aufbewahren).
- **Hooks und Befehle:** Pre-/Post-Hooks laufen mit `sh -c` im Container, die Befehle des Ziels „Befehl/Skript“ ohne Shell. Beides setzt Vertrauen in den voraus, der die
  Einstellungen bearbeitet (siehe Root-Rechte).
- **Eingebundene Host-Pfade** bleiben für Moby Disk **nur lesend**; Restores schreiben über einen kurzlebigen Hilfs-Container (Label `mobydisk.task=1`), der nur den Zielordner sieht.
- **Image:** Das Image enthält restic und den Proxmox-Backup-Client. Der Signaturschlüssel des Proxmox-Repositorys wird beim Bau gegen eine feste Prüfsumme geprüft
  (`PROXMOX_KEY_SHA256` im Dockerfile). Ändert Proxmox den Schlüssel, schlägt der Bau fehl und die Prüfsumme muss bewusst aktualisiert werden.
