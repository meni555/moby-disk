import base64
import time
from urllib.parse import quote

import requests

from .. import applog
from ..config import config


class CVError(Exception):
    pass


def as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def to_cv_path(host_path, windows=False):
    """Pfad im Format des Commvault-Clients (Unix: /opt/x, Windows: \\opt\\x)."""
    if windows:
        p = host_path.replace("/", "\\")
        return p if p.startswith("\\") else "\\" + p
    p = host_path.replace("\\", "/")
    return p if p.startswith("/") else "/" + p


def is_windows(identity):
    return (identity or {}).get("app_type_id") == 33


def browse_url_path(cv_path):
    return quote(cv_path, safe="")


class Commvault:
    def __init__(self, url=None, username=None, password=None, verify_tls=None, timeout=60):
        self.base = url or config.cv_api_base
        self.username = username or config.cv_username
        self.password = password or config.cv_password
        self.verify = config.cv_verify_tls if verify_tls is None else verify_tls
        self.timeout = timeout
        self.session = requests.Session()
        self.session.verify = self.verify
        self._token = None
        self._user_guid = None
        self._token_time = 0.0

    @property
    def configured(self):
        return bool(self.base and self.username and self.password)

    def _headers(self, with_token=True):
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if with_token and self._token:
            headers["Authtoken"] = self._token
        return headers

    def _request(self, method, endpoint, with_token=True, params=None, json_body=None):
        url = self.base.rstrip("/") + "/" + endpoint.lstrip("/")
        log = applog.get("target.commvault")
        started = time.time()
        try:
            resp = self.session.request(
                method,
                url,
                headers=self._headers(with_token),
                params=params,
                json=json_body,
                timeout=self.timeout,
            )
        except requests.exceptions.SSLError as exc:
            log.warning("%s %s: TLS-Fehler: %s", method, endpoint, exc)
            raise CVError(f"TLS-Fehler beim Verbinden mit {url}: {exc}. "
                          "Falls selbstsigniertes Zertifikat: CV_VERIFY_TLS=false setzen.")
        except requests.exceptions.RequestException as exc:
            log.warning("%s %s: Verbindung fehlgeschlagen (%s)", method, endpoint, type(exc).__name__)
            raise CVError(f"Verbindung zu {url} fehlgeschlagen: {exc}")
        log.debug("%s %s → %d (%.0f ms)", method, endpoint, resp.status_code, (time.time() - started) * 1000)
        if resp.status_code >= 400:
            log.warning("%s %s → %d: %s", method, endpoint, resp.status_code, resp.text[:300])
            raise CVError(f"CV-API Fehler {resp.status_code} bei {method} {endpoint}: {resp.text[:1000]}")
        try:
            return resp.json()
        except ValueError:
            raise CVError(f"Keine gültige JSON-Antwort von {method} {endpoint}: {resp.text[:500]}")

    def login(self, force=False):
        if not self.configured:
            raise CVError("Commvault-Zugangsdaten fehlen (CV_URL, CV_USERNAME, CV_PASSWORD).")
        if not force and self._token and (time.time() - self._token_time) < 25 * 60:
            return self._token
        data = self._request(
            "POST", "Login", with_token=False,
            json_body={"username": self.username,
                       "password": base64.b64encode(self.password.encode("utf-8")).decode("ascii")},
        )
        token = data.get("token")
        if not token:
            raise CVError(f"Login fehlgeschlagen: {data}")
        self._token = token
        self._user_guid = data.get("userGUID") or ""
        self._token_time = time.time()
        return token

    def test_connection(self):
        self.login(force=True)
        return {"token_prefix": self._token[:12] + "...", "user_guid": self._user_guid}

    @property
    def user_guid(self):
        self.login()
        return self._user_guid

    def list_clients(self):
        self.login()
        data = self._request("GET", "Client")
        clients = []
        for entry in as_list(data.get("clientProperties")):
            client = entry.get("client") or {}
            entity = client.get("clientEntity") or client or entry
            clients.append({
                "client_id": entity.get("clientId"),
                "client_name": entity.get("clientName") or entity.get("name"),
                "host_name": entity.get("hostName") or entry.get("hostName"),
                "display": entity.get("displayName") or entry.get("displayName"),
            })
        return clients

    def list_subclients(self, client_id):
        self.login()
        data = self._request("GET", "Subclient", params={"clientId": client_id})
        subclients = []
        for entry in as_list(data.get("subClientProperties")):
            entity = entry.get("subClientEntity") or entry
            if isinstance(entity, list):
                entity = entity[0] if entity else {}
            subclients.append({
                "subclient_id": entity.get("subclientId"),
                "subclient_name": entity.get("subclientName"),
                "client_id": entity.get("clientId"),
                "client_name": entity.get("clientName"),
                "app_name": entity.get("appName"),
                "application_id": entity.get("applicationId"),
                "instance_id": entity.get("instanceId"),
                "instance_name": entity.get("instanceName"),
                "backup_set_id": entity.get("backupsetId"),
                "backup_set_name": entity.get("backupsetName"),
            })
        return subclients

    def find_client_id(self, hostname):
        for client in self.list_clients():
            for candidate in (client["client_name"], client["host_name"], client["display"]):
                if candidate and candidate.lower() == hostname.lower():
                    return client
        return None

    def browse(self, subclient_id, host_path):
        self.login()
        cv_path = to_cv_path(host_path)
        data = self._request(
            "GET",
            f"Subclient/{subclient_id}/Browse",
            params={"path": browse_url_path(cv_path)},
        )
        return self._parse_browse_results(data)

    def _parse_browse_results(self, data):
        items = []
        for response in as_list(data.get("browseResponses")):
            result = response.get("browseResult") or response
            for row in as_list(result.get("dataResultSet")):
                advanced = row.get("advancedData") or {}
                flags = row.get("flags") or {}
                items.append({
                    "name": row.get("displayName") or row.get("name"),
                    "path": row.get("path"),
                    "display_path": row.get("displayPath"),
                    "size": row.get("size"),
                    "modification_time": row.get("modificationTime"),
                    "backup_time": advanced.get("backupTime"),
                    "backup_job_id": advanced.get("backupJobId"),
                    "version": row.get("version"),
                    "directory": bool(int(flags.get("directory", 0) or 0)),
                    "deleted": bool(int(flags.get("deleted", 0) or 0)),
                })
        return items

    def browse_versions(self, identity, host_path, op_type=1, show_deleted=False):
        self.login()
        body = {
            "opType": op_type,
            "mode": {"mode": 3},
            "paths": [{"path": to_cv_path(host_path, is_windows(identity))}],
            "entity": {
                "clientId": identity["client_id"],
                "applicationId": identity["app_type_id"],
                "subclientId": identity["subclient_id"],
                "backupsetId": identity["backup_set_id"],
                "instanceId": identity["instance_id"],
            },
            "options": {"showDeletedFiles": bool(show_deleted), "restoreIndex": True},
            "timeRange": {"fromTime": 0, "toTime": 0},
            "queries": [
                {
                    "type": 0,
                    "queryId": "dataQuery",
                    "dataParam": {
                        "paging": {"firstNode": 0, "skipNode": 0, "pageSize": 10000},
                        "sortParam": {"ascending": False, "sortBy": [0]},
                    },
                },
            ],
        }
        data = self._request("POST", "DoBrowse", json_body=body)
        return self._parse_browse_results(data)

    def restore(self, identity, host_path, dest_path, to_time=None, wait=False):
        self.login()
        client_id = identity["client_id"]
        entity = {
            "clientId": client_id,
            "applicationId": identity["app_type_id"],
            "backupsetId": identity["backup_set_id"],
            "subclientId": identity["subclient_id"],
            "_type_": 7,
        }
        if identity.get("instance_id"):
            entity["instanceId"] = identity["instance_id"]
        browse_option = {
            "commCellId": 0,
            "noImage": True,
            "useExactIndex": False,
            "listMedia": False,
            "backupset": {"backupsetId": identity["backup_set_id"], "clientId": client_id},
        }
        if to_time:
            browse_option["timeRange"] = {"toTime": int(to_time)}
        body = {
            "taskInfo": {
                "associations": [entity],
                "task": {"initiatedFrom": 1, "taskType": 1, "policyType": 0,
                         "taskFlags": {"disabled": False}},
                "subTasks": [{
                    "subTask": {"subTaskType": 3, "operationType": 1001},
                    "options": {
                        "restoreOptions": {
                            "browseOption": browse_option,
                            "destination": {
                                "inPlace": False,
                                "destClient": {"clientId": client_id,
                                               "clientName": identity.get("client_name") or ""},
                                "destPath": [dest_path],
                            },
                            "fileOption": {"sourceItem": [to_cv_path(host_path, is_windows(identity))]},
                            "commonOptions": {
                                "preserveLevel": 1,
                                "stripLevelType": 1,
                                "unconditionalOverwrite": True,
                                "restoreACLs": False,
                                "detectRegularExpression": True,
                            },
                        },
                        "commonOpts": {"notifyUserOnJobCompletion": False},
                    },
                }],
            }
        }
        data = self._request("POST", "CreateTask", json_body=body)
        job_ids = as_list(data.get("jobIds"))
        if not job_ids:
            raise CVError(f"Restore-Job wurde nicht angenommen: {data}")
        return int(job_ids[0])

    BACKUP_LEVELS = {1: "Full", 2: "Incremental", 3: "Differential", 4: "Synthetic_full"}

    def start_backup(self, subclient_id, backup_level=2):
        """Backup des Subclients starten. Die Stufe muss als Query-Parameter übergeben werden –
        im JSON-Body ignoriert Commvault sie (dann läuft immer ein Incremental)."""
        self.login()
        level = self.BACKUP_LEVELS.get(int(backup_level))
        if not level:
            raise CVError("backup_level muss 1 (Voll), 2 (Inkrementell), 3 (Differentiell) oder 4 (Synthetisch Voll) sein.")
        return self._request("POST", f"Subclient/{subclient_id}/action/backup", params={"backupLevel": level})

    def subclient_content(self, subclient_id):
        """Inhalt des Subclients: (Pfade, sonstige Einträge wie Filter/Ausnahmen – bleiben beim Schreiben erhalten)."""
        self.login()
        data = self._request("GET", f"Subclient/{subclient_id}")
        props = (as_list(data.get("subClientProperties")) or [{}])[0]
        paths, other = [], []
        for entry in as_list(props.get("content")):
            if entry.get("path"):
                paths.append(entry["path"])
            else:
                other.append(entry)
        return paths, other

    def set_subclient_content(self, subclient_id, paths, other=()):
        """Ersetzt den Inhalt des Subclients (contentOperationType 1 = überschreiben)."""
        self.login()
        content = [{"path": p} for p in paths] + list(other)
        data = self._request("POST", f"Subclient/{subclient_id}",
                             json_body={"subClientProperties": {"contentOperationType": 1, "content": content}})
        resp = (as_list(data.get("response")) or [data])[0]
        code = resp.get("errorCode", data.get("errorCode", 0))
        if code not in (0, "0", None):
            raise CVError(f"Commvault hat den Subclient-Inhalt nicht übernommen: {resp.get('errorString') or data}")
        return data

    def last_full_time(self, identity, lookup_days=120):
        """Ende des jüngsten erfolgreichen Full/Synthetic Full des Subclients (epoch) oder None."""
        self.login()
        data = self._request("GET", "Job", params={"clientId": identity["client_id"], "jobFilter": "Backup",
                                                    "completedJobLookupTime": int(lookup_days * 86400)})
        best = None
        for job in as_list(data.get("jobs")):
            s = job.get("jobSummary") or job
            if str((s.get("subclient") or {}).get("subclientId")) != str(identity["subclient_id"]):
                continue
            if "full" not in (s.get("backupLevelName") or "").lower() or not (s.get("status") or "").startswith("Completed"):
                continue
            when = s.get("jobEndTime") or s.get("lastUpdateTime") or s.get("jobStartTime")
            if when and (best is None or when > best):
                best = when
        return best

    def kill_job(self, job_id):
        """Laufenden Job beenden (POST Job/<id>/action/kill)."""
        self.login()
        return self._request("POST", f"Job/{job_id}/action/kill")

    def get_job(self, job_id):
        self.login()
        data = self._request("GET", f"Job/{job_id}")
        jobs = as_list(data.get("jobs"))
        for job in jobs:
            summary = job.get("jobSummary") or job
            if summary.get("jobId") in (job_id, str(job_id)) or len(jobs) == 1:
                return summary
        return None

    def list_active_jobs(self, client_id=None):
        self.login()
        params = {"jobCategory": "Active"}
        if client_id:
            params["clientId"] = client_id
        data = self._request("GET", "Job", params=params)
        jobs = []
        for job in as_list(data.get("jobs")):
            summary = job.get("jobSummary") or job
            jobs.append(summary)
        return jobs


def terminal_job_status(status):
    return status in (
        "Completed", "Completed w/ one or more errors", "Completed w/ one or more warnings",
        "Failed", "Failed to Start", "Killed", "Committed", "Interrupted",
        "Abnormal Terminated Cleanup", "Interrupt Pending",
    )
