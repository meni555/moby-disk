FROM python:3.12-slim

WORKDIR /app

# restic: Backup-Ziel mit inhaltsbasierter Deduplizierung (nur genutzt, wenn TARGETS=restic gesetzt ist)
RUN apt-get update && apt-get install -y --no-install-recommends restic ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Proxmox-Backup-Client aus dem offiziellen pbs-client-Repository (nur genutzt, wenn TARGETS=pbs gesetzt ist).
# Der Signaturschlüssel wird gegen seine Prüfsumme geprüft; ändert Proxmox den Schlüssel, schlägt der Bau hier fehl.
# Für Architekturen ohne Paket wird der Client übersprungen (das Ziel meldet das dann).
ARG PROXMOX_KEY_SHA256=1bcd2d5bab556076c9ea756a84fe2b7445b13f4ef6e97b2e412b68778377ba6d
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && curl -fsSL https://enterprise.proxmox.com/debian/proxmox-release-trixie.gpg -o /usr/share/keyrings/proxmox-archive-keyring.gpg \
    && echo "${PROXMOX_KEY_SHA256}  /usr/share/keyrings/proxmox-archive-keyring.gpg" | sha256sum -c - \
    && echo "deb [signed-by=/usr/share/keyrings/proxmox-archive-keyring.gpg] http://download.proxmox.com/debian/pbs-client trixie main" > /etc/apt/sources.list.d/pbs-client.list \
    && apt-get update \
    && (apt-get install -y --no-install-recommends proxmox-backup-client || echo "HINWEIS: proxmox-backup-client ist für diese Architektur nicht verfügbar") \
    && apt-get purge -y curl && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

CMD ["gunicorn", "-b", "0.0.0.0:8000", "-w", "1", "--threads", "8", "--timeout", "300", "app.main:app"]
