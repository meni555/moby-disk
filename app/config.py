import json
import os
import threading

DATA_DIR = os.environ.get("DATA_DIR", "/data")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")

_lock = threading.Lock()

# Leere Platzhalter aus der .env (z. B. „VEEAM_JOB=“) gelten als nicht gesetzt – sonst würden Programme wie restic oder
# proxmox-backup-client die leeren Werte als echte Angabe lesen (z. B. einen leeren Fingerabdruck).
for _name in [n for n, v in os.environ.items() if n.startswith(("VEEAM_", "PBS_", "RESTIC_", "AWS_", "COMMAND_")) and not v.strip()]:
    del os.environ[_name]


def _env(name, default=""):
    value = os.environ.get(name, "").strip()
    return value if value else default


class Settings:
    def __init__(self):
        self._data = {}
        self.load()

    def load(self):
        with _lock:
            try:
                with open(SETTINGS_FILE, "r", encoding="utf-8") as fh:
                    self._data = json.load(fh)
            except (OSError, ValueError):
                self._data = {}

    def save(self):
        with _lock:
            os.makedirs(DATA_DIR, exist_ok=True)
            tmp = SETTINGS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2)
            os.replace(tmp, SETTINGS_FILE)

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value
        self.save()

    def update(self, mapping):
        self._data.update(mapping)
        self.save()

    def as_dict(self):
        return dict(self._data)


settings = Settings()


def env_bool(name, default=False):
    value = _env(name)
    if value == "":
        return default
    return value.lower() in ("1", "true", "yes", "on")


class Config:
    @property
    def app_password(self):
        return _env("APP_PASSWORD")

    @property
    def session_secret(self):
        return _env("SESSION_SECRET") or os.urandom(32).hex()

    @property
    def docker_sock(self):
        return _env("DOCKER_SOCK", "/var/run/docker.sock")

    @property
    def volumes_root(self):
        return _env("VOLUMES_ROOT", "/var/lib/docker/volumes")

    @property
    def backup_root(self):
        return _env("BACKUP_ROOT", "/opt/docker-backups")

    @property
    def staging_root(self):
        return os.path.join(self.backup_root, ".staging")

    @property
    def cv_url(self):
        return _env("CV_URL")

    @property
    def cv_webservice_path(self):
        return _env("CV_WEBSERVICE_PATH", "/SearchSvc/CVWebService.svc")

    @property
    def cv_username(self):
        return _env("CV_USERNAME")

    @property
    def cv_password(self):
        return _env("CV_PASSWORD")

    @property
    def cv_verify_tls(self):
        return env_bool("CV_VERIFY_TLS", False)

    @property
    def cv_api_base(self):
        url = self.cv_url.rstrip("/")
        path = self.cv_webservice_path.strip()
        if not path.startswith("/"):
            path = "/" + path
        return url + path

    def effective_cv_identity(self):
        return {
            "client_id": settings.get("cv_client_id") or int(_env("CV_CLIENT_ID", "0")) or None,
            "client_name": settings.get("cv_client_name") or _env("CV_CLIENT_NAME"),
            "subclient_id": settings.get("cv_subclient_id") or int(_env("CV_SUBCLIENT_ID", "0")) or None,
            "subclient_name": settings.get("cv_subclient_name") or _env("CV_SUBCLIENT_NAME"),
            "app_type_id": settings.get("cv_app_type_id") or int(_env("CV_APP_TYPE_ID", "0")) or None,
            "instance_id": settings.get("cv_instance_id") or int(_env("CV_INSTANCE_ID", "0")) or None,
            "instance_name": settings.get("cv_instance_name") or _env("CV_INSTANCE_NAME"),
            "backup_set_id": settings.get("cv_backup_set_id") or int(_env("CV_BACKUP_SET_ID", "0")) or None,
            "backup_set_name": settings.get("cv_backup_set_name") or _env("CV_BACKUP_SET_NAME"),
        }


config = Config()
