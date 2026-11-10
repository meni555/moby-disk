import ipaddress
import os
import re
import socket
import tarfile
import threading
import time

import docker
from docker.errors import DockerException, NotFound

from . import applog
from .config import config


class DockerError(Exception):
    pass


_client = None


def client():
    global _client
    if _client is None:
        try:
            _client = docker.DockerClient(base_url="unix://" + config.docker_sock, timeout=30, version="auto")
        except DockerException as exc:
            raise DockerError(f"Docker-Socket nicht erreichbar ({config.docker_sock}): {exc}")
    return _client


_long_client = None


def long_client():
    """Client ohne kurzen Read-Timeout für große Streams (Image-Export, der Daemon liefert erst nach Sekunden Daten)."""
    global _long_client
    if _long_client is None:
        try:
            _long_client = docker.DockerClient(base_url="unix://" + config.docker_sock, timeout=3600, version="auto")
        except DockerException as exc:
            raise DockerError(f"Docker-Socket nicht erreichbar ({config.docker_sock}): {exc}")
    return _long_client


def hostname():
    return socket.gethostname()


_own_id = None


def own_container_id():
    """Volle ID des Containers, in dem der Helper selbst läuft (None, wenn nicht ermittelbar)."""
    global _own_id
    if _own_id is not None:
        return _own_id or None
    found = ""
    try:
        with open("/proc/self/mountinfo", "r", encoding="utf-8") as fh:
            match = re.search(r"/docker/containers/([0-9a-f]{64})", fh.read())
        if match:
            found = match.group(1)
    except OSError:
        pass
    if not found:
        try:
            found = client().containers.get(socket.gethostname()).id
        except Exception:
            found = ""
    _own_id = found
    return found or None


def is_self(name):
    """True, wenn 'name' der Container ist, in dem dieser Helper läuft."""
    own = own_container_id()
    if not own:
        return False
    try:
        return get_container(name).id == own
    except DockerError:
        return False


# ───────── Host-Pfade ↔ Pfade im Helper ─────────
_own_mounts = {"at": 0.0, "mounts": None}


def own_mounts():
    """Mounts des Helper-Containers als [(host_quelle, ziel_im_helper, beschreibbar)].
    None, wenn der Helper nicht in Docker läuft (Entwicklung) – dann gelten Host-Pfade direkt."""
    now = time.time()
    if _own_mounts["mounts"] is not None and now - _own_mounts["at"] < 60:
        return _own_mounts["mounts"] or None
    own = own_container_id()
    mounts = []
    if own:
        try:
            for m in client().api.inspect_container(own).get("Mounts") or []:
                if m.get("Source") and m.get("Destination"):
                    mounts.append((m["Source"].rstrip("/") or "/", m["Destination"].rstrip("/") or "/", bool(m.get("RW"))))
        except Exception:
            mounts = []
    _own_mounts.update(at=now, mounts=mounts)
    return mounts or None


def _map(path, pairs):
    best = None
    for src, dst in pairs:
        if path == src or path.startswith(src.rstrip("/") + "/"):
            if best is None or len(src) > len(best[0]):
                best = (src, dst)
    if best is None:
        return None
    rest = path[len(best[0]):]
    return (best[1].rstrip("/") + rest) if rest else best[1]


def to_local(host_path):
    """Wo liegt ein Host-Pfad im Helper? None, wenn er dort nicht eingehängt ist."""
    if not host_path:
        return None
    mounts = own_mounts()
    if mounts is None:
        return host_path
    return _map(os.path.normpath(host_path), [(s, d) for s, d, _rw in mounts])


def to_host(local_path):
    """Umkehrung von to_local: Host-Pfad zu einem Pfad im Helper (z. B. für den Hilfs-Container)."""
    mounts = own_mounts()
    if mounts is None:
        return local_path
    return _map(os.path.normpath(local_path), [(d, s) for s, d, _rw in mounts])


def host_share_paths():
    """Lesende 1:1-Freigaben von Host-Pfaden (Quelle = Ziel) – gehören nicht zu den Helper-Daten."""
    return [s for s, d, rw in (own_mounts() or []) if s == d and not rw]


# ───────── Image-Herkunft ─────────
_origin_cache = {}


def registry_of(ref):
    first = (ref or "").split("/")[0]
    if "/" in (ref or "") and ("." in first or ":" in first or first == "localhost"):
        return first.lower()
    return "docker.io"


