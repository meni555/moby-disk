"""Datenbank-Dumps: Erkennung, Dump-, Bereitschafts- und Import-Befehle.

Die Befehle laufen per 'docker exec' im Datenbank-Container und lesen die Zugangsdaten aus dessen
eigenen Umgebungsvariablen (MARIADB_ROOT_PASSWORD, POSTGRES_USER, …). Der Helper sieht und speichert
keine Passwörter. Ein Dump macht das Backup auch im Live-Modus konsistent."""
import re

TYPES = ("mariadb", "postgres", "redis", "mongo")
LABELS = {"mariadb": "MariaDB/MySQL", "postgres": "PostgreSQL", "redis": "Redis/Valkey", "mongo": "MongoDB"}

_NAMES = {
    "mariadb": {"mariadb", "mysql", "mysql-server", "percona", "percona-server", "mariadb-server"},
    "postgres": {"postgres", "postgresql", "postgis", "pgvecto-rs", "timescaledb", "timescaledb-ha", "pgvector"},
    "redis": {"redis", "valkey", "redis-stack-server", "keydb", "redis-stack"},
    "mongo": {"mongo", "mongodb", "mongodb-community-server"},
}
# Versionsvariablen, die nur die offiziellen Server-Images (und darauf aufbauende) setzen –
# Werkzeuge wie phpMyAdmin oder Exporter haben sie nicht
_ENV = {
    "mariadb": {"MARIADB_VERSION", "MARIADB_MAJOR", "MYSQL_VERSION", "MYSQL_MAJOR"},
    "postgres": {"PG_MAJOR", "PG_VERSION"},
    "mongo": {"MONGO_VERSION", "MONGO_MAJOR"},
    "redis": {"REDIS_VERSION", "VALKEY_VERSION"},
}

# Zugangsdaten aus der Umgebung des Datenbank-Containers (läuft dort mit sh)
_MY_CREDS = r'''
if [ -n "$MARIADB_ROOT_PASSWORD_FILE" ]; then PW=$(cat "$MARIADB_ROOT_PASSWORD_FILE");
elif [ -n "$MYSQL_ROOT_PASSWORD_FILE" ]; then PW=$(cat "$MYSQL_ROOT_PASSWORD_FILE");
else PW="${MARIADB_ROOT_PASSWORD:-$MYSQL_ROOT_PASSWORD}"; fi
U=root
if [ -z "$PW" ] && [ -n "${MARIADB_USER:-$MYSQL_USER}" ]; then
  U="${MARIADB_USER:-$MYSQL_USER}"; PW="${MARIADB_PASSWORD:-$MYSQL_PASSWORD}"; DB="${MARIADB_DATABASE:-$MYSQL_DATABASE}"; fi
export MYSQL_PWD="$PW"
'''
_PG_CREDS = r'''
U="${POSTGRES_USER:-postgres}"
if [ -n "$POSTGRES_PASSWORD_FILE" ]; then export PGPASSWORD=$(cat "$POSTGRES_PASSWORD_FILE"); else export PGPASSWORD="$POSTGRES_PASSWORD"; fi
'''
_MONGO_CREDS = r'''
AUTH=""
if [ -n "$MONGO_INITDB_ROOT_USERNAME" ]; then
  P="$MONGO_INITDB_ROOT_PASSWORD"; [ -n "$MONGO_INITDB_ROOT_PASSWORD_FILE" ] && P=$(cat "$MONGO_INITDB_ROOT_PASSWORD_FILE")
  AUTH="--username $MONGO_INITDB_ROOT_USERNAME --password $P --authenticationDatabase admin"; fi
'''

DUMP_FILE = {"mariadb": "dump-mariadb.sql", "postgres": "dump-postgres.sql", "mongo": "dump-mongo.archive"}


def _image_name(image):
    ref = (image or "").split("@")[0]
    last = ref.rsplit("/", 1)[-1]
    return last.split(":")[0].lower()


def detect(image, env=None):
    """Datenbanktyp anhand von Image-Name und Umgebungsvariablen (nur Namen) – oder None."""
    name = _image_name(image)
    for kind, names in _NAMES.items():
        if name in names:
            return kind
    keys = {str(e).split("=", 1)[0] for e in (env or [])}
    for kind, markers in _ENV.items():
        if keys & markers:
            return kind
    return None


