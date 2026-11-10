"""Kurzlebiger Hilfsprozess für Schreibzugriffe auf Host-Pfade.

Der Helper sieht fremde Bind-Mounts (z. B. /home/user, /opt/stacks) nur lesend. Für einen Restore
startet er einen eigenen Container aus seinem Image, in dem genau das Zielverzeichnis beschreibbar
eingehängt ist, und ruft darin dieses Modul auf:

    python -m app.worker extract <archiv> <ziel> [--clear]
    python -m app.worker clear <ziel>

Nur Standardbibliothek – kein Zugriff auf Docker, Einstellungen oder Commvault."""
import os
import shutil
import sys
import tarfile


def _safe(member):
    parts = member.name.replace("\\", "/").split("/")
    return not member.name.startswith("/") and ".." not in parts


def clear_dir(target):
    """Leert ein Verzeichnis, ohne es selbst zu löschen (es ist der Mount-Punkt)."""
    removed = 0
    if not os.path.isdir(target):
        return removed
    for entry in os.scandir(target):
        if entry.is_dir(follow_symlinks=False):
            shutil.rmtree(entry.path)
        else:
            os.unlink(entry.path)
        removed += 1
    return removed


def extract(tar_path, target, clear=False):
    os.makedirs(target, exist_ok=True)
    if clear:
        clear_dir(target)
    count = 0
    with tarfile.open(tar_path, "r:*") as tar:
        for member in tar:
            if not _safe(member):
                print(f"übersprungen (unsicherer Pfad): {member.name}", file=sys.stderr)
                continue
            tar.extract(member, target, numeric_owner=True, filter="fully_trusted")
            count += 1
    return count


def main(argv):
    if len(argv) >= 3 and argv[0] == "extract":
        print(extract(argv[1], argv[2], clear="--clear" in argv[3:]))
        return 0
    if len(argv) == 2 and argv[0] == "clear":
        print(clear_dir(argv[1]))
        return 0
    print("Aufruf: extract <archiv> <ziel> [--clear] | clear <ziel>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