def is_private_registry(host, extra=()):
    host = (host or "").lower()
    if host in {e.strip().lower() for e in extra if e.strip()}:
        return True
    if host.startswith("localhost") or ":" in host:      # mit Port: typisch für eigene Registries
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    return host.endswith((".local", ".lan", ".home", ".internal", ".intern", ".home.arpa", ".fritz.box"))


def image_origin(image_ref, extra=()):
    """Woher stammt ein Image? origin: local (selbst gebaut, nie aus einer Registry) | private
    (eigene Registry, z. B. Gitea) | public (Docker Hub, ghcr.io …) | unknown."""
    key = (image_ref, tuple(extra))
    cached = _origin_cache.get(key)
    if cached and time.time() - cached[0] < 300:
        return cached[1]
    try:
        img = client().images.get(image_ref)
    except Exception:
        result = {"origin": "unknown", "text": "Image lokal nicht vorhanden", "size": None, "id": None}
    else:
        digests = img.attrs.get("RepoDigests") or []
        size = img.attrs.get("Size")
        regs = sorted({registry_of(d.split("@")[0]) for d in digests})
        own = [r for r in regs if is_private_registry(r, extra)]
        if not digests:
            result = {"origin": "local", "text": "selbst gebaut (lokal)", "size": size, "id": img.id}
        elif own:
            result = {"origin": "private", "text": f"selbst gebaut (Registry {own[0]})", "size": size, "id": img.id}
        else:
            result = {"origin": "public", "text": f"öffentlich ({regs[0]})", "size": size, "id": img.id,
                      "digest": digests[0]}
    _origin_cache[key] = (time.time(), result)
    return result


# ───────── Container lesen ─────────
LABEL_PREFIX = "mobydisk."
LEGACY_PREFIXES = ("cvhelper.",)   # Container aus der Zeit vor Moby Disk tragen noch dieses Präfix


def own_label(labels, key, default=None):
    """Wert eines Moby-Disk-Labels (z. B. own_label(labels, "backup")); ältere cvhelper.*-Labels gelten weiter."""
    labels = labels or {}
    for prefix in (LABEL_PREFIX, *LEGACY_PREFIXES):
        if prefix + key in labels:
            return labels[prefix + key]
    return default


def own_labels(values):
    """Labels für Container und Volumes, die Moby Disk selbst anlegt. Zusätzlich mit dem alten Präfix, damit ein
    älterer Helper auf demselben Host sie ebenfalls erkennt (und z. B. Hilfs-Container nicht sichert)."""
    out = {}
    for key, value in values.items():
        for prefix in (LABEL_PREFIX, *LEGACY_PREFIXES):
            out[prefix + key] = value
    return out


def list_containers():
    containers = []
    for c in client().containers.list(all=True):
        if own_label((c.attrs.get("Config") or {}).get("Labels"), "task") == "1":
            continue   # kurzlebige Hilfs-Container des Helpers
        containers.append(container_summary(c))
    containers.sort(key=lambda x: x["name"])
    return containers


def backup_candidate(summary):
    """Gehört der Container in 'Alle mit Daten'? Nicht: Klone und Container mit mobydisk.backup=false."""
    if summary.get("clone_of"):
        return False
    if str(own_label(summary.get("labels"), "backup", "")).lower() in ("false", "off", "0", "no"):
        return False
    return bool(summary.get("has_data"))


def _volume_info_from_mount(mount):
    if mount.get("Type") == "volume":
        return {
            "type": "volume",
            "name": mount.get("Name"),
            "source": mount.get("Source"),
            "destination": mount.get("Destination"),
            "readonly": bool(mount.get("RW") is False),
        }
    if mount.get("Type") == "bind":
        return {
            "type": "bind",
            "source": mount.get("Source"),
            "destination": mount.get("Destination"),
            "readonly": bool(mount.get("RW") is False),
        }
    return {
        "type": mount.get("Type"),
        "source": mount.get("Source"),
        "destination": mount.get("Destination"),
        "readonly": bool(mount.get("RW") is False),
    }


def parse_depends_on(value):
    """Compose-Label 'db:service_started:false,redis:…' → ['db', 'redis']."""
    return [part.split(":")[0].strip() for part in (value or "").split(",") if part.split(":")[0].strip()]