def dump_command(kind):
    """Befehl, der den Dump nach stdout schreibt. Redis: SAVE (die RDB-Datei landet im Volume)."""
    if kind == "mariadb":
        script = _MY_CREDS + r'''
BIN=$(command -v mariadb-dump || command -v mysqldump) || { echo "mariadb-dump/mysqldump fehlt" >&2; exit 127; }
OPTS="--single-transaction --quick --routines --triggers --hex-blob"
if [ "$U" = root ]; then exec $BIN -uroot --all-databases --events $OPTS; fi
exec $BIN -u"$U" --databases "$DB" $OPTS
'''
    elif kind == "postgres":
        script = _PG_CREDS + 'exec pg_dumpall -U "$U" --clean --if-exists\n'
    elif kind == "mongo":
        script = _MONGO_CREDS + 'exec mongodump --archive --quiet $AUTH\n'
    elif kind == "redis":
        script = r'''
C=$(command -v redis-cli || command -v valkey-cli || command -v keydb-cli) || { echo "redis-cli fehlt" >&2; exit 127; }
[ -n "$REDIS_PASSWORD" ] && export REDISCLI_AUTH="$REDIS_PASSWORD"
OUT=$($C SAVE 2>&1); echo "$OUT" >&2
case "$OUT" in *OK*) exit 0;; *) exit 1;; esac
'''
    else:
        raise ValueError(f"Unbekannter Datenbanktyp: {kind}")
    return ["sh", "-c", script]


def ready_command(kind):
    """Prüft über TCP – der temporäre Server während der Erstinitialisierung lauscht nur am Socket."""
    if kind == "mariadb":
        script = _MY_CREDS + r'''
A=$(command -v mariadb-admin || command -v mysqladmin)
exec $A -u"$U" -h127.0.0.1 --protocol=tcp ping
'''
    elif kind == "postgres":
        script = _PG_CREDS + 'exec pg_isready -h 127.0.0.1 -U "$U"\n'
    elif kind == "mongo":
        script = _MONGO_CREDS + r'''
M=$(command -v mongosh || command -v mongo)
exec $M --quiet $AUTH --eval "db.adminCommand('ping').ok"
'''
    else:
        script = "exit 0"
    return ["sh", "-c", script]


def import_command(kind, path):
    if kind == "mariadb":
        script = _MY_CREDS + f'''
BIN=$(command -v mariadb || command -v mysql)
$BIN -u"$U" < "{path}"
'''
    elif kind == "postgres":
        # search_path wie in der immich-Doku setzen, sonst schlagen Erweiterungen (z. B. Vektoren) fehl
        script = _PG_CREDS + f'''
sed "s/SELECT pg_catalog.set_config('search_path', '', false);/SELECT pg_catalog.set_config('search_path', 'public, pg_catalog', true);/g" "{path}" \\
  | psql -q -v ON_ERROR_STOP=0 -U "$U" -d postgres >/dev/null
'''
    elif kind == "mongo":
        script = _MONGO_CREDS + f'exec mongorestore --archive="{path}" --drop --quiet $AUTH\n'
    else:
        raise ValueError(f"Für {kind} gibt es keinen Import.")
    return ["sh", "-c", script]


def data_dirs(kind, env=None):
    """Mögliche Datenverzeichnisse im Container (für den Neuaufbau aus dem Dump)."""
    values = dict(str(e).split("=", 1) for e in (env or []) if "=" in str(e))
    if kind == "postgres":
        return [values.get("PGDATA") or "/var/lib/postgresql/data", "/var/lib/postgresql"]
    if kind == "mariadb":
        return ["/var/lib/mysql", "/bitnami/mariadb", "/bitnami/mysql", "/config/databases"]
    if kind == "mongo":
        return ["/data/db", "/bitnami/mongodb"]
    return []


def data_mount(kind, mounts, env=None):
    """Der Mount, in dem das Datenverzeichnis liegt (längster passender Zielpfad)."""
    best = None
    for directory in data_dirs(kind, env):
        for m in mounts or []:
            dest = (m.get("destination") or "").rstrip("/")
            if dest and (directory == dest or directory.startswith(dest + "/")):
                if best is None or len(dest) > len(best["destination"].rstrip("/")):
                    best = m
        if best:
            return best
    return None


def count_errors(stderr):
    lines = [l for l in (stderr or "").splitlines() if re.search(r"\bERROR\b|FATAL", l)]
    return len(lines), lines[:5]
