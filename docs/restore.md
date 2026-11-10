# Restore

Ein Restore holt eine Sicherung aus der **lokalen Kopie** oder aus einem **Ziel**, prüft sie und spielt sie zurück – ins Original oder als Klon.

## Starten

Container-Zeile (oder **Backups**-Tab → Versionen) → **Versionen**. Der Dialog hat einen Reiter je Ziel und einen für die lokale Kopie, jeweils mit Zeitpunkt, Größe, Modus,
Dumps, Stack und der Angabe, in welchen Zielen die lokale Kopie ebenfalls liegt. **Wiederherstellen** öffnet die Optionen.

| Option | Wirkung |
| --- | --- |
| Original / Als Klon testen | Das Original wird überschrieben, oder es entsteht ein isolierter Klon (siehe unten). |
| Bind-Mounts mit wiederherstellen | Auch Host-Ordner zurückspielen (über einen Hilfs-Container). |
| Container danach neu starten | Start nach dem Zurückspielen. |
| Container neu anlegen, falls er fehlt | Recreate aus `container.json`; das Image kommt aus der Sicherung oder wird exakt per Digest geladen. |
| Datenbank aus dem Dump neu aufbauen | Leert das Datenverzeichnis, lässt die Datenbank frisch initialisieren und spielt den Dump ein. |
| Ganzen Stack zurücksetzen | Alle Container desselben Backup-Laufs gemeinsam (siehe [Compose-Stacks](scope-and-settings.md#compose-stacks)). Bequemer im Reiter **Stacks** (siehe [Bedienung](usage.md#stacks)). |

**Der aktuelle Datenstand wird beim Restore ins Original überschrieben.** Wer unsicher ist, testet den Stand vorher als Klon.

## Ablauf im Detail

1. **Holen:** aus einem Ziel in `BACKUP_ROOT/.staging/restore-<job>-<container>` (je Ziel anders: Commvault-Restore-Job mit Status-Polling, `restic restore`,
   `proxmox-backup-client restore`, Dateien aus `COMMAND_FETCH`); eine lokale Kopie wird direkt verwendet. Bis hierhin lässt sich der Auftrag pausieren und abbrechen,
   das Staging wird dabei aufgeräumt.
2. **Prüfen:** `manifest.json` suchen, SHA256-Prüfsummen aller Archive verifizieren. Danach ist der Auftrag **gesperrt** (nicht mehr unterbrechbar).
3. **Stoppen:** der laufende Container wird gestoppt.
4. **Zurückspielen:** Volumes werden nach `…/volumes/<name>/_data` entpackt (Besitzer bleiben erhalten); Bind-Mount-Ziele schreibt ein kurzlebiger Hilfs-Container,
   weil Moby Disk selbst die Host-Freigaben nur lesend sieht.
5. **Starten bzw. neu anlegen:** wenn gewünscht; bei einem Datenbank-Neuaufbau zuerst initialisieren lassen, dann den Dump einspielen.
6. **Aufräumen:** das Staging wird entfernt.

Bei einem Fehler zwischen Stoppen und Starten startet Moby Disk zuvor laufende Container wieder, damit nichts gestoppt zurückbleibt.

## Aus welchem Ziel?

- **Lokal:** immer möglich, solange der Sicherungsordner noch da ist. Funktioniert auch, wenn ein Ziel nicht erreichbar ist.
- **Commvault, restic, Proxmox Backup Server:** direkt aus dem Ziel; die Sicherung muss lokal nicht mehr vorhanden sein.
- **Veeam:** Dateien in Veeam nach `BACKUP_ROOT` zurückholen, dann lokal wiederherstellen.
- **Befehl/Skript:** nur mit `COMMAND_FETCH`.
- **Direkt über die Backup-Lösung gesicherte Pfade** stecken nicht im Archiv; der Dialog nennt sie. Sie bleiben beim Restore unverändert und werden bei Bedarf in der Lösung selbst zurückgeholt.

## Restore als Klon (Sandbox)

„Als Klon testen“ legt einen neuen Container (Standard `<name>-clone`) mit **eigenen Volumes** aus der Sicherung an. Das Original bleibt unberührt.

- **Abschottung:** keine Ports (außer optional einem Test-Port), isoliert ohne Netzwerk oder auf der Standard-Bridge, keine Neustart-Richtlinie, wird nicht gesichert.
- **Mounts:** gesicherte Bind-Mounts werden zu Volumes, einzelne Dateien per Volume-Subpath eingebunden. Nicht gesicherte Mounts (NAS) lassen sich schreibgeschützt einbinden,
  der Docker-Socket nie.
- **Aktionen in der Liste:** **Übernehmen** spielt denselben Stand ins Original zurück und entfernt danach den Klon; **Verwerfen** löscht Klon und Volumes.

## Notfall: ohne Oberfläche

Eine Sicherung ist ein normaler Ordner: `container.json` zeigt, wie der Container aussah, die `volume-*.tar` lassen sich mit `tar -xf` in ein neues Volume entpacken, ein
Datenbank-Dump liegt als `dump-*.sql` bzw. `dump-mongo.archive` daneben. Die Prüfsummen stehen in `manifest.json`. Aus einem Ziel genügt es, den Ordner mit dessen eigenem Werkzeug
zurückzuholen (z. B. `restic restore`, `proxmox-backup-client restore`).