def compose_info(labels):
    labels = labels or {}
    return {
        "stack": labels.get("com.docker.compose.project") or None,
        "service": labels.get("com.docker.compose.service") or None,
        "depends_on": parse_depends_on(labels.get("com.docker.compose.depends_on")),
        "working_dir": labels.get("com.docker.compose.project.working_dir") or None,
    }


def container_summary(c):
    attrs = c.attrs
    config_blob = attrs.get("Config") or {}
    state_blob = attrs.get("State") or {}
    image_ref = config_blob.get("Image") or attrs.get("Image") or ""
    mounts = [_volume_info_from_mount(m) for m in (attrs.get("Mounts") or [])]
    all_labels = config_blob.get("Labels") or {}
    labels = {k: v for k, v in all_labels.items() if k.startswith((LABEL_PREFIX, *LEGACY_PREFIXES))}
    return {
        "id": c.id,
        "short_id": c.short_id,
        "name": c.name.lstrip("/"),
        "image": image_ref,
        "image_id": attrs.get("Image") or "",
        "state": c.status,
        "running": bool(state_blob.get("Running")),
        "health": (state_blob.get("Health") or {}).get("Status"),
        "created": attrs.get("Created"),
        "restart_policy": (attrs.get("HostConfig") or {}).get("RestartPolicy") or {},
        "mounts": mounts,
        "has_data": any(m["type"] in ("volume", "bind") for m in mounts),
        "labels": labels,
        "env_keys": [str(e).split("=", 1)[0] for e in config_blob.get("Env") or []],
        "is_self": bool(own_container_id()) and c.id == own_container_id(),
        "clone_of": own_label(labels, "clone-of") or None,
        **compose_info(all_labels),
    }


def get_container(name):
    for c in client().containers.list(all=True):
        if c.name.lstrip("/") == name:
            return c
    raise DockerError(f"Container '{name}' nicht gefunden.")


def inspect_snapshot(name):
    c = get_container(name)
    attrs = c.attrs
    blob = attrs.get("Config") or {}
    host_config = attrs.get("HostConfig") or {}
    network_settings = attrs.get("NetworkSettings") or {}
    networks = {}
    for net_name, net_conf in (network_settings.get("Networks") or {}).items():
        networks[net_name] = {
            "aliases": list((net_conf or {}).get("Aliases") or []),
        }
    return {
        "name": name,
        "image": blob.get("Image") or "",
        "image_id": attrs.get("Image") or "",
        "image_repo_digests": _repo_digests(attrs.get("Image")),
        "command": blob.get("Cmd"),
        "entrypoint": blob.get("Entrypoint"),
        "env": blob.get("Env") or [],
        "labels": blob.get("Labels") or {},
        "mounts": [_volume_info_from_mount(m) for m in (attrs.get("Mounts") or [])],
        "ports": _ports_from_container(c),
        "exposed_ports": sorted((blob.get("ExposedPorts") or {}).keys()),
        "restart_policy": host_config.get("RestartPolicy") or {},
        "network_mode": host_config.get("NetworkMode") or "default",
        "networks": networks,
        "user": blob.get("User") or "",
        "working_dir": blob.get("WorkingDir") or "",
        "hostname": blob.get("Hostname") or "",
        "stop_signal": blob.get("StopSignal") or "",
        "stop_timeout": blob.get("StopTimeout") or host_config.get("StopTimeout") or 10,
        "tty": bool(blob.get("Tty")),
        "stdin_open": bool(blob.get("OpenStdin")),
        "healthcheck": blob.get("Healthcheck"),
        "runtime": host_config.get("Runtime") or "",
        "cap_add": host_config.get("CapAdd") or [],
        "cap_drop": host_config.get("CapDrop") or [],
        "privileged": bool(host_config.get("Privileged")),
        "security_opt": host_config.get("SecurityOpt") or [],
        "group_add": host_config.get("GroupAdd") or [],
        "init": host_config.get("Init"),
        "tmpfs": host_config.get("Tmpfs") or {},
        "devices": host_config.get("Devices") or [],
        "device_requests": host_config.get("DeviceRequests") or [],
        "dns": host_config.get("Dns") or [],
        "extra_hosts": host_config.get("ExtraHosts") or [],
        "sysctls": host_config.get("Sysctls") or {},
        "pids_limit": host_config.get("PidsLimit"),
        "shm_size": host_config.get("ShmSize"),
        "mac_address": (blob.get("MacAddress") or "") if (blob.get("MacAddress") or "") else "",
        "mem_limit": host_config.get("Memory") or 0,
        "cpu_shares": host_config.get("CpuShares") or 0,
    }


def neutral_compose_labels(image):
    """Compose-Labels, die ein Image mitbringt, leer überschreiben – sonst zählt Compose einen daraus
    gestarteten Hilfs-Container oder Klon zum Projekt."""
    try:
        labels = client().images.get(image).labels or {}
    except Exception:
        return {}
    return {k: "" for k in labels if k.startswith("com.docker.compose.")}


def _repo_digests(image_id):
    try:
        return client().images.get(image_id).attrs.get("RepoDigests") or []
    except Exception:
        return []


def _ports_from_container(c):
    ports = {}
    for key, entries in (c.attrs.get("NetworkSettings") or {}).get("Ports", {}).items():
        for entry in entries or []:
            if entry:
                ports.setdefault(key, []).append({
                    "HostIp": entry.get("HostIp", ""),
                    "HostPort": entry.get("HostPort", ""),
                })
    return ports


# ───────── Volumes ─────────
def volume_exists(name):
    try:
        client().volumes.get(name)
        return True
    except NotFound:
        return False


def ensure_volume(name, labels=None):
    if volume_exists(name):
        return
    try:
        client().volumes.create(name=name, labels=labels or None)
    except DockerException as exc:
        raise DockerError(f"Volume '{name}' konnte nicht angelegt werden: {exc}")


def remove_volume(name):
    try:
        client().volumes.get(name).remove(force=True)
        return True
    except NotFound:
        return False
    except DockerException as exc:
        raise DockerError(f"Volume '{name}' konnte nicht entfernt werden: {exc}")


def volumes_with_label(key, value):
    """Volumes mit dem Moby-Disk-Label 'key' (ohne Präfix) = value – auch solche mit dem alten Präfix."""
    names = []
    for prefix in (LABEL_PREFIX, *LEGACY_PREFIXES):
        try:
            names += [v.name for v in client().volumes.list(filters={"label": f"{prefix}{key}={value}"})]
        except DockerException:
            pass
    return sorted(set(names))


# ───────── Start/Stopp ─────────
def stop_container(name, timeout=120):
    c = get_container(name)
    started = time.time()
    try:
        c.stop(timeout=timeout)
        c.wait(timeout=timeout + 60)
    except DockerException as exc:
        applog.get("docker").error("Container '%s' konnte nicht gestoppt werden: %s", name, exc)
        raise DockerError(f"Container '{name}' konnte nicht gestoppt werden: {exc}")
    applog.get("docker").info("Container '%s' gestoppt (%.1f s)", name, time.time() - started)


def start_container(name):
    c = get_container(name)
    try:
        c.start()
    except DockerException as exc:
        applog.get("docker").error("Container '%s' konnte nicht gestartet werden: %s", name, exc)
        raise DockerError(f"Container '{name}' konnte nicht gestartet werden: {exc}")
    applog.get("docker").info("Container '%s' gestartet", name)


def container_running(name):
    try:
        c = get_container(name)
        return c.status == "running"
    except DockerError:
        return False


def wait_state(name, seconds=60):
    """Wartet, bis der Container läuft (und ggf. 'healthy' ist). Rückgabe: (status, health)."""
    deadline = time.time() + seconds
    status, health = None, None
    while time.time() < deadline:
        try:
            c = get_container(name)
        except DockerError:
            return "missing", None
        state = c.attrs.get("State") or {}
        status, health = c.status, (state.get("Health") or {}).get("Status")
        if status in ("exited", "dead"):
            return status, health
        if status == "running" and health != "starting":
            return status, health
        time.sleep(2)
    return status, health


# ───────── exec / Dateien in Container ─────────
def exec_stream(name, cmd, out_path=None, timeout=3600, max_err=65536):
    """Führt 'cmd' im Container aus. stdout geht in out_path (Datei) oder wird gesammelt.
    Rückgabe: (exit_code, stdout_text, stderr_text)."""
    api = long_client().api if timeout >= 3600 else docker.APIClient(
        base_url="unix://" + config.docker_sock, timeout=timeout, version="auto")
    c = get_container(name)
    try:
        ex = api.exec_create(c.id, cmd, stdout=True, stderr=True)
        out_small, err = bytearray(), bytearray()
        fh = open(out_path, "wb") if out_path else None
        try:
            for out_chunk, err_chunk in api.exec_start(ex["Id"], stream=True, demux=True):
                if out_chunk:
                    if fh:
                        fh.write(out_chunk)
                    elif len(out_small) < max_err:
                        out_small.extend(out_chunk[: max_err - len(out_small)])
                if err_chunk and len(err) < max_err:
                    err.extend(err_chunk[: max_err - len(err)])
        finally:
            if fh:
                fh.close()
        code = api.exec_inspect(ex["Id"]).get("ExitCode")
    except DockerException as exc:
        raise DockerError(f"Befehl in '{name}' fehlgeschlagen: {exc}")
    except Exception as exc:   # z. B. Read-Timeout
        raise DockerError(f"Befehl in '{name}' abgebrochen: {exc}")
    return code, out_small.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def put_file(name, dest_dir, arcname, src_path):
    """Kopiert eine (auch große) Datei per Stream in den Container – ohne Zwischenkopie."""
    size = os.path.getsize(src_path)
    info = tarfile.TarInfo(arcname)
    info.size, info.mode, info.mtime = size, 0o644, int(time.time())

    def stream():
        yield info.tobuf(format=tarfile.PAX_FORMAT)
        with open(src_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                yield chunk
        yield b"\0" * ((512 - size % 512) % 512) + b"\0" * 1024

    try:
        ok = long_client().api.put_archive(get_container(name).id, dest_dir, stream())
    except DockerException as exc:
        raise DockerError(f"Datei konnte nicht nach '{name}' kopiert werden: {exc}")
    if not ok:
        raise DockerError(f"Datei konnte nicht nach '{name}:{dest_dir}' kopiert werden.")


# ───────── Hilfs-Container für Schreibzugriffe ─────────
_worker_lock = threading.Lock()


def run_worker(args, binds, timeout=6 * 3600):
    """Startet einen kurzlebigen Container aus dem Helper-Image mit 'binds' ({host: {bind, mode}})
    und führt 'python -m app.worker <args>' aus. So bleiben die Host-Freigaben im Helper nur lesend."""
    own = own_container_id()
    if not own:
        raise DockerError("Hilfs-Container nicht möglich: der Helper läuft nicht in Docker.")
    image = client().api.inspect_container(own)["Image"]
    with _worker_lock:
        try:
            c = long_client().containers.run(
                image, ["python", "-m", "app.worker", *args], volumes=binds, network_mode="none",
                detach=True, working_dir="/app", labels={**own_labels({"task": "1", "backup": "false"}),
                                                         **neutral_compose_labels(image)},
                name=f"mobydisk-task-{int(time.time() * 1000) % 10 ** 9}")
        except DockerException as exc:
            raise DockerError(f"Hilfs-Container konnte nicht gestartet werden: {exc}")
        try:
            result = c.wait(timeout=timeout)
            logs = c.logs().decode("utf-8", "replace")
        finally:
            try:
                c.remove(force=True)
            except DockerException:
                pass
    if result.get("StatusCode") != 0:
        raise DockerError(f"Hilfs-Container fehlgeschlagen: {logs.strip()[-600:]}")
    return logs.strip()


# ───────── Images ─────────
def image_present(ref):
    try:
        client().images.get(ref)
        return True
    except Exception:
        return False


def load_image(tar_path):
    try:
        with open(tar_path, "rb") as fh:
            images = long_client().images.load(fh)
        return [t for img in images for t in (img.tags or [img.id])]
    except DockerException as exc:
        raise DockerError(f"Image konnte nicht geladen werden: {exc}")


def pull_image(image):
    try:
        long_client().images.pull(image)
    except DockerException as exc:
        raise DockerError(f"Image '{image}' konnte nicht geladen werden: {exc}")


def tag_image(image_id, ref):
    """Gibt einem (z. B. per load ohne Namen geladenen) Image wieder seinen Namen 'repo:tag'."""
    if not ref or "@" in ref:
        return
    if ":" in ref.rsplit("/", 1)[-1]:
        repo, tag = ref.rsplit(":", 1)
    else:
        repo, tag = ref, "latest"
    try:
        client().images.get(image_id).tag(repo, tag)
    except DockerException as exc:
        raise DockerError(f"Image konnte nicht als '{ref}' markiert werden: {exc}")


# ───────── Container anlegen ─────────
def _mounts_from_snapshot(snapshot):
    mounts = []
    for m in snapshot.get("mounts") or []:
        if m["type"] == "volume":
            mounts.append(docker.types.Mount(target=m["destination"], source=m.get("name"), type="volume",
                                             read_only=bool(m.get("readonly"))))
        elif m["type"] == "bind":
            mounts.append(docker.types.Mount(target=m["destination"], source=m["source"], type="bind",
                                             read_only=bool(m.get("readonly"))))
    return mounts


def create_kwargs(snapshot):
    """Docker-SDK-Parameter, um einen Container wie im Snapshot anzulegen."""
    kwargs = {"name": snapshot["name"], "image": snapshot.get("image") or snapshot.get("image_id"), "detach": True}
    mounts = _mounts_from_snapshot(snapshot)
    if mounts:
        kwargs["mounts"] = mounts
    simple = {
        "command": "command", "entrypoint": "entrypoint", "env": "environment", "labels": "labels",
        "user": "user", "working_dir": "working_dir", "hostname": "hostname", "stop_signal": "stop_signal",
        "healthcheck": "healthcheck", "runtime": "runtime", "cap_add": "cap_add", "cap_drop": "cap_drop",
        "security_opt": "security_opt", "group_add": "group_add", "tmpfs": "tmpfs", "devices": "devices",
        "dns": "dns", "extra_hosts": "extra_hosts", "sysctls": "sysctls", "pids_limit": "pids_limit",
        "shm_size": "shm_size", "mac_address": "mac_address", "mem_limit": "mem_limit",
        "cpu_shares": "cpu_shares", "restart_policy": "restart_policy",
    }
    for key, arg in simple.items():
        if snapshot.get(key):
            kwargs[arg] = snapshot[key]
    if snapshot.get("tty"):
        kwargs["tty"] = True
    if snapshot.get("stdin_open"):
        kwargs["stdin_open"] = True
    if snapshot.get("privileged"):
        kwargs["privileged"] = True
    if snapshot.get("init"):
        kwargs["init"] = True
    if snapshot.get("device_requests"):
        kwargs["device_requests"] = [docker.types.DeviceRequest(
            driver=d.get("Driver") or "", count=d.get("Count") or 0, device_ids=d.get("DeviceIDs") or None,
            capabilities=d.get("Capabilities") or None, options=d.get("Options") or None)
            for d in snapshot["device_requests"]]
    if snapshot.get("ports"):
        kwargs["ports"] = {key: [(e.get("HostIp") or "", e.get("HostPort") or "") for e in entries]
                           for key, entries in snapshot["ports"].items()}
    network_mode = snapshot.get("network_mode") or "default"
    if network_mode not in ("default", "host", "none") and not network_mode.startswith("container:"):
        kwargs["network_mode"] = "default"
        kwargs["_attach_networks"] = [network_mode]
    else:
        kwargs["network_mode"] = network_mode
    return kwargs


def create_container(kwargs):
    attach_networks = kwargs.pop("_attach_networks", None) or []
    try:
        container = client().containers.create(**kwargs)
    except DockerException as exc:
        raise DockerError(f"Container '{kwargs.get('name')}' konnte nicht angelegt werden: {exc}")
    for net_name in attach_networks:
        try:
            client().networks.get(net_name).connect(container)
        except DockerException as exc:
            raise DockerError(f"Netzwerk '{net_name}' konnte nicht verbunden werden: {exc}")
    return container.id


def recreate_container(snapshot):
    name = snapshot["name"]
    if any(c.name.lstrip("/") == name for c in client().containers.list(all=True)):
        raise DockerError(f"Container '{name}' existiert bereits. Recreate nur bei fehlendem Container.")
    for mount in snapshot.get("mounts") or []:
        if mount["type"] == "volume" and mount.get("name"):
            ensure_volume(mount["name"])
    return create_container(create_kwargs(snapshot))


def remove_container(name):
    try:
        get_container(name).remove(force=True)
    except DockerException as exc:
        raise DockerError(f"Container '{name}' konnte nicht entfernt werden: {exc}")
