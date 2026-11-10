"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/* ───────── Persistenz (nur Komfort, darf fehlschlagen) ───────── */
const store = {
  get(key, fallback) {
    try {
      const raw = localStorage.getItem("cvh-" + key);
      return raw === null ? fallback : JSON.parse(raw);
    } catch (_e) { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem("cvh-" + key, JSON.stringify(value)); } catch (_e) { /* ignore */ }
  },
};

const appState = {
  containers: [],
  containersLoaded: false,
  jobs: [],
  schedules: [],
  editingSchedule: null,
  clients: [],
  subclients: [],
  restoreTarget: null,
  settingsSec: store.get("settings-sec", "targets"),
  versionsFor: null,
  expanded: new Set(store.get("expanded", [])),
  selected: new Set(),
  lastClicked: null,
  visible: [],
  openLogs: new Set(),
  cFilter: Object.assign({ q: "", state: "all", dataOnly: false, noBackup: false, unprot: false, stack: "" }, store.get("c-filter", {})),
  forecast: null,
  plans: {},
  cSort: Object.assign({ key: "name", dir: "asc" }, store.get("c-sort", {})),
  jFilter: Object.assign({ q: "", status: "all", kind: "all", sort: "new" }, store.get("j-filter", {})),
  vSort: {},
  versions: {},
};
appState.cFilter.q = "";   // Suchtext nicht über Reloads hinweg behalten

/* ───────── Helpers ───────── */
async function api(path, options = {}) {
  const opts = { headers: {}, ...options };
  if (opts.body) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.body);
  }
  const resp = await fetch(path, opts);
  if (resp.status === 401) {
    showLogin();
    throw new Error("Nicht angemeldet");
  }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function hl(text, q) {
  const raw = String(text ?? "");
  if (!q) return esc(raw);
  const re = new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "ig");
  let out = "", last = 0, m;
  while ((m = re.exec(raw)) !== null) {
    if (m[0] === "") { re.lastIndex++; continue; }
    out += esc(raw.slice(last, m.index)) + "<mark>" + esc(m[0]) + "</mark>";
    last = m.index + m[0].length;
  }
  return out + esc(raw.slice(last));
}

function icon(name, cls = "") {
  return `<svg class="ico ${cls}"><use href="#i-${name}"/></svg>`;
}

function toast(message, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), 6000);
}

function fmtTime(ts) {
  if (!ts) return "-";
  return new Date(ts * 1000).toLocaleString("de-DE", { timeZone: "Europe/Berlin", dateStyle: "medium", timeStyle: "medium" });
}

function fmtDate(d) {
  return d.toLocaleString("de-DE", { timeZone: "Europe/Berlin", dateStyle: "medium", timeStyle: "medium" });
}

function ago(date) {
  const s = Math.round((Date.now() - date.getTime()) / 1000);
  if (s < 45) return "gerade eben";
  if (s < 3600) return `vor ${Math.round(s / 60)} Min.`;
  if (s < 86400) return `vor ${Math.round(s / 3600)} Std.`;
  if (s < 86400 * 45) return `vor ${Math.round(s / 86400)} Tg.`;
  return fmtDate(date);
}

function fmtSize(bytes) {
  if (bytes === null || bytes === undefined) return "-";
  const n = Number(bytes);
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(Math.floor(Math.log(n) / Math.log(1024)), units.length - 1);
  return (n / Math.pow(1024, i)).toFixed(i ? 1 : 0) + " " + units[i];
}

function fmtDuration(sec) {
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return `${sec} s`;
  if (sec < 3600) return `${Math.floor(sec / 60)} min ${sec % 60} s`;
  return `${Math.floor(sec / 3600)} h ${Math.floor((sec % 3600) / 60)} min`;
}

function parseLocalTs(ts) {
  const m = /^(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})-(\d{2})-(\d+)Z$/.exec(ts || "");
  if (!m) return null;
  const d = new Date(`${m[1]}T${m[2]}:${m[3]}:${m[4]}.${m[5].slice(0, 3)}Z`);
  return isNaN(d) ? null : d;
}

function cmp(a, b) {
  if (typeof a === "number" && typeof b === "number") return a - b;
  return String(a ?? "").localeCompare(String(b ?? ""), "de", { numeric: true, sensitivity: "base" });
}

function debounce(fn, ms = 120) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

/* ───────── Theme / Views / Modals ───────── */
function applyThemeIcon() {
  const theme = document.documentElement.dataset.theme || "light";
  $("#theme-btn").innerHTML = icon(theme === "light" ? "moon" : "sun");
}

function toggleTheme() {
  const cur = document.documentElement.dataset.theme || "light";
  const next = cur === "light" ? "dark" : "light";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("cvh-theme", next); } catch (_e) { /* ignore */ }
  applyThemeIcon();
}

function showLogin() {
  $("#login-view").style.display = "flex";
  $("#app-view").style.display = "none";
}
function showApp() {
  $("#login-view").style.display = "none";
  $("#app-view").style.display = "block";
}

function openModal(id) { const m = $(`#${id}`); m.classList.add("open"); m.setAttribute("aria-hidden", "false"); }
function closeModal(id) { const m = $(`#${id}`); m.classList.remove("open"); m.setAttribute("aria-hidden", "true"); }

function switchTab(name) {
  $$("nav.tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  const activeTab = $(`nav.tabs button[data-tab="${name}"]`);
  const strip = activeTab?.parentElement;
  if (strip && strip.scrollWidth > strip.clientWidth) {   // mobil: aktiven Tab in der Leiste sichtbar halten
    strip.scrollTo({ left: activeTab.offsetLeft - (strip.clientWidth - activeTab.offsetWidth) / 2, behavior: "smooth" });
  }
  $$("main section").forEach((s) => s.classList.toggle("active", s.id === `tab-${name}`));
  store.set("tab", name);
  const titles = { containers: "Docker-Container", stacks: "Stacks", logs: "Logs", jobs: "Aufträge", backups: "Backups", capacity: "Kapazität &amp; Prognose", schedules: "Zeitpläne", settings: "Einstellungen" };
  const plain = (titles[name] || name).replace(/&amp;/g, "&");
  $("#page-title").textContent = plain;
  $("#crumb").textContent = plain;
  document.title = `${plain} · Moby Disk`;
  if (name === "stacks") loadStacks();
  logsActive(name === "logs");
  if (name === "jobs") loadJobs();
  if (name === "schedules") loadSchedules();
  if (name === "backups") loadBackups();
  if (name === "capacity") loadForecast();
  if (name === "settings") loadSettings();
}

/* ───────── Status ───────── */
async function loadStatus() {
  try {
    const status = await api("/api/status");
    const d = $("#badge-docker");
    d.classList.toggle("ok", status.docker_ok);
    d.classList.toggle("bad", !status.docker_ok);
    d.title = status.docker_error || "Docker erreichbar";
    const c = $("#badge-cv");
    c.hidden = !(status.targets || []).some((t) => t.id === "commvault" && t.enabled);
    c.classList.toggle("ok", status.cv_configured);
    c.classList.toggle("bad", !status.cv_configured);
    c.title = status.cv_configured ? (status.cv_url || "") : "Commvault nicht konfiguriert";
    $("#badge-targets").innerHTML = (status.targets || []).filter((t) => t.id !== "commvault" && t.enabled).map((t) =>
      `<span class="pill ${t.ready ? "ok" : "bad"}" title="${t.ready ? "bereit" : "nicht eingerichtet"}"><i class="dot"></i>${esc(t.label)}</span>`).join("");
    appState.status = status;
    renderTargetsPanel();
    renderSettingsSections();
    $("#wordmark").title = status.host;   // Host-Name nur noch als Tooltip
    if (status.ui_version && window.CVH_UI_VERSION && status.ui_version !== window.CVH_UI_VERSION && !appState.updateShown) {
      appState.updateShown = true;   // Server läuft mit neuerer Oberfläche als dieser Tab
      const bar = document.createElement("div");
      bar.className = "update-bar";
      bar.innerHTML = `${icon("refresh")}<span>Moby Disk wurde aktualisiert – diese Seite ist noch die alte Version.</span><button class="btn primary sm">Neu laden</button>`;
      bar.querySelector("button").addEventListener("click", () => location.reload());
      document.body.appendChild(bar);
    }
    appState.backupDisk = status.backup_disk || null;
    appState.status = status;
    if (appState.containersLoaded) renderStats();
    const warnings = [];
    if (!status.docker_ok) warnings.push(`Docker nicht erreichbar: ${status.docker_error}`);
    const hasCv = (status.targets || []).some((t) => t.id === "commvault" && t.enabled);
    if (hasCv && !status.cv_configured) warnings.push("Commvault-Zugangsdaten fehlen (CV_URL/CV_USERNAME/CV_PASSWORD).");
    else if (hasCv && (!status.identity.client_id || !status.identity.subclient_id)) {
      warnings.push("Commvault-Client/Subclient noch nicht gewählt – bitte in den Einstellungen festlegen.");
    }
    if (status.password_is_default) warnings.push("APP_PASSWORD ist noch 'changeme' – bitte ändern!");
    $("#containers-notice").innerHTML = warnings
      .map((w) => `<div class="notice">${icon("alert")}<span>${esc(w)}</span></div>`).join("");
  } catch (err) {
    toast(err.message, "err");
  }
}

/* ───────── Container: Laden, Filtern, Sortieren, Rendern ───────── */
function skeletonRows(n = 5) {
  return Array.from({ length: n }, () =>
    `<tr class="skeleton">${'<td><div></div></td>'.repeat(7)}</tr>`).join("");
}

async function loadContainers(manual = false) {
  const btn = $("#refresh-containers");
  if (manual) btn.classList.add("spin");
  if (!appState.containersLoaded) $("#container-rows").innerHTML = skeletonRows();
  try {
    appState.containers = await api("/api/containers");
    appState.containersLoaded = true;
    renderContainers();
  } catch (err) {
    toast(err.message, "err");
  } finally {
    btn.classList.remove("spin");
  }
}

function containerMatches(c, f) {
  if (f.state === "running" && !c.running) return false;
  if (f.state === "stopped" && c.running) return false;
  if (f.dataOnly && !c.has_data) return false;
  if (f.noBackup && c.local_backups > 0) return false;
  if (f.unprot && !["none", "stale"].includes(c.protection?.state)) return false;
  if (f.stack && c.stack !== f.stack) return false;
  if (f.q) {
    const hay = [c.name, c.image, c.short_id, c.state, c.stack, c.clone_of, c.backup_settings?.dump,
      ...c.mounts.flatMap((m) => [m.name, m.source, m.destination])].join(" ").toLowerCase();
    return f.q.toLowerCase().split(/\s+/).filter(Boolean).every((tok) => hay.includes(tok));
  }
  return true;
}

const SORTERS = {
  name: (c) => c.name,
  image: (c) => c.image,
  state: (c) => (c.running ? 0 : 1) + c.state,
  protection: (c) => {
    const st = c.protection?.state;
    const rank = { none: 0, stale: 1, ok: 2, external: 3, "n/a": 3 }[st] ?? 4;
    return rank * 1e6 - Math.min(c.protection?.age_hours || 0, 999999) / 1e3;   // kritisch zuerst, älteste zuerst
  },
  mounts: (c) => c.mounts.filter((m) => m.type === "volume" || m.type === "bind").length,
  backups: (c) => c.local_backups,
  size: (c) => c.local_size || 0,
  created: (c) => (c.created ? Date.parse(c.created) || 0 : 0),
};

function sortedContainers(list) {
  const { key, dir } = appState.cSort;
  const get = SORTERS[key] || SORTERS.name;
  const sign = dir === "desc" ? -1 : 1;
  return list.slice().sort((a, b) => sign * cmp(get(a), get(b)) || cmp(a.name, b.name));
}

function renderStats() {
  const list = appState.containers;
  $("#st-total").textContent = list.length;
  $("#st-running").textContent = list.filter((c) => c.running).length;
  const withData = list.filter((c) => c.has_data);
  const good = withData.filter((c) => c.protection?.state === "ok").length;
  const bad = withData.filter((c) => ["none", "stale"].includes(c.protection?.state)).length;
  $("#st-protected").textContent = `${good}/${withData.length}`;
  $("#st-protected-sub").textContent = bad ? `${bad} ungeschützt oder veraltet` : "alle im Schutzziel";
  $("#stat-protect").classList.toggle("warn", bad > 0);
  const count = list.reduce((n, c) => n + (c.local_backups || 0), 0);
  const bytes = list.reduce((n, c) => n + (c.local_size || 0), 0);
  $("#st-size").textContent = fmtSize(bytes);
  const free = appState.backupDisk ? ` · ${fmtSize(appState.backupDisk.free)} frei` : "";
  $("#st-backups-sub").textContent = `${count} ${count === 1 ? "Sicherung" : "Sicherungen"}${free}`;
  $("#st-backups-sub").title = appState.backupDisk
    ? `Backup-Ordner: ${fmtSize(appState.backupDisk.used)} von ${fmtSize(appState.backupDisk.total)} belegt, ${fmtSize(appState.backupDisk.free)} frei`
    : "";
}

function imageHtml(image, q) {
  const i = image.lastIndexOf(":");
  const slash = image.lastIndexOf("/");
  if (i > slash && i > 0) {
    return `${hl(image.slice(0, i), q)}<span class="tag">:${hl(image.slice(i + 1), q)}</span>`;
  }
  return hl(image, q);
}

function mountsDetail(c) {
  const rows = c.mounts.filter((m) => m.type === "volume" || m.type === "bind");
  if (!rows.length) return '<span class="muted">Keine Volumes oder Bind-Mounts.</span>';
  return `<div class="mounts">${rows.map((m) => `
    <div class="mount">
      <span class="kind ${m.type === "bind" ? "bind" : ""}">${m.type === "bind" ? "Bind" : "Volume"}</span>
      <code>${esc(m.type === "volume" ? (m.name || m.source) : m.source)}</code>
      <span class="arrow">→</span>
      <code>${esc(m.destination)}</code>
      ${m.readonly ? '<span class="chip">nur lesen</span>' : ""}
    </div>`).join("")}</div>`;
}

function skipChip(c) {
  const plan = c.mount_plan || [];
  const direct = plan.filter((d) => d.reason === "cv_direct" || d.direct_sub);
  const skipped = plan.filter((d) => d.action === "skip" && d.reason !== "cv_direct");
  const tip = skipped.map((d) => `${d.destination}: ${d.text}`).join("\n");
  const dtip = direct.map((d) => `${d.destination}: ${d.text}`).join("\n");
  return (direct.length ? `<span class="chip cvd" title="${esc(dtip)}">↗ ${direct.length} direkt über CV</span>` : "") +
    (skipped.length ? `<span class="chip warnchip" title="${esc(tip)}">⚠ ${skipped.length} übersprungen</span>` : "");
}

const DUMP_LABEL = { mariadb: "MariaDB-Dump", postgres: "Postgres-Dump", redis: "Redis-SAVE", mongo: "Mongo-Dump" };

function settingsChips(c) {
  const b = c.backup_settings || {};
  const out = [];
  if (b.dump) out.push(`<span class="chip cv" title="Vor jedem Backup läuft ein Datenbank-Dump – auch im Live-Modus konsistent">${icon("db")}${esc(DUMP_LABEL[b.dump] || b.dump)}</span>`);
  if (b.pre || b.post) out.push(`<span class="chip" title="${esc([b.pre ? "Pre-Hook" : "", b.post ? "Post-Hook" : ""].filter(Boolean).join(" + "))}">Hooks</span>`);
  if (b.mode) out.push(`<span class="chip" title="Fest eingestellter Modus für diesen Container">${b.mode === "stop" ? "immer stoppen" : "immer live"}</span>`);
  if (b.image === "never") out.push('<span class="chip" title="Image wird nie gesichert">kein Image</span>');
  else if (b.image === "always" || b.image_origin === "local" || b.image_origin === "private") {
    out.push(`<span class="chip" title="${esc(b.image_origin_text || "")}${b.image === "always" ? " – per Einstellung immer" : " – wird bei „nur selbst gebaute“ mitgesichert"}">${icon("box")}eigenes Image</span>`);
  }
  return out.join("");
}

function protectionCell(c) {
  const p = c.protection || {};
  if (p.state === "n/a" || !c.has_data) return '<span class="muted">–</span>';
  if (p.state === "clone") return '<span class="state">Klon</span><div class="prot-sub"><span class="muted small">wird nicht gesichert</span></div>';
  if (p.state === "excluded") return '<span class="state">Ausgenommen</span><div class="prot-sub"><span class="muted small">Label mobydisk.backup=false</span></div>';
  if (p.state === "direct") return '<span class="state finished">Direkt über CV</span><div class="prot-sub"><span class="muted small">Commvault sichert alle Daten selbst</span></div>';
  if (p.state === "external") return '<span class="state">Daten extern</span><div class="prot-sub"><span class="muted small">alle Mounts ausgeschlossen</span></div>';
  const label = { ok: ["finished", "Geschützt"], stale: ["queued", "Veraltet"], none: ["failed", "Nie gesichert"] }[p.state] || ["", p.state];
  const age = p.last_backup && !p.cv_only ? ago(new Date(p.last_backup * 1000)) : "";
  const where = Object.keys(c.target_backups || {}).map(sourceLabel).join(" · ") || "Ziel";
  const cv = p.cv_last ? `<span class="muted small" title="Jüngste Sicherung in ${esc(where)}: ${esc(fmtDate(new Date(p.cv_last * 1000)))}${p.cv_only ? " – lokal keine Kopie mehr" : ""}">${esc(where)} ✓ ${esc(ago(new Date(p.cv_last * 1000)))}${p.cv_only ? " · nur im Ziel" : ""}</span>` : "";
  const plan = p.scheduled ? "" : '<span class="muted small">kein Zeitplan</span>';
  return `<span class="state ${label[0]}">${label[1]}</span><div class="prot-sub">${age ? `<span class="muted small">${esc(age)}</span>` : ""}${cv}${plan}</div>`;
}

function renderContainers() {
  renderStats();
  const f = appState.cFilter;
  const all = appState.containers;
  const list = sortedContainers(all.filter((c) => containerMatches(c, f)));
  const q = f.q.trim();

  // Header-Sortierzustand
  $$("#c-table th.sortable").forEach((th) => {
    const on = th.dataset.sort === appState.cSort.key;
    th.classList.toggle("sorted", on);
    th.setAttribute("aria-sort", on ? (appState.cSort.dir === "asc" ? "ascending" : "descending") : "none");
  });
  const sel = `${appState.cSort.key}:${appState.cSort.dir}`;
  if ($("#c-sort").querySelector(`option[value="${sel}"]`)) $("#c-sort").value = sel;

  // Auswahl auf existierende Container beschränken
  const names = new Set(all.map((c) => c.name));
  for (const n of Array.from(appState.selected)) if (!names.has(n)) appState.selected.delete(n);
  appState.visible = list.map((c) => c.name);

  const tbody = $("#container-rows");
  if (!all.length) {
    tbody.innerHTML = `<tr><td colspan="7"><div class="empty">${icon("box")}<strong>Keine Container gefunden</strong>Auf diesem Host laufen aktuell keine Docker-Container.</div></td></tr>`;
  } else if (!list.length) {
    tbody.innerHTML = `<tr><td colspan="7"><div class="empty">${icon("search")}<strong>Keine Treffer</strong>Passe Suche oder Filter an. <br><button class="btn ghost sm" id="reset-filters" style="margin-top:12px">Filter zurücksetzen</button></div></td></tr>`;
  } else {
    tbody.innerHTML = list.map((c) => {
      const vols = c.mounts.filter((m) => m.type === "volume").length;
      const binds = c.mounts.filter((m) => m.type === "bind").length;
      const open = appState.expanded.has(c.name);
      const chipHtml = `${vols ? `<span class="chip vol">${icon("db")}${vols} Volume${vols > 1 ? "s" : ""}</span>` : ""}${binds ? `<span class="chip bind">${icon("link")}${binds} Bind</span>` : ""}${skipChip(c)}${settingsChips(c)}`;
      const chips = chipHtml ? `<div class="chips">${chipHtml}</div>` : '<span class="muted">–</span>';
      const n = c.local_backups;
      const nm = esc(c.name);
      return `
      <tr class="item ${open ? "open" : ""} ${appState.selected.has(c.name) ? "selected" : ""}" data-name="${nm}">
        <td class="name">
          <div class="cname">
            <input type="checkbox" class="sel-box" data-sel="${nm}" aria-label="${nm} auswählen" ${appState.selected.has(c.name) ? "checked" : ""}>
            <button class="chev" data-toggle="${nm}" aria-label="Details" aria-expanded="${open}">${icon("chevron")}</button>
            <div><strong>${hl(c.name, q)}</strong>${c.is_self ? '<span class="badge-self" title="Moby Disk selbst – wird immer live gesichert">Moby Disk</span>' : ""}${c.clone_of ? `<span class="badge-clone" title="Sandbox-Klon aus einer Sicherung von ${esc(c.clone_of)}">Klon von ${esc(c.clone_of)}</span>` : ""}<br><span class="id">${hl(c.short_id, q)}</span>${c.stack ? ` <button class="stack-chip" data-stack="${esc(c.stack)}" title="Alle Container des Stacks '${esc(c.stack)}' auswählen">${icon("box")}${hl(c.stack, q)}</button>` : ""}</div>
          </div>
        </td>
        <td class="image" data-label="Image">${imageHtml(c.image, q)}</td>
        <td data-label="Status"><span class="state ${esc(c.state)}">${hl(c.state, q)}</span></td>
        <td data-label="Schutz">${protectionCell(c)}</td>
        <td data-label="Daten">${chips}</td>
        <td data-label="Sicherungen" class="backups-cell">${n ? `<b>${n}</b> ${n === 1 ? "Kopie" : "Kopien"}<br><span class="muted small">${esc(fmtSize(c.local_size))}</span>` : `<span class="muted">${c.cv_backups ? "keine lokal" : "keine"}</span>`}${c.cv_backups ? `<br><span class="muted small" title="In Backup-Zielen vermerkte Sicherungen">${c.cv_backups} in Zielen</span>` : ""}</td>
        <td class="right actions-cell">${c.clone_of ? `
          <button class="btn primary sm" data-clone-promote="${nm}" title="Denselben Sicherungsstand ins Original '${esc(c.clone_of)}' übernehmen, danach den Klon entfernen">${icon("check")}Übernehmen</button>
          <button class="btn ghost sm" data-clone-drop="${nm}" title="Klon samt seinen Volumes löschen" aria-label="Klon ${nm} verwerfen">${icon("trash")}</button>` : `
          <button class="btn primary sm" data-action="backup" data-name="${nm}">${icon("archive")}Backup</button>
          <button class="btn ghost sm" data-action="versions" data-name="${nm}" title="Versionen anzeigen / wiederherstellen" aria-label="Versionen von ${nm}">${icon("clock")}</button>`}
        </td>
      </tr>
      ${open ? `<tr class="detail"><td colspan="7"><div class="plan" data-plan="${nm}">${planHtml(c.name)}</div></td></tr>` : ""}`;
    }).join("");
    list.filter((c) => appState.expanded.has(c.name)).forEach((c) => refreshPlan(c.name));
  }

  const stacks = Array.from(new Set(all.map((c) => c.stack).filter(Boolean))).sort((a, b) => cmp(a, b));
  const sel2 = $("#c-stack");
  const want = `<option value="">Alle</option>${stacks.map((st) => `<option value="${esc(st)}">${esc(st)} (${all.filter((c) => c.stack === st).length})</option>`).join("")}`;
  if (sel2.dataset.sig !== want) { sel2.innerHTML = want; sel2.dataset.sig = want; }
  sel2.value = stacks.includes(f.stack) ? f.stack : "";
  const filtered = list.length !== all.length;
  $("#c-count").textContent = all.length
    ? `${list.length}${filtered ? ` von ${all.length}` : ""} Container`
    : "";
  $("#c-search-clear").hidden = !f.q;
  updateSelectionUi();
}

function updateSelectionUi() {
  const sel = appState.selected;
  const visible = appState.visible;
  const selVisible = visible.filter((n) => sel.has(n)).length;
  const all = $("#c-selall");
  all.checked = visible.length > 0 && selVisible === visible.length;
  all.indeterminate = selVisible > 0 && selVisible < visible.length;
  const bar = $("#bulkbar");
  bar.hidden = sel.size === 0;
  if (sel.size) {
    const list = Array.from(sel);
    $("#bulk-count").textContent = `${sel.size} Container ausgewählt`;
    $("#bulk-names").textContent = list.slice(0, 6).join(", ") + (list.length > 6 ? ` … +${list.length - 6}` : "");
  }
  $$("#container-rows tr.item").forEach((tr) => {
    const on = sel.has(tr.dataset.name);
    tr.classList.toggle("selected", on);
    const box = tr.querySelector(".sel-box");
    if (box) box.checked = on;
  });
}

function setSelected(name, on) {
  if (on) appState.selected.add(name); else appState.selected.delete(name);
}

function persistContainerPrefs() {
  const { state, dataOnly, noBackup, unprot, stack } = appState.cFilter;
  store.set("c-filter", { state, dataOnly, noBackup, unprot, stack });
  store.set("c-sort", appState.cSort);
  store.set("expanded", Array.from(appState.expanded));
}

/* ───────── Dialoge: Backup / Versionen / Restore ───────── */
function openBackupModal(names) {
  const list = Array.isArray(names) ? names : [names];
  appState.backupTargets = list;
  const multi = list.length > 1;
  $("#backup-heading").textContent = multi ? `Backup für ${list.length} Container erstellen` : "Backup erstellen";
  $("#backup-title").textContent = multi ? "Sammel-Backup" : list[0];
  const targets = $("#backup-targets");
  targets.hidden = !multi;
  targets.innerHTML = multi ? list.map((n) => `<span class="chip">${icon("box")}${esc(n)}</span>`).join("") : "";
  $("#backup-submit").textContent = multi ? `${list.length} Backups starten` : "Backup starten";
  renderTargetPick($("#backup-targets-pick"), []);
  $("#backup-requirecv").checked = readyTargets().length > 0;   // ohne Ziel kann nichts bestätigt werden
  const skipHtml = skipNoteText(list);
  $("#backup-skip-note").hidden = !skipHtml;
  $("#backup-skip-note").innerHTML = skipHtml;
  $("#backup-self-note").hidden = !list.some((n) => appState.containers.find((c) => c.name === n)?.is_self);
  const picked = appState.containers.filter((c) => list.includes(c.name));
  const stacks = new Set(picked.map((c) => c.stack).filter(Boolean));
  const extra = appState.containers.filter((c) => stacks.has(c.stack) && !list.includes(c.name) && !c.clone_of).map((c) => c.name);
  const sameStack = picked.some((c) => c.stack && picked.filter((x) => x.stack === c.stack).length > 1);
  $("#backup-stack-wrap").hidden = !(extra.length || sameStack);
  $("#backup-wholestacks").checked = false;
  $("#backup-wholestacks").disabled = !extra.length;
  $("#backup-stack-add").textContent = extra.length
    ? `(ergänzt ${extra.length}: ${extra.slice(0, 6).join(", ")}${extra.length > 6 ? " …" : ""})`
    : "(alle Container der Stacks sind schon ausgewählt)";
  openModal("modal-backup");
}

function sortIcon() { return icon("sort", "sort"); }

/* ───────── Logs ───────── */
const logState = { last: 0, entries: [], timer: null, busy: false, day: "" };
const LOG_SHORT = { DEBUG: "DEBUG", INFO: "INFO", WARNING: "WARN", ERROR: "FEHLER" };

function logsActive(on) {
  clearInterval(logState.timer);
  logState.timer = null;
  if (!on) return;
  loadLogs(true);
  logState.timer = setInterval(() => { if ($("#log-live").checked) loadLogs(false); }, 3000);
}

function logRowHtml(e) {
  const d = new Date(e.t * 1000);
  const day = d.toLocaleDateString("de-DE", { timeZone: "Europe/Berlin", weekday: "short", day: "2-digit", month: "2-digit", year: "numeric" });
  const time = d.toLocaleTimeString("de-DE", { timeZone: "Europe/Berlin", hour12: false }) + "." + String(Math.floor((e.t % 1) * 1000)).padStart(3, "0");
  let head = "";
  if (day !== logState.day) { logState.day = day; head = `<div class="lg-day">${esc(day)}</div>`; }
  const msg = esc(e.msg).replace(/\[([0-9a-f]{12})\]/g, '<a href="#" class="lg-job" data-log-q="$1" title="Nur diesen Auftrag zeigen">[$1]</a>');
  return `${head}<div class="lg lv-${esc(e.level)}"><span class="lt">${esc(time)}</span><span class="ll">${esc(LOG_SHORT[e.level] || e.level)}</span><span class="ln">${esc(e.logger)}</span><span class="lm">${msg}${e.exc ? `<details><summary>Traceback</summary><pre>${esc(e.exc)}</pre></details>` : ""}</span></div>`;
}

async function loadLogs(reset) {
  if (logState.busy) return;
  logState.busy = true;
  const view = $("#log-view");
  try {
    const p = new URLSearchParams({ level: $("#log-level").value, logger: $("#log-logger").value, q: $("#log-q").value.trim(), limit: reset ? 800 : 400 });
    if (!reset && logState.last) p.set("since", logState.last);
    const data = await api(`/api/logs?${p}`);
    const atEnd = view.scrollTop + view.clientHeight >= view.scrollHeight - 48;
    if (reset) { view.innerHTML = ""; logState.day = ""; logState.entries = []; }
    if (data.entries.length) {
      view.insertAdjacentHTML("beforeend", data.entries.map(logRowHtml).join(""));
      logState.entries.push(...data.entries);
      const excess = view.querySelectorAll(".lg").length - 2000;   // Anzeige begrenzen
      for (let i = 0; i < excess; i++) view.querySelector(".lg").remove();
      if (reset || atEnd) view.scrollTop = view.scrollHeight;
    } else if (reset) {
      view.innerHTML = '<div class="empty"><strong>Keine Einträge</strong>Für diese Filter gibt es (noch) keine Log-Einträge.</div>';
    }
    logState.last = data.last_id || logState.last;
    $("#log-record").value = data.level;
    $("#log-meta").textContent = `${logState.entries.length} Einträge in der Ansicht · Datei: ${data.file.path} (${fmtSize(data.file.bytes)}, rotierend 5 × 5 MB)`;
  } catch (err) {
    if (reset) view.innerHTML = `<div class="notice">${icon("alert")}<span>${esc(err.message)}</span></div>`;
  } finally { logState.busy = false; }
}

let logSearchTimer;
["#log-level", "#log-logger"].forEach((sel) => $(sel).addEventListener("change", () => loadLogs(true)));
$("#log-q").addEventListener("input", () => { clearTimeout(logSearchTimer); logSearchTimer = setTimeout(() => loadLogs(true), 300); });
$("#log-wrap").addEventListener("change", (e) => $("#log-view").classList.toggle("nowrap", !e.target.checked));
$("#log-clear").addEventListener("click", () => { $("#log-view").innerHTML = ""; logState.entries = []; logState.day = ""; $("#log-meta").textContent = "Ansicht geleert – neue Einträge erscheinen hier."; });
$("#log-record").addEventListener("change", async (e) => {
  try {
    await api("/api/logs/config", { method: "PUT", body: { level: e.target.value } });
    toast(`Es wird jetzt ab Stufe „${e.target.selectedOptions[0].textContent}“ protokolliert.`, "ok");
    if (e.target.value === "DEBUG") $("#log-level").value = "DEBUG";
    loadLogs(true);
  } catch (err) { toast(err.message, "err"); }
});
document.addEventListener("click", (e) => {
  const job = e.target.closest("[data-log-q]");
  if (!job) return;
  e.preventDefault();
  $("#log-q").value = job.dataset.logQ;
  loadLogs(true);
});

/* ───────── Stacks: gemeinsam sichern und wiederherstellen ───────── */
async function loadStacks() {
  const box = $("#stacks-list");
  if (!appState.stacks) box.innerHTML = '<div class="panel pad muted">Lade …</div>';
  try {
    appState.stacks = await api("/api/stacks");
    renderStacks();
  } catch (err) { box.innerHTML = `<div class="notice">${icon("alert")}<span>${esc(err.message)}</span></div>`; }
}

function stackSourceChips(sources, avail) {
  return sources.map((s) => avail && avail[s.id]
    ? `<span class="chip stk-ok">${esc(s.label)} ✓</span>`
    : `<span class="chip off" title="Nicht vollständig vorhanden">${esc(s.label)} –</span>`).join(" ");
}

function stackCardHtml(s) {
  const chips = s.containers.map((c) => `<span class="chip stk-c" title="${esc(c.image || "")}"><i class="dot ${c.running ? "run" : "stop"}"></i>${esc(c.name)}</span>`).join(" ");
  const last = s.last_run;
  let lastHtml = '<span class="muted">Noch kein gemeinsamer Stack-Backup vorhanden – „Stack sichern“ legt den ersten an.</span>';
  if (last) {
    lastHtml = `Letzter Stack-Backup: <b>${esc(fmtTime(last.time))}</b> <span class="muted">(${esc(ago(new Date(last.time * 1000)))}) · ${last.members.length} Container</span><br>${stackSourceChips(s.sources, last.sources)}`;
    if (last.added_since.length) lastHtml += `<div class="small cv-no">Seit diesem Lauf neu im Stack: ${esc(last.added_since.join(", "))} – nicht enthalten.</div>`;
  }
  const state = !s.present ? '<span class="state failed">nicht mehr vorhanden</span>'
    : `<span class="state ${s.running === s.containers.length ? "running" : s.running ? "queued" : "failed"}">${s.running} von ${s.containers.length} laufen</span>`;
  return `
    <div class="panel pad stk" data-stk-name="${esc(s.name)}">
      <div class="stk-head">
        <div><h2 style="margin:0">${icon("layers")} ${esc(s.name)}</h2>${state}</div>
        <div class="row gap">
          <button class="btn primary" data-stk-backup="${esc(s.name)}" ${s.present ? "" : "disabled"}>${icon("archive")}Stack sichern</button>
          <button class="btn ghost" data-stk-runs="${esc(s.name)}" ${last ? "" : "disabled"}>${icon("restore")}Wiederherstellen …</button>
        </div>
      </div>
      <div class="stk-chips">${chips || '<span class="muted small">keine Container</span>'}</div>
      <div class="stk-last small">${lastHtml}</div>
      <div class="stk-runs" id="stk-runs-${esc(s.name)}" hidden></div>
    </div>`;
}

function renderStacks() {
  const list = appState.stacks || [];
  const open = new Set($$(".stk-runs:not([hidden])").map((e) => e.id));
  $("#stacks-list").innerHTML = list.length ? list.map(stackCardHtml).join("")
    : `<div class="empty">${icon("layers")}<strong>Keine Compose-Stacks gefunden</strong>Container, die mit Docker Compose gestartet wurden, erscheinen hier als Stack.</div>`;
  open.forEach((id) => { const el = $(`#${id}`); if (el) { el.hidden = false; renderStackRuns(el.id.slice(9)); } });
}

async function stackBackup(name) {
  const s = (appState.stacks || []).find((x) => x.name === name);
  if (!s || !s.containers.length) return;
  if (!appState.containersLoaded) await loadContainers();
  openBackupModal(s.containers.map((c) => c.name));
  $("#backup-heading").textContent = `Stack „${name}“ sichern`;
  $("#backup-title").textContent = `${s.containers.length} Container gemeinsam`;
  $("#backup-stackmode").checked = true;
}

async function toggleStackRuns(name) {
  const box = $(`#stk-runs-${CSS.escape(name)}`);
  if (!box.hidden) { box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML = '<span class="muted small">Lade …</span>';
  try {
    const data = await api(`/api/stacks/${encodeURIComponent(name)}/runs`);
    (appState.stackRuns = appState.stackRuns || {})[name] = data;
    renderStackRuns(name);
  } catch (err) { box.innerHTML = `<span class="cv-no small">${esc(err.message)}</span>`; }
}

function renderStackRuns(name) {
  const box = $(`#stk-runs-${CSS.escape(name)}`);
  const data = (appState.stackRuns || {})[name];
  if (!box || !data) return;
  if (!data.runs.length) { box.innerHTML = '<span class="muted small">Keine Stack-Läufe vorhanden.</span>'; return; }
  box.innerHTML = `
    <table class="data">
      <thead><tr><th>Zeitpunkt</th><th>Container</th><th class="right">Wiederherstellen aus</th></tr></thead>
      <tbody>${data.runs.map((r, i) => `
        <tr class="item">
          <td>${esc(fmtTime(r.time))}${i === 0 ? '<span class="badge-new">Neueste</span>' : ""}<br><span class="muted small">${esc(ago(new Date(r.time * 1000)))}</span></td>
          <td>${r.members.map((m) => `<span class="chip ${m.exists ? "" : "off"}" title="${m.exists ? "" : "existiert nicht mehr – wird beim Restore neu angelegt"}">${esc(m.container)}</span>`).join(" ")}
            ${r.added_since.length ? `<div class="small cv-no">nicht enthalten: ${esc(r.added_since.join(", "))}</div>` : ""}</td>
          <td class="right">${data.sources.filter((s) => r.sources[s.id]).map((s) =>
            `<button class="btn ghost sm" data-stk-restore="${esc(name)}|${esc(r.run_id)}|${esc(s.id)}">${icon("restore")}${esc(s.label)}</button>`).join(" ")
            || '<span class="muted small">nirgends vollständig vorhanden</span>'}</td>
        </tr>`).join("")}
      </tbody>
    </table>`;
}

function stackRestore(name, runId, source) {
  const run = ((appState.stackRuns || {})[name]?.runs || []).find((r) => r.run_id === runId);
  if (!run) return;
  const first = run.members[0];
  openRestoreModal(first.container, first.folder, source, first.folder, fmtTime(run.time),
    { stack: run.members.length > 1, title: `Stack ${name} · ${run.members.length} Container` });
}

/* ───────── Backup-Ziele ───────── */
const TARGET_HELP = {
  commvault: "Commvault holt die Archive aus dem Backup-Ordner ab",
  restic: "schreibt verschlüsselt und dedupliziert in ein restic-Repository",
  command: "eigene Befehle/Skripte aus der .env",
  veeam: "stößt den Backup-Job in Veeam Backup & Replication an",
  pbs: "lädt jede Sicherung als Snapshot in einen Proxmox Backup Server",
};
const targetsOf = () => appState.status?.targets || [];                       // alle bekannten Ziele (Einstellungen)
const activeTargets = () => targetsOf().filter((t) => t.enabled);              // nur die in TARGETS aktivierten
const sourceLabel = (id) => (id === "local" ? "Lokal" : targetsOf().find((t) => t.id === id)?.label || id);
const readyTargets = () => activeTargets().filter((t) => t.ready && t.capabilities.includes("trigger"));

function renderTargetPick(el, selected = []) {
  const sel = new Map(selected.map((t) => [t.id, t]));
  const rows = activeTargets().filter((t) => t.capabilities.includes("trigger")).map((t) => {
    const on = sel.has(t.id);
    const level = t.id === "commvault"
      ? `<select data-tlevel="${esc(t.id)}" ${t.ready ? "" : "disabled"} aria-label="Backup-Stufe"><option value="2" ${sel.get(t.id)?.level !== 1 ? "selected" : ""}>Inkrementell</option><option value="1" ${sel.get(t.id)?.level === 1 ? "selected" : ""}>Voll</option></select>` : "";
    return `<label class="check target-row ${t.ready ? "" : "off"}"><input type="checkbox" data-tid="${esc(t.id)}" ${on ? "checked" : ""} ${t.ready ? "" : "disabled"}>
      <span><b>${esc(t.label)}</b> <small class="muted">${t.ready ? esc(TARGET_HELP[t.id] || (t.kind === "push" ? "Helper schreibt ins Ziel" : "Lösung holt die Archive ab")) : "nicht eingerichtet (.env)"}</small></span>${level}</label>`;
  });
  el.innerHTML = rows.join("") || '<span class="muted small">Kein Ziel aktiv – die Sicherungen bleiben lokal (Umgebungsvariable TARGETS).</span>';
}

function readTargetPick(el) {
  return $$("input[data-tid]:checked", el).map((cb) => {
    const lvl = $(`select[data-tlevel="${cb.dataset.tid}"]`, el);
    return lvl ? { id: cb.dataset.tid, level: Number(lvl.value) } : { id: cb.dataset.tid };
  });
}

function targetChips(sc) {
  const runs = sc.targets || (sc.cv_backup_level ? [{ id: "commvault", level: sc.cv_backup_level }] : []);
  return runs.map((r) => `${sourceLabel(r.id)}${r.id === "commvault" ? (r.level === 1 ? " (Voll)" : " (Inkrementell)") : ""}`);
}

const CAP_TEXT = { trigger: "Läufe anstoßen", versions: "Stände auflisten", fetch: "Restore", direct_paths: "Direkt-Pfade", synthetic_full: "Synthetic Full", state_check: "Vermerke prüfen" };
function targetState(t) {
  if (!t.enabled) return t.ready ? ["queued", "eingerichtet, nicht aktiv"] : ["failed", "nicht eingerichtet"];
  return t.ready ? ["finished", "bereit"] : t.configured ? ["queued", "unvollständig"] : ["failed", "nicht eingerichtet"];
}

const TARGET_DOCS = {
  veeam: {
    intro: "Ein Job in Veeam Backup & Replication sichert den Backup-Ordner dieses Hosts. Moby Disk startet den Job per REST-API, wartet auf das Ende der Sitzung und vermerkt die Sicherung als bestätigt.",
    notes: ["Der Job muss den Backup-Ordner sichern (z. B. ein Agent-Job für Linux-Dateien).", "Die REST-API liefert keine Ordnerliste: Ein Vermerk verfällt, wenn kein Restore-Punkt mehr aus der Zeit zwischen Entstehung und lokalem Löschen des Ordners existiert.", "Restore: in Veeam die Dateien in den Backup-Ordner zurückholen – danach erscheint die Sicherung als lokal."],
    env: "TARGETS=…,veeam\nVEEAM_URL=https://vbr.example.com:9419\nVEEAM_USERNAME=svc-mobydisk\nVEEAM_PASSWORD=…\nVEEAM_JOB=Docker-Backups\n# VEEAM_VERIFY_TLS=false\n# VEEAM_API_VERSION=",
  },
  pbs: {
    intro: "Jede Sicherung wird ein Snapshot der Gruppe host/<container> in einem Datastore des Proxmox Backup Servers – deduplizierend und auf Wunsch clientseitig verschlüsselt.",
    notes: ["Der API-Token braucht auf dem Datastore DatastoreBackup und DatastoreReader, für die Aufbewahrung zusätzlich DatastorePrune.", "Bei selbstsigniertem Zertifikat den Fingerabdruck eintragen (PBS: Dashboard → Zertifikate).", "Verschlüsselungsschlüssel (PBS_KEYFILE) sicher aufbewahren – ohne ihn sind die Snapshots nicht lesbar.", "Speicher gibt der Server über seine Garbage Collection frei."],
    env: "TARGETS=…,pbs\nPBS_REPOSITORY=backup@pbs!moby@pbs.example.com:8007:docker\nPBS_PASSWORD=<Geheimnis des API-Tokens>\nPBS_FINGERPRINT=ff:9a:…\n# PBS_NAMESPACE=\n# PBS_KEYFILE=/data/pbs.key\n# PBS_KEEP=\"--keep-last 14 --keep-monthly 6\"",
  },
  restic: {
    intro: "Jede Sicherung wird ein verschlüsselter, deduplizierter Snapshot in einem restic-Repository – lokal, per SFTP, S3/MinIO, Backblaze B2 oder rest-server.",
    notes: ["Das Repository-Passwort sicher aufbewahren – ohne es sind die Snapshots nicht lesbar.", "RESTIC_AUTO_INIT=true legt ein fehlendes Repository beim ersten Lauf an.", "Das Repository regelmäßig mit „restic check“ prüfen."],
    env: "TARGETS=…,restic\nRESTIC_REPOSITORY=s3:https://minio.example.com/backups\nRESTIC_PASSWORD=…\nAWS_ACCESS_KEY_ID=…\nAWS_SECRET_ACCESS_KEY=…\n# RESTIC_AUTO_INIT=true\n# RESTIC_KEEP=\"--keep-daily 14 --keep-monthly 6\"",
  },
  command: {
    intro: "Bindet jede andere Backup-Lösung über eigene Befehle an (Borg, Kopia, rclone, ein eigenes Skript …). Die Befehle laufen ohne Shell, Platzhalter werden als einzelne Argumente ersetzt.",
    notes: ["Die Programme müssen im Container liegen (z. B. ein eingebundener Ordner /scripts).", "COMMAND_STATUS: Exit 0 = fertig, 10 = läuft noch, sonst Fehler.", "Welche Fähigkeiten das Ziel hat, ergibt sich daraus, welche Befehle gesetzt sind."],
    env: "TARGETS=…,command\nCOMMAND_LABEL=Mein Ziel\nCOMMAND_START=/scripts/start.sh\n# COMMAND_STATUS=/scripts/status.sh\n# COMMAND_LIST=/scripts/list.sh {container}\n# COMMAND_FETCH=/scripts/fetch.sh {container} {folder} {dest}",
  },
};

function renderTargetsPanel() {
  const list = targetsOf();
  $("#targets-list").innerHTML = list.map((t) => {
    const [cls, text] = targetState(t);
    return `
    <div class="target-card">
      <div><b>${esc(t.label)}</b> <span class="chip">${t.kind === "push" ? "schreibt selbst" : "wird abgeholt"}</span>
        <span class="state ${cls}">${text}</span>
        <div class="muted small">${esc(t.capabilities.map((c) => CAP_TEXT[c] || c).join(" · "))}</div></div>
      <div class="row gap"><button class="btn ghost sm" data-goto-sec="${esc(t.id)}">Einstellungen</button><button class="btn ghost sm" data-target-test="${esc(t.id)}" data-out="tt-ov-${esc(t.id)}">Verbindung testen</button><span class="small" id="tt-ov-${esc(t.id)}"></span></div>
    </div>`;
  }).join("") || '<span class="muted">Kein Ziel aktiv.</span>';
}

/* ───────── Einstellungen: Unternavigation und Rubriken je Ziel ───────── */
function targetSectionHtml(t) {
  const doc = TARGET_DOCS[t.id] || { intro: "", notes: [], env: "" };
  const [cls, text] = targetState(t);
  const rows = [{ label: "Aktiv", value: t.enabled ? "ja" : "nein – unter „Einrichtung“ einschalten" }, ...(t.info || [])]
    .map((r) => `<dt>${esc(r.label)}</dt><dd>${esc(r.value)}</dd>`).join("");
  return `
    <div class="sec" id="sec-${esc(t.id)}" hidden>
      <div class="sec-head"><h2>${esc(t.label)} <span class="state ${cls}">${text}</span></h2><p class="hint">${esc(doc.intro)}</p></div>
      <div class="panel pad">
        <h2>Verbindung</h2>
        <dl class="kv">${rows || '<dt>Status</dt><dd>keine Angaben</dd>'}</dl>
        <div class="row gap" style="margin-top:16px"><button class="btn primary" data-target-test="${esc(t.id)}" data-out="tt-sec-${esc(t.id)}">Verbindung testen</button><span class="small" id="tt-sec-${esc(t.id)}"></span></div>
        <div class="muted small" style="margin-top:12px">Fähigkeiten: ${esc((t.capabilities || []).map((c) => CAP_TEXT[c] || c).join(" · "))}</div>
      </div>
      ${configPanelHtml(t)}
      <div class="panel pad">
        <h2>Hinweise</h2>
        ${doc.notes.length ? `<ul class="plain-list notes">${doc.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}
        <details class="envalt"><summary>Alternativ per <code>.env</code></summary><pre class="envsnip">${esc(doc.env)}</pre></details>
      </div>
    </div>`;
}

/* ───────── Einrichtung der Ziele im Webinterface ───────── */
const BOOL_WORDS = { 1: "true", true: "true", yes: "true", on: "true", 0: "false", false: "false", no: "false", off: "false" };
function cfgFieldHtml(t, f) {
  const id = `cfg-${t.id}-${f.env}`;
  const src = f.source === "ui" ? '<span class="chip">Webinterface</span>' : f.source === "env" ? '<span class="chip off">.env</span>' : "";
  const orig = f.type === "bool" ? (BOOL_WORDS[String(f.value).toLowerCase()] || "") : f.value;
  const attrs = `id="${id}" data-cfg="${esc(f.env)}" data-orig="${esc(orig)}"`;
  let input;
  if (f.type === "secret") {
    input = `<input type="password" ${attrs} autocomplete="new-password" placeholder="${f.set ? "gesetzt – leer lassen zum Behalten" : ""}">` +
      (f.source === "ui" ? `<label class="small cfg-clear"><input type="checkbox" data-cfg-clear="${esc(f.env)}"> Wert aus dem Webinterface entfernen</label>` : "");
  } else if (f.type === "bool") {
    input = `<select ${attrs}><option value="">(Standard)</option><option value="true"${orig === "true" ? " selected" : ""}>ja</option><option value="false"${orig === "false" ? " selected" : ""}>nein</option></select>`;
  } else if (f.type === "select") {
    input = `<span class="cfg-pick" style="display:flex;gap:8px;align-items:center;width:100%"><select ${attrs} style="flex:1;width:auto;min-width:0">${f.value ? `<option value="${esc(f.value)}" selected>${esc(f.value)}</option>` : '<option value="">– Jobs laden –</option>'}</select>` +
      `<button type="button" class="btn ghost sm" data-cfg-load="${esc(t.id)}:${esc(f.env)}">Jobs laden</button></span>`;
  } else {
    input = `<input type="text" ${attrs} value="${esc(f.value)}" placeholder="${esc(f.placeholder)}" autocomplete="off" spellcheck="false">`;
  }
  return `<label class="field"><span>${esc(f.label)} ${src}</span>${input}${f.help ? `<small class="muted">${esc(f.help)}</small>` : ""}</label>`;
}

function configPanelHtml(t) {
  const fields = (t.config && t.config.fields) || [];
  if (!fields.length) return "";
  return `
      <div class="panel pad" data-cfg-panel="${esc(t.id)}">
        <h2>Einrichtung</h2>
        <p class="hint">Die Werte werden im Datenordner des Containers gespeichert und gelten sofort, ohne Neustart. Zugangsdaten werden hier nie angezeigt. Ein Wert hier hat Vorrang vor der <code>.env</code>; leert man das Feld, gilt wieder die <code>.env</code>.</p>
        <label class="row gap cfg-enable"><input type="checkbox" data-cfg-enabled data-orig="${t.enabled ? 1 : 0}" ${t.enabled ? "checked" : ""}> <span>Dieses Ziel verwenden</span></label>
        ${fields.map((f) => cfgFieldHtml(t, f)).join("")}
        <div class="row gap"><button class="btn primary" data-cfg-save="${esc(t.id)}">Speichern</button><button class="btn ghost" data-cfg-save="${esc(t.id)}" data-test="1">Speichern &amp; testen</button><span class="small" id="cfg-msg-${esc(t.id)}"></span></div>
      </div>`;
}

async function saveTargetConfig(id, thenTest) {
  const panel = $(`[data-cfg-panel="${id}"]`);
  const msg = $(`#cfg-msg-${id}`);
  const values = {};
  $$("[data-cfg]", panel).forEach((el) => {
    const env = el.dataset.cfg;
    const clear = $(`[data-cfg-clear="${env}"]`, panel);
    if (clear && clear.checked) { values[env] = null; return; }
    if (el.type === "password") { if (el.value) values[env] = el.value; return; }
    if (el.value !== el.dataset.orig) values[env] = el.value || null;
  });
  const en = $("[data-cfg-enabled]", panel);
  const body = { values };
  if (en && en.checked !== (en.dataset.orig === "1")) body.enabled = en.checked;
  msg.className = "small";
  msg.textContent = "speichere …";
  try {
    await api(`/api/targets/${id}/config`, { method: "PUT", body });
  } catch (err) { msg.className = "small cv-no"; msg.textContent = err.message; return; }
  appState.sectionsSig = null;
  await loadStatus();
  toast("Gespeichert – gilt sofort.", "ok");
  if (thenTest) testTarget(id, `cfg-msg-${id}`);
}

async function loadChoices(id, env) {
  const sel = $(`#cfg-${id}-${env}`);
  const msg = $(`#cfg-msg-${id}`);
  msg.className = "small";
  msg.textContent = "lade …";
  try {
    const r = await api(`/api/targets/${id}/choices/${env}`);
    const cur = sel.value;
    sel.innerHTML = '<option value="">– auswählen –</option>' + r.choices.map((c) => `<option value="${esc(c.value)}">${esc(c.label)}</option>`).join("");
    if (cur && !r.choices.some((c) => c.value === cur)) sel.insertAdjacentHTML("beforeend", `<option value="${esc(cur)}">${esc(cur)} (nicht gefunden)</option>`);
    sel.value = cur;
    msg.className = "small cv-ok";
    msg.textContent = `${r.choices.length} gefunden – auswählen und speichern`;
  } catch (err) { msg.className = "small cv-no"; msg.textContent = err.message; }
}

function renderSettingsSections() {
  const list = targetsOf();
  const sig = JSON.stringify(list);
  if (appState.sectionsSig === sig) return;
  const focused = document.activeElement;
  if (focused && focused.closest && focused.closest("[data-cfg-panel]")) return;   // nicht unter den Händen neu zeichnen
  appState.sectionsSig = sig;
  const cv = list.find((t) => t.id === "commvault");
  $("#cv-config-slot").innerHTML = cv ? configPanelHtml(cv) : "";
  $("#sec-targets-generated").innerHTML = list.filter((t) => t.id !== "commvault").map(targetSectionHtml).join("");
  const dot = (t) => `<i class="dot ${t.ready && t.enabled ? "ok" : t.ready ? "warn" : "bad"}"></i>`;
  const item = (id, label, t) => `<button data-sec="${esc(id)}" class="${appState.settingsSec === id ? "on" : ""}">${t ? dot(t) : ""}<span>${esc(label)}</span></button>`;
  $("#settings-nav").innerHTML = `
    <span class="nav-group">Allgemein</span>
    ${item("notify", "Benachrichtigungen")}${item("scope", "Backup-Umfang")}
    <span class="nav-group">Backup-Ziele</span>
    ${item("targets", "Übersicht")}
    ${list.map((t) => item(t.id, t.label, t)).join("")}`;
  showSettingsSection(appState.settingsSec);
}

function showSettingsSection(id) {
  const valid = $$("#tab-settings .sec").map((e) => e.id.slice(4));
  if (!valid.includes(id)) id = valid.includes("targets") ? "targets" : valid[0];
  appState.settingsSec = id;
  store.set("settings-sec", id);
  $$("#tab-settings .sec").forEach((e) => { e.hidden = e.id !== `sec-${id}`; });
  $$("#settings-nav button").forEach((b) => b.classList.toggle("on", b.dataset.sec === id));
}

async function testTarget(id, outId) {
  const out = $(`#${outId || `tt-ov-${id}`}`);
  out.className = "small";
  out.textContent = "prüfe …";
  try {
    const r = await api(`/api/targets/${id}/test`, { method: "POST" });
    out.className = "small cv-ok";
    out.textContent = "✓ " + Object.entries(r).filter(([k]) => k !== "ok" && !/token|guid/i.test(k)).map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(", ") : v}`).join(" · ");
  } catch (err) { out.className = "small cv-no"; out.textContent = err.message; }
}

function renderVersionTable(kind) {
  const rows = appState.versions[kind] || [];
  const target = $(`#versions-list-${kind}`);
  const label = sourceLabel(kind);
  if (!rows.length) {
    target.innerHTML = `<div class="empty">${icon(kind === "local" ? "db" : "archive")}<strong>${kind === "local" ? "Keine lokalen Kopien" : `Keine Versionen in ${esc(label)}`}</strong>${kind === "local" ? "Es liegen noch keine lokalen Sicherungen vor." : `Sobald ein Backup an ${esc(label)} übergeben wurde, erscheint es hier.`}</div>`;
    return;
  }
  const { key, dir } = appState.vSort[kind] || (appState.vSort[kind] = { key: "time", dir: "desc" });
  const sign = dir === "desc" ? -1 : 1;
  const sorted = rows.slice().sort((a, b) => sign * cmp(a[key], b[key]) || cmp(b.time, a.time));
  const newest = rows.reduce((m, r) => Math.max(m, r.time || 0), 0);
  const th = (k, text, cls = "") =>
    `<th class="sortable ${cls} ${key === k ? "sorted" : ""}" data-vsort="${kind}:${k}">${text}${sortIcon()}</th>`;
  const third = kind === "local" ? th("mode", "Modus") : th("job", "Kennung");
  target.innerHTML = `
    <table class="data">
      <thead><tr>${th("time", "Zeitpunkt")}${th("size", "Größe")}${third}<th></th></tr></thead>
      <tbody>${sorted.map((v) => `
        <tr class="item">
          <td>${esc(v.timeLabel)}${v.time === newest ? '<span class="badge-new">Neueste</span>' : ""}<br><span class="muted small">${esc(v.ago)}${v.deleted ? " · lokal gelöscht" : ""}${v.skipped ? ` · <span class="cv-no" title="Mounts, die nicht gesichert wurden (Netzwerkspeicher, Limit, Regeln)">${v.skipped} Mounts übersprungen</span>` : ""}${(v.dumps || []).length ? ` · ${esc(v.dumps.map((t) => DUMP_LABEL[t] || t).join(", "))}` : ""}${v.stack ? ` · Stack ${esc(v.stack)}` : ""}${Object.entries(v.inTargets || {}).map(([id, on]) => on ? ` · <span class="cv-ok">in ${esc(sourceLabel(id))} ✓</span>` : ` · <span class="cv-no" title="Noch nicht dort gefunden – wird bei aktiver Prüfung nicht gelöscht">nicht in ${esc(sourceLabel(id))}</span>`).join("")}</span></td>
          <td>${esc(fmtSize(v.size))}</td>
          <td>${esc(kind === "local" ? (v.mode || "-") : (v.job ?? "-"))}</td>
          <td class="right"><button class="btn ghost sm" data-restore="${esc(v.ref)}" data-folder="${esc(v.folder || "")}" data-time="${esc(v.timeLabel)}" data-kind="${esc(kind)}">${icon("restore")}Wiederherstellen</button></td>
        </tr>`).join("")}
      </tbody>
    </table>`;
}

function setVersionTab(which) {
  $$("#v-tabs button").forEach((b) => b.classList.toggle("on", b.dataset.v === which));
  $$("#versions-lists > div").forEach((d) => { d.hidden = d.id !== `versions-list-${which}`; });
}

async function openVersionsModal(name) {
  appState.versionsFor = name;
  $("#versions-title").textContent = name;
  const sources = [...activeTargets().filter((t) => t.capabilities.includes("versions")).map((t) => t.id), "local"];
  $("#v-tabs").innerHTML = sources.map((id) => `<button data-v="${esc(id)}">${esc(id === "local" ? "Lokal auf der Platte" : "In " + sourceLabel(id))} <i class="count-pill" id="v-count-${esc(id)}">–</i></button>`).join("");
  $("#versions-lists").innerHTML = sources.map((id) => `<div id="versions-list-${esc(id)}" class="vlist">${id === "local" ? "" : "Lade …"}</div>`).join("");
  appState.versions = {};
  setVersionTab(sources[0]);
  openModal("modal-versions");
  try {
    const data = await api(`/api/containers/${encodeURIComponent(name)}/versions`);
    const have = {};   // Ziel → Ordner, nur für erreichbare Ziele
    for (const id of sources.filter((x) => x !== "local")) {
      const entry = (data.targets || {})[id] || {};
      if (entry.error) {
        $(`#versions-list-${id}`).innerHTML = `<div class="notice error" style="margin:14px">${icon("alert")}<span>${esc(entry.error)}</span></div>`;
        $(`#v-count-${id}`).textContent = "!";
        continue;
      }
      appState.versions[id] = (entry.folders || []).map((v) => {
        const d = parseLocalTs(v.folder);
        const t = d ? d.getTime() / 1000 : (v.backup_time || 0);
        return { ref: v.backup_time, folder: v.folder, time: t, size: v.size ?? 0, job: v.backup_job_id ?? "-",
          timeLabel: fmtTime(t), ago: ago(new Date(t * 1000)), deleted: !!v.deleted_locally };
      });
      have[id] = new Set(appState.versions[id].map((v) => v.folder));
      $(`#v-count-${id}`).textContent = appState.versions[id].length;
      renderVersionTable(id);
    }
    appState.versions.local = (data.local || []).map((v) => {
      const d = parseLocalTs(v.ts);
      return {
        ref: v.ts, folder: v.ts, time: d ? d.getTime() / 1000 : 0, size: v.size_bytes ?? 0, mode: v.mode || "",
        dumps: v.dumps || [], stack: v.stack || "", skipped: v.skipped || 0,
        inTargets: Object.fromEntries(Object.entries(have).map(([id, set]) => [id, set.has(v.ts)])),
        timeLabel: d ? fmtDate(d) : v.ts, ago: d ? ago(d) : "",
      };
    });
    $("#v-count-local").textContent = appState.versions.local.length;
    renderVersionTable("local");
    const first = sources.find((id) => id !== "local" && (appState.versions[id] || []).length);
    setVersionTab(first || (appState.versions.local.length ? "local" : sources[0]));
  } catch (err) {
    $(`#versions-list-${sources[0]}`).innerHTML = `<div class="notice error" style="margin:14px">${icon("alert")}<span>${esc(err.message)}</span></div>`;
  }
}

function setRestoreTarget(which) {
  setSeg("#restore-target", which);
  $("#restore-original-opts").hidden = which !== "original";
  $("#restore-clone-opts").hidden = which !== "clone";
  $("#restore-submit").textContent = which === "clone" ? "Klon anlegen" : "Wiederherstellung starten";
  $("#restore-submit").classList.toggle("danger", which !== "clone");
  $("#restore-submit").classList.toggle("solid", which !== "clone");
  $("#restore-submit").classList.toggle("primary", which === "clone");
}

async function openRestoreModal(container, version, kind, folder = "", timeLabel = "", opts = {}) {
  const fold = folder || (kind === "local" ? version : "");
  appState.restoreTarget = { container, version, kind, folder: fold, info: null, preferStack: !!opts.stack };
  $("#restore-heading").textContent = opts.stack ? "Stack wiederherstellen" : "Wiederherstellen";
  $("#restore-title").textContent = opts.title || container;
  $("#restore-version-hint").textContent = kind !== "local"
    ? `Sicherung vom ${timeLabel || fmtTime(version)} (aus ${sourceLabel(kind)})`
    : `Lokale Kopie ${version}`;
  $("#restore-stack-wrap").hidden = true;
  $("#restore-dump-wrap").hidden = true;
  $("#restore-direct").hidden = true;
  $("#restore-stack").checked = false;
  $("#restore-dump").checked = false;
  $("#clone-name").value = `${container}-clone`;
  $("#clone-port").value = "";
  $("#clone-net").value = "none";
  $("#clone-attach").checked = true;
  $("#clone-start").checked = true;
  $("#clone-port-hint").textContent = "(optional, Host-Port)";
  setRestoreTarget("original");
  openModal("modal-restore");
  if (!fold) return;
  try {
    const info = await api(`/api/containers/${encodeURIComponent(container)}/backup-info?folder=${encodeURIComponent(fold)}&kind=${kind}`);
    if (appState.restoreTarget?.folder !== fold) return;
    appState.restoreTarget.info = info;
    if (info.members && info.members.length > 1) {
      $("#restore-stack-wrap").hidden = false;
      $("#restore-stack-name").textContent = info.stack;
      const missing = info.members.filter((m) => !m.local && !m.in_cv);
      $("#restore-stack-members").textContent = `(${info.members.map((m) => m.container).join(", ")})`
        + (missing.length ? ` – fehlt: ${missing.map((m) => m.container).join(", ")}` : "");
      $("#restore-stack").disabled = missing.length > 0;
      if (appState.restoreTarget.preferStack && !missing.length) $("#restore-stack").checked = true;   // aus der Stack-Ansicht vorbelegt
    }
    if ((info.cv_direct || []).length) {
      const list = info.cv_direct.map((d) => d.rel === "." ? d.host_path : `${d.host_path} (${d.rel}/ in ${d.destination})`);
      $("#restore-direct-text").textContent = `Direkt über Commvault gesichert und hier nicht enthalten: ${list.join(", ")}. ` +
        "Diese Daten bleiben beim Restore unverändert (beim Klon fehlen sie). Einen älteren Stand holst du in Commvault per Restore dieses Pfads zurück.";
      $("#restore-direct").hidden = false;
    }
    if (info.dumps && info.dumps.length) {
      $("#restore-dump-wrap").hidden = false;
      $("#restore-dump").checked = info.mode === "live";
      $("#restore-dump-hint").textContent = info.mode === "live"
        ? `(${info.dumps.map((t) => DUMP_LABEL[t] || t).join(", ")} – empfohlen: die Dateikopie entstand im Live-Modus)`
        : `(${info.dumps.map((t) => DUMP_LABEL[t] || t).join(", ")} – optional: die Dateikopie entstand gestoppt und ist konsistent)`;
    }
    if (info.ports && info.ports.length) $("#clone-port-hint").textContent = `(optional, Host-Port für ${info.ports[0]})`;
  } catch (_e) { /* Zusatzinfos sind optional */ }
}

async function submitBackup() {
  const targets = appState.backupTargets || [$("#backup-title").textContent];
  const name = targets.length > 1 ? `${targets.length} Container` : targets[0];
  const payload = {
    container: targets[0],
    containers: targets.length > 1 ? targets : undefined,
    mode: $("#backup-mode").value,
    image_mode: $("#backup-imagemode").value,
    whole_stacks: !$("#backup-stack-wrap").hidden && $("#backup-wholestacks").checked,
    stack_mode: $("#backup-stackmode").checked,
    retention: parseInt($("#backup-retention").value || "0", 10),
    retention_days: parseInt($("#backup-retdays").value || "0", 10),
    format: $("#backup-format").value,
    require_cv: $("#backup-requirecv").checked,
    targets: readTargetPick($("#backup-targets-pick")),
  };
  $("#backup-submit").disabled = true;
  try {
    const job = await api("/api/backup", { method: "POST", body: payload });
    toast(`Backup für '${name}' gestartet (Job ${job.id})`, "ok");
    if (targets.length > 1) appState.selected.clear();
    closeModal("modal-backup");
    switchTab("jobs");
  } catch (err) {
    toast(err.message, "err");
  } finally {
    $("#backup-submit").disabled = false;
  }
}

async function submitRestore() {
  const target = appState.restoreTarget;
  if (!target) return;
  const asClone = $("#restore-target button.on")?.dataset.v === "clone";
  const payload = {
    container: target.container,
    version: target.version,
    folder: target.folder || null,
    restore_bind_mounts: $("#restore-binds").checked,
    restart_container: $("#restore-restart").checked,
    recreate_if_missing: $("#restore-recreate").checked,
    source: target.kind,
    use_dump: !$("#restore-dump-wrap").hidden && $("#restore-dump").checked,
    whole_stack: !$("#restore-stack-wrap").hidden && $("#restore-stack").checked,
    target: asClone ? "clone" : "original",
    clone: asClone ? {
      name: $("#clone-name").value.trim(), network: $("#clone-net").value, host_port: $("#clone-port").value || null,
      attach_skipped: $("#clone-attach").checked, start: $("#clone-start").checked,
    } : undefined,
  };
  if (!asClone && payload.whole_stack && !confirm(`Alle Container des Stacks '${target.info?.stack}' werden gestoppt und auf diesen Lauf zurückgesetzt. Fortfahren?`)) return;
  $("#restore-submit").disabled = true;
  try {
    const job = await api("/api/restore", { method: "POST", body: payload });
    toast(asClone ? `Klon '${payload.clone.name}' wird angelegt (Job ${job.id})` : `Restore für '${target.container}' gestartet (Job ${job.id})`, "ok");
    closeModal("modal-restore");
    closeModal("modal-versions");
    switchTab("jobs");
  } catch (err) {
    toast(err.message, "err");
  } finally {
    $("#restore-submit").disabled = false;
  }
}

/* ───────── Jobs ───────── */
const JOB_LABEL = { queued: "In Warteschlange", running: "Läuft", paused: "Pausiert", finished: "Abgeschlossen", failed: "Fehlgeschlagen", cancelled: "Abgebrochen" };
const JOB_STATE_CLASS = { paused: "queued", cancelled: "cancelled" };   // Farbe der Zustandsmarke
const jobActive = (j) => j.status === "queued" || j.status === "running" || j.status === "paused";

function jobActionsHtml(job) {
  if (!jobActive(job)) return "";
  const id = esc(job.id);
  if (job.locked) return '<span class="job-meta" title="Daten werden gerade zurückgeschrieben – das lässt sich nicht gefahrlos unterbrechen">nicht mehr unterbrechbar</span>';
  if (job.control === "cancel") return '<span class="job-meta">wird abgebrochen …</span>';
  const abort = `<button class="btn ghost sm danger" data-job-act="cancel" data-id="${id}" title="Auftrag abbrechen">Abbrechen</button>`;
  if (job.status === "paused") return `<button class="btn ghost sm" data-job-act="resume" data-id="${id}">Fortsetzen</button>${abort}`;
  if (job.control === "pause") return `<span class="job-meta">Pause angefordert …</span><button class="btn ghost sm" data-job-act="resume" data-id="${id}" title="Pause zurücknehmen">Doch weiter</button>${abort}`;
  return `<button class="btn ghost sm" data-job-act="pause" data-id="${id}" title="Hält den Auftrag an der nächsten sicheren Stelle an">Pausieren</button>${abort}`;
}

function jobDuration(job) {
  return (job.finished || Date.now() / 1000) - job.started;
}

function jobMatches(job, f) {
  if (f.status === "active" && !jobActive(job)) return false;
  if (f.status === "finished" && job.status !== "finished") return false;
  if (f.status === "failed" && job.status !== "failed") return false;
  if (f.status === "cancelled" && job.status !== "cancelled") return false;
  if (f.kind !== "all" && job.kind !== f.kind) return false;
  if (f.q) {
    const hay = [job.params?.container, ...(job.params?.containers || []), job.kind, job.message, job.error, job.id,
      ...(job.log || []).map((l) => l.message)].join(" ").toLowerCase();
    return f.q.toLowerCase().split(/\s+/).filter(Boolean).every((t) => hay.includes(t));
  }
  return true;
}

function renderJobs() {
  const f = appState.jFilter;
  const all = appState.jobs;
  const active = all.filter(jobActive).length;
  const pill = $("#jobs-active-pill");
  pill.hidden = !active;
  pill.textContent = active;

  let list = all.filter((j) => jobMatches(j, f));
  const sorters = {
    new: (a, b) => b.started - a.started,
    old: (a, b) => a.started - b.started,
    name: (a, b) => cmp(a.params?.container, b.params?.container) || b.started - a.started,
    duration: (a, b) => jobDuration(b) - jobDuration(a),
  };
  list = list.slice().sort(sorters[f.sort] || sorters.new);

  const wrap = $("#job-list");
  // Scrollposition der Protokolle (und der Seite) merken, damit das Neuzeichnen nichts nach oben springen lässt
  const logState = new Map();
  $$(".job-log[data-log]", wrap).forEach((el) => {
    logState.set(el.dataset.log, { top: el.scrollTop, atBottom: el.scrollHeight - el.scrollTop - el.clientHeight < 24 });
  });
  const pageY = window.scrollY;
  if (!all.length) {
    wrap.innerHTML = `<div class="empty">${icon("clock")}<strong>Noch keine Aufträge</strong>Gestartete Backups und Restores erscheinen hier.</div>`;
  } else if (!list.length) {
    wrap.innerHTML = `<div class="empty">${icon("search")}<strong>Keine Treffer</strong>Passe Suche oder Filter an.</div>`;
  } else {
    const q = f.q.trim();
    wrap.innerHTML = list.map((job) => {
      const log = (job.log || []).map((l) => `[${new Date(l.time * 1000).toLocaleTimeString("de-DE")}] ${l.message}`).join("\n");
      const isRestore = job.kind === "restore";
      const started = new Date(job.started * 1000);
      const batch = job.params?.containers && job.params.containers.length > 1;
      const subject = batch ? `${job.params.containers.length} Container` : (job.params?.container || "");
      const running = job.status === "queued" || job.status === "running";
      return `
      <div class="job ${esc(job.status)}">
        <div class="job-head">
          <span class="job-kind ${isRestore ? "restore" : ""}">${icon(isRestore ? "restore" : job.kind === "cvsync" ? "refresh" : "archive")}</span>
          <div>
            <div class="job-title">${job.kind === "cvsync" ? esc(job.params?.container || "Abgleich") : isRestore ? (job.params?.target === "clone" ? `Klon: ${hl(subject, q)} → ${esc(job.params.clone?.name || "")}` : job.params?.members ? `Stack-Restore: ${hl(subject, q)} (+${job.params.members.length - 1})` : `Restore: ${hl(subject, q)}`) : `Backup: ${hl(subject, q)}`}</div>
            ${job.params?.schedule_name ? `<div class="job-meta">${icon("calendar")} Zeitplan: ${hl(job.params.schedule_name, q)}${job.params.manual ? " (manuell gestartet)" : ""}</div>` : ""}
            ${batch ? `<div class="job-meta" title="${esc(job.params.containers.join(", "))}">${esc(job.params.containers.slice(0, 5).join(", "))}${job.params.containers.length > 5 ? ` … +${job.params.containers.length - 5}` : ""}</div>` : ""}
            <div class="job-meta" title="${esc(fmtDate(started))}">${esc(ago(started))} · ${esc(fmtDuration(jobDuration(job)))}${running ? " (läuft)" : ""} · ${esc(job.id)}</div>
          </div>
          <span class="state ${esc(JOB_STATE_CLASS[job.status] || job.status)}">${esc(JOB_LABEL[job.status] || job.status)}</span>
          <span class="spacer"></span>
          <span class="job-meta">${hl(job.message || "", q)}</span>
          <b>${esc(Math.round(job.progress))}%</b>
          <span class="job-actions">${jobActionsHtml(job)}</span>
        </div>
        <div class="progress"><i style="width:${Math.max(0, Math.min(100, job.progress))}%"></i></div>
        ${job.error ? `<div class="notice error" style="margin:10px 0 0">${icon("alert")}<span>${esc(job.error)}</span></div>` : ""}
        ${log ? `<details data-job="${esc(job.id)}" ${appState.openLogs.has(job.id) ? "open" : ""}><summary>Protokoll (${job.log.length} Einträge)</summary><div class="job-log" data-log="${esc(job.id)}">${esc(log)}</div></details>` : ""}
      </div>`;
    }).join("");
  }
  $$(".job-log[data-log]", wrap).forEach((el) => {
    const st = logState.get(el.dataset.log);
    el.scrollTop = !st || st.atBottom ? el.scrollHeight : st.top;   // am Ende mitlesen, sonst Position halten
  });
  if (window.scrollY !== pageY) window.scrollTo(0, pageY);
  const filtered = list.length !== all.length;
  $("#j-count").textContent = all.length ? `${list.length}${filtered ? ` von ${all.length}` : ""} Aufträge` : "";
}

async function loadJobs(manual = false) {
  const btn = $("#refresh-jobs");
  if (manual) btn.classList.add("spin");
  try {
    appState.jobs = await api("/api/jobs");
    renderJobs();
  } catch (err) {
    toast(err.message, "err");
  } finally {
    btn.classList.remove("spin");
  }
}

/* ───────── Zeitpläne ───────── */
const DAY_NAMES = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];

function daysLabel(days) {
  const d = days.slice().sort();
  if (d.length === 7) return "Täglich";
  if (d.join() === "0,1,2,3,4") return "Mo–Fr";
  if (d.join() === "5,6") return "Sa, So";
  return d.map((i) => DAY_NAMES[i]).join(", ");
}

function scheduleStatus(sc) {
  const map = { ok: ["finished", "Erfolgreich"], failed: ["failed", "Fehlgeschlagen"], running: ["running", "Läuft"],
    skipped: ["queued", "Übersprungen"], missed: ["queued", "Verpasst"], unknown: ["queued", "Unbekannt"] };
  return map[sc.last_status] || null;
}

async function loadSchedules(manual = false) {
  try {
    appState.schedules = await api("/api/schedules");
    renderSchedules();
  } catch (err) {
    if (manual) toast(err.message, "err");
  }
}

function renderSchedules() {
  const wrap = $("#sched-list");
  const list = appState.schedules;
  if (!list.length) {
    wrap.innerHTML = `<div class="empty" style="background:var(--panel);border-radius:6px">${icon("calendar")}<strong>Noch keine Zeitpläne</strong>Lege einen Zeitplan an, um Container automatisch zu sichern.<br><button class="btn primary" id="sched-new-empty" style="margin-top:14px">${icon("plus")}Neuer Zeitplan</button></div>`;
    return;
  }
  wrap.innerHTML = list.map((sc) => {
    const st = scheduleStatus(sc);
    const target = sc.scope === "all_with_data" ? "Alle Container mit Daten"
      : `${sc.containers.length} Container: ${sc.containers.slice(0, 3).join(", ")}${sc.containers.length > 3 ? " …" : ""}`;
    const cv = targetChips(sc).join(" + ") || null;
    const ret = [sc.retention ? `letzte ${sc.retention}` : "alle", sc.retention_days ? `max. ${sc.retention_days} Tage` : ""].filter(Boolean).join(" · ");
    const cls = !sc.enabled ? "off" : sc.last_status === "failed" ? "last-failed" : sc.last_status === "ok" ? "last-ok" : "";
    const next = sc.enabled && sc.next_run ? fmtDate(new Date(sc.next_run * 1000)) : "–";
    const last = sc.last_run ? `${ago(new Date(sc.last_run * 1000))}` : "noch nie";
    return `
    <div class="sched ${cls}" data-sid="${esc(sc.id)}">
      <div>
        <h3>${esc(sc.name)}${sc.enabled ? "" : '<span class="chip off">pausiert</span>'}</h3>
        <div class="when">${esc(daysLabel(sc.days))} um ${esc(sc.time)} Uhr</div>
        <div class="facts">
          <span class="chip">${icon("box")}${esc(target)}</span>
          <span class="chip ${sc.mode === "stop" ? "vol" : ""}">${sc.mode === "stop" ? "Konsistent (stop)" : "Live"}</span>
          <span class="chip">${icon("archive")}${esc(ret)}</span>
          ${cv ? `<span class="chip cv" title="Ziele nach dem Lauf">${icon("link")}${esc(cv)}</span>` : ""}
          ${(sc.image_mode || (sc.include_image ? "own" : "none")) !== "none" ? `<span class="chip">${sc.image_mode === "all" ? "+ alle Images" : "+ eigene Images"}</span>` : ""}
          ${sc.stack_mode !== false ? '<span class="chip" title="Container eines Compose-Stacks werden gemeinsam gestoppt">Stacks gemeinsam</span>' : ""}
          <span class="chip ${sc.format === "tar" ? "cv" : ""}" title="${sc.format === 'tar' ? 'unkomprimiert: Commvault kann deduplizieren' : 'gzip: spart lokal Platz, schlechtere Deduplizierung'}">${esc(sc.format || "tar.gz")}</span>
          ${sc.require_cv && (sc.retention || sc.retention_days) ? '<span class="chip cv" title="Lokale Kopien werden erst nach Bestätigung in Commvault gelöscht">Löschen nur nach CV ✓</span>' : ""}
        </div>
      </div>
      <div class="meta">
        <div>Nächster Lauf: <b>${esc(next)}</b></div>
        <div>Letzter Lauf: <b>${esc(last)}</b> ${st ? `<span class="state ${st[0]}" style="margin-left:6px">${st[1]}</span>` : ""}</div>
        ${sc.last_message ? `<div title="${esc(sc.last_message)}">${esc(sc.last_message.length > 110 ? sc.last_message.slice(0, 110) + "…" : sc.last_message)}</div>` : ""}
      </div>
      <div class="acts">
        <button class="btn ghost sm" data-sched-run="${esc(sc.id)}">${icon("play")}Jetzt ausführen</button>
        <button class="icon-btn" data-sched-toggle="${esc(sc.id)}" title="${sc.enabled ? "Pausieren" : "Aktivieren"}" aria-label="${sc.enabled ? "Pausieren" : "Aktivieren"}">${icon(sc.enabled ? "clock" : "check")}</button>
        <button class="icon-btn" data-sched-edit="${esc(sc.id)}" title="Bearbeiten" aria-label="Bearbeiten">${icon("edit")}</button>
        <button class="icon-btn del" data-sched-del="${esc(sc.id)}" title="Löschen" aria-label="Löschen">${icon("trash")}</button>
      </div>
    </div>`;
  }).join("");
}

function renderScheduleContainerPick(selected) {
  const q = ($("#sf-filter").value || "").toLowerCase();
  const list = appState.containers.filter((c) => !q || c.name.toLowerCase().includes(q));
  $("#sf-containers").innerHTML = list.length ? list.map((c) => `
    <label><input type="checkbox" value="${esc(c.name)}" ${selected.has(c.name) ? "checked" : ""}>
      <span>${esc(c.name)}${c.is_self ? '<span class="badge-self">Moby Disk</span>' : ""}</span><small>${c.has_data ? "mit Daten" : "ohne Daten"}</small></label>`).join("")
    : '<div class="empty" style="padding:20px">Keine Container</div>';
  updatePickAll(list);
}

function updatePickAll(list = null) {
  const q = ($("#sf-filter").value || "").toLowerCase();
  const visible = (list || appState.containers.filter((c) => !q || c.name.toLowerCase().includes(q))).map((c) => c.name);
  const sel = appState.pickSelected || new Set();
  const n = visible.filter((name) => sel.has(name)).length;
  const all = $("#sf-all");
  all.checked = visible.length > 0 && n === visible.length;
  all.indeterminate = n > 0 && n < visible.length;
  all.disabled = visible.length === 0;
  $("#sf-all-count").textContent = `${sel.size} von ${appState.containers.length} gewählt`;
}

function setScope(scope) {
  $$("#sf-scope button").forEach((b) => b.classList.toggle("on", b.dataset.v === scope));
  $("#sf-containers-wrap").hidden = scope !== "selected";
  $("#sf-scope-hint").hidden = scope !== "all_with_data";
}

function currentScope() {
  return $("#sf-scope button.on")?.dataset.v || "all_with_data";
}

async function openScheduleModal(sc = null) {
  appState.editingSchedule = sc ? sc.id : null;
  if (!appState.containers.length) { try { appState.containers = await api("/api/containers"); } catch (_e) { /* ignore */ } }
  $("#sched-heading").textContent = sc ? "Zeitplan bearbeiten" : "Neuer Zeitplan";
  $("#sf-name").value = sc?.name || "";
  const days = new Set(sc ? sc.days : [0, 1, 2, 3, 4, 5, 6]);
  $$("#sf-days input").forEach((i) => { i.checked = days.has(Number(i.value)); });
  $("#sf-time").value = sc?.time || "02:00";
  $("#sf-mode").value = sc?.mode || "stop";
  renderTargetPick($("#sf-targets-pick"), sc ? (sc.targets || (sc.cv_backup_level ? [{ id: "commvault", level: sc.cv_backup_level }] : [])) : []);
  $("#sf-format").value = sc?.format || "tar";
  $("#sf-requirecv").checked = sc ? !!sc.require_cv : !!(appState.status?.cv_configured);
  $("#sf-ret").value = sc ? sc.retention : 7;
  $("#sf-retdays").value = sc ? sc.retention_days : 0;
  $("#sf-imagemode").value = sc ? (sc.image_mode || (sc.include_image ? "own" : "none")) : "own";
  $("#sf-stackmode").checked = sc ? sc.stack_mode !== false : true;
  $("#sf-enabled").checked = sc ? !!sc.enabled : true;
  $("#sf-filter").value = "";
  setScope(sc?.scope || "all_with_data");
  appState.pickSelected = new Set(sc?.containers || []);
  renderScheduleContainerPick(appState.pickSelected);
  openModal("modal-schedule");
  setTimeout(() => $("#sf-name").focus(), 50);
}

async function saveSchedule() {
  const scope = currentScope();
  const body = {
    name: $("#sf-name").value.trim(),
    enabled: $("#sf-enabled").checked,
    scope,
    containers: scope === "selected" ? Array.from(appState.pickSelected || []) : [],
    days: $$("#sf-days input:checked").map((i) => Number(i.value)),
    time: $("#sf-time").value,
    mode: $("#sf-mode").value,
    image_mode: $("#sf-imagemode").value,
    stack_mode: $("#sf-stackmode").checked,
    retention: parseInt($("#sf-ret").value || "0", 10),
    retention_days: parseInt($("#sf-retdays").value || "0", 10),
    targets: readTargetPick($("#sf-targets-pick")),
    format: $("#sf-format").value,
    require_cv: $("#sf-requirecv").checked,
  };
  $("#sf-save").disabled = true;
  try {
    const id = appState.editingSchedule;
    await api(id ? `/api/schedules/${id}` : "/api/schedules", { method: id ? "PUT" : "POST", body });
    toast(id ? "Zeitplan gespeichert" : "Zeitplan angelegt", "ok");
    closeModal("modal-schedule");
    loadSchedules();
  } catch (err) {
    toast(err.message, "err");
  } finally {
    $("#sf-save").disabled = false;
  }
}

async function scheduleAction(kind, id) {
  const sc = appState.schedules.find((x) => x.id === id);
  if (!sc) return;
  try {
    if (kind === "run") {
      const job = await api(`/api/schedules/${id}/run`, { method: "POST" });
      toast(`'${sc.name}' gestartet (Job ${job.id})`, "ok");
      switchTab("jobs");
    } else if (kind === "toggle") {
      await api(`/api/schedules/${id}`, { method: "PUT", body: { enabled: !sc.enabled } });
      toast(sc.enabled ? "Zeitplan pausiert" : "Zeitplan aktiviert", "ok");
      loadSchedules();
    } else if (kind === "del") {
      if (!confirm(`Zeitplan '${sc.name}' wirklich löschen?`)) return;
      await api(`/api/schedules/${id}`, { method: "DELETE" });
      toast("Zeitplan gelöscht", "ok");
      loadSchedules();
    } else if (kind === "edit") {
      openScheduleModal(sc);
    }
  } catch (err) {
    toast(err.message, "err");
  }
}

/* ───────── Backup-Umfang pro Container ───────── */
function planHtml(name) {
  const entry = appState.plans[name];
  if (!entry) return '<span class="muted">Lade Backup-Umfang …</span>';
  const d = entry.data;
  const rows = d.mounts.map((m) => {
    const take = m.action === "backup";
    const size = take && m.size !== null ? ` <span class="muted small">${m.size_complete ? "" : "> "}${esc(fmtSize(m.size))}</span>` : "";
    const fs = m.fstype && m.fstype !== "overlay" ? `<span class="chip">${esc(m.fstype)}</span>` : "";
    const locked = ["backup_root", "volumes_root", "unreachable", "system", "missing", "special", "cv_direct"].includes(m.reason)
      || (m.reason === "host_share" && d.is_self);
    const direct = m.reason === "cv_direct";
    // Commvault liest direkt auf dem Host – auch Pfade, die der Helper nicht sieht oder bewusst überspringt
    const cvAllowed = !d.is_self && m.source && !["backup_root", "volumes_root", "system", "missing", "special", "cv_direct"].includes(m.reason);
    const btns = [];
    if (direct) btns.push(`<button class="btn ghost sm" data-cvd="remove" data-path="${esc(m.source)}" title="Wieder vom Helper sichern lassen (Pfad aus dem Commvault-Subclient entfernen)">Zurück zum Helper</button>`);
    if (!locked) {
      if (take && m.reason !== "included") btns.push(`<button class="btn ghost sm" data-rule="skip" data-name="${esc(name)}" data-dest="${esc(m.destination)}">Ausschließen</button>`);
      else if (m.reason === "manual" || m.reason === "included") btns.push(`<button class="btn ghost sm" data-rule="clear" data-name="${esc(name)}" data-dest="${esc(m.destination)}">Zurücknehmen</button>`);
      else btns.push(`<button class="btn ghost sm" data-rule="include" data-name="${esc(name)}" data-dest="${esc(m.destination)}" title="Ohne Größenlimit sichern">Trotzdem sichern</button>`);
    }
    if (cvAllowed) btns.push(`<button class="btn ghost sm" data-cvd="add" data-path="${esc(m.source)}" data-net="${m.reason === "network" ? esc(m.fstype || "Netzwerk") : ""}" title="Pfad in den Commvault-Subclient aufnehmen – Commvault sichert ihn dateiweise direkt auf dem Host, der Helper lässt ihn weg">Über CV sichern</button>`);
    const btn = btns.join("");
    return `<div class="mount">
      <span class="kind ${m.type === "bind" ? "bind" : ""}">${m.type === "bind" ? "Bind" : "Volume"}</span>
      <code>${esc(m.type === "volume" ? (m.name || m.source) : m.source)}</code><span class="arrow">→</span><code>${esc(m.destination)}</code>${fs}
      <span class="verdict">${size}<span class="chip ${direct ? "cvd" : take ? "take" : "skip"}">${direct ? "↗" : take ? "✓" : "⚠"} ${esc(m.text)}</span>${btn}</span>
    </div>`;
  }).join("") || '<span class="muted">Keine Volumes oder Bind-Mounts.</span>';
  const globs = (d.rules.exclude_globs || []).join(", ");
  const limit = d.limit ? `Limit pro Mount: ${fmtSize(d.limit)}` : "kein Größenlimit";
  return `${rows}<div class="plan-rules"><span class="muted small">${esc(limit)} · Ausschlussmuster in diesem Container:</span>
    <input type="text" id="glob-${esc(name)}" data-globs-for="${esc(name)}" value="${esc(globs)}" placeholder="z. B. cache, *.tmp, logs/**">
    <button class="btn ghost sm" data-save-globs="${esc(name)}">Speichern</button></div>${settingsFormHtml(name, d)}`;
}

function settingsFormHtml(name, d) {
  const st = d.settings || {};
  const r = d.rules || {};
  const fromLabel = (key, label) => (st[key + "_source"] === "label" ? `Label: ${st[key]}` : label);
  const opt = (value, label, cur) => `<option value="${esc(value)}" ${String(cur ?? "") === value ? "selected" : ""}>${esc(label)}</option>`;
  const detected = st.dump_detected ? (DUMP_LABEL[st.dump_detected] || st.dump_detected) : "keine Datenbank erkannt";
  const facts = [
    d.stack ? `Stack <b>${esc(d.stack)}</b>${d.service ? ` · Dienst ${esc(d.service)}` : ""}${(d.depends_on || []).length ? ` · hängt ab von ${esc(d.depends_on.join(", "))}` : ""}` : "kein Compose-Stack",
    `Image: ${esc(d.image_origin?.text || "?")}`,
  ];
  return `<div class="plan-settings" data-settings-for="${esc(name)}">
    <div class="ps-head"><b>Backup-Einstellungen</b><span class="muted small">${facts.join(" · ")}</span></div>
    <div class="ps-grid">
      <label class="field"><span>Modus</span><select data-ps="mode">${opt("", st.mode_source === "label" ? `wie Label (${st.mode})` : "wie der Auftrag", r.mode)}${opt("live", "immer live", r.mode)}${opt("stop", "immer stoppen", r.mode)}</select></label>
      <label class="field"><span>Stop-Timeout (Sekunden)</span><input type="number" data-ps="stop_timeout" min="0" max="3600" value="${esc(r.stop_timeout || "")}" placeholder="${esc(fromLabel("stop_timeout", "120 (Standard)"))}"></label>
      <label class="field"><span>Datenbank-Dump</span><select data-ps="dump">${opt("", `automatisch (${detected})`, r.dump)}${opt("off", "aus", r.dump)}${opt("mariadb", "MariaDB/MySQL", r.dump)}${opt("postgres", "PostgreSQL", r.dump)}${opt("redis", "Redis/Valkey (SAVE)", r.dump)}${opt("mongo", "MongoDB", r.dump)}</select></label>
      <label class="field"><span>Image</span><select data-ps="image">${opt("", st.image_source === "label" ? `wie Label (${st.image})` : "wie der Auftrag", r.image)}${opt("always", "immer sichern", r.image)}${opt("never", "nie sichern", r.image)}</select></label>
      <label class="field wide"><span>Pre-Hook <small class="muted">(sh -c im Container, vor Dump und Stopp)</small></span><input type="text" data-ps="pre" value="${esc(r.pre || "")}" placeholder="${esc(fromLabel("pre", "z. B. php occ maintenance:mode --on"))}"></label>
      <label class="field wide"><span>Post-Hook <small class="muted">(nach dem Start, auch nach Fehlern)</small></span><input type="text" data-ps="post" value="${esc(r.post || "")}" placeholder="${esc(fromLabel("post", "z. B. php occ maintenance:mode --off"))}"></label>
    </div>
    <div class="row gap"><button class="btn primary sm" data-save-settings="${esc(name)}">Einstellungen speichern</button>
      <span class="muted small">Auch als Label möglich: <code>mobydisk.mode</code>, <code>mobydisk.stop-timeout</code>, <code>mobydisk.dump</code>, <code>mobydisk.image</code>, <code>mobydisk.pre</code>, <code>mobydisk.post</code> – Werte hier haben Vorrang.</span></div>
  </div>`;
}

async function saveContainerSettings(name) {
  const box = $(`.plan-settings[data-settings-for="${CSS.escape(name)}"]`);
  if (!box) return;
  const body = {};
  $$("[data-ps]", box).forEach((el) => { body[el.dataset.ps] = el.value; });
  body.stop_timeout = parseInt(body.stop_timeout || "0", 10) || 0;
  try {
    await api(`/api/rules/containers/${encodeURIComponent(name)}`, { method: "PUT", body });
    toast(`Einstellungen für '${name}' gespeichert`, "ok");
    document.activeElement?.blur?.();
    await refreshPlan(name, true);
    loadContainers();
  } catch (err) { toast(err.message, "err"); }
}

async function refreshPlan(name, force = false) {
  const cached = appState.plans[name];
  if (cached && !force && Date.now() - cached.at < 60000) return;
  try {
    const data = await api(`/api/containers/${encodeURIComponent(name)}/plan?sizes=1`);
    appState.plans[name] = { at: Date.now(), data };
    const box = $(`.plan[data-plan="${CSS.escape(name)}"]`);
    if (box && !box.contains(document.activeElement)) box.innerHTML = planHtml(name);
  } catch (err) {
    const box = $(`.plan[data-plan="${CSS.escape(name)}"]`);
    if (box) box.innerHTML = `<span class="muted">${esc(err.message)}</span>`;
  }
}

async function setMountRule(name, dest, kind) {
  const cur = appState.plans[name]?.data.rules || {};
  let skip = (cur.skip || []).filter((d) => d !== dest);
  let include = (cur.include || []).filter((d) => d !== dest);
  if (kind === "skip") skip.push(dest);
  if (kind === "include") include.push(dest);
  try {
    await api(`/api/rules/containers/${encodeURIComponent(name)}`, { method: "PUT", body: { skip, include } });
    await refreshPlan(name, true);
    loadContainers();
  } catch (err) { toast(err.message, "err"); }
}

async function saveGlobs(name) {
  const value = $(`#glob-${CSS.escape(name)}`).value;
  try {
    await api(`/api/rules/containers/${encodeURIComponent(name)}`, { method: "PUT", body: { exclude_globs: value.split(",") } });
    toast("Ausschlussmuster gespeichert", "ok");
    await refreshPlan(name, true);
  } catch (err) { toast(err.message, "err"); }
}

function skipNoteText(names) {
  const picked = appState.containers.filter((c) => names.includes(c.name));
  const skipped = picked.flatMap((c) => (c.mount_plan || []).filter((d) => d.action === "skip" && d.reason !== "cv_direct").map((d) => ({ c: c.name, ...d })));
  if (!skipped.length) return "";
  if (picked.length === 1) {
    return `<svg class="ico"><use href="#i-alert"/></svg><span><b>Nicht gesichert werden:</b> ${skipped.map((d) => `${esc(d.name || d.source || d.destination)} <span class="muted">(${esc(d.text)})</span>`).join(", ")}. Änderbar in der aufgeklappten Zeile der Container-Liste.</span>`;
  }
  const by = {};
  skipped.forEach((d) => { by[d.reason] = (by[d.reason] || 0) + 1; });
  return `<svg class="ico"><use href="#i-alert"/></svg><span><b>${skipped.length} Mounts</b> in ${new Set(skipped.map((d) => d.c)).size} Containern werden nicht gesichert (${Object.entries(by).map(([r, n]) => `${n}× ${esc({ network: "Netzwerkspeicher", manual: "manuell", pattern: "Muster", unreachable: "nicht freigegeben", backup_root: "Backup-Ziel", volumes_root: "Docker-Volumes", system: "Systempfad", host_share: "Host-Freigabe", missing: "fehlt", special: "Socket/Gerät" }[r] || r)}`).join(", ")}). Zusätzlich greift das Größenlimit beim Packen.</span>`;
}

async function loadRules() {
  try {
    const r = await api("/api/rules");
    $("#rf-network").checked = r.skip_network;
    $("#rf-max").value = r.max_mount_gb;
    $("#rf-vol").value = r.exclude_volume_patterns.join("\n");
    $("#rf-dest").value = r.exclude_dest_patterns.join("\n");
    $("#rf-glob").value = r.exclude_globs.join("\n");
    $("#rf-regs").value = (r.own_registries || []).join("\n");
    const shares = appState.status?.host_shares || [];
    $("#rf-shares").innerHTML = shares.length ? shares.map((p) => `<span class="chip">${icon("link")}${esc(p)}</span>`).join("")
      : '<span class="muted small">keine – Bind-Mounts anderer Container werden übersprungen</span>';
    $("#cvd-paths").value = (r.cv_direct_paths || []).join("\n");
    $("#cvd-synth").value = r.cv_synth_full_days ?? 7;
  } catch (err) { toast(err.message, "err"); }
  loadCvContent();
}

function renderCvContent(st, err) {
  $("#cvdirect-panel").hidden = !activeTargets().some((t) => t.id === "commvault" && t.capabilities.includes("direct_paths"));
  const box = $("#cvd-status");
  if (err) { box.innerHTML = `<div class="notice error">${icon("alert")}<span>${esc(err)}</span></div>`; return; }
  const row = (p, cls, note) => `<li><code>${esc(p)}</code> <span class="${cls}">${esc(note)}</span></li>`;
  const items = [
    ...st.current.filter((p) => !st.remove.includes(p)).map((p) => row(p, st.foreign.includes(p) ? "muted" : "cv-ok",
      st.foreign.includes(p) ? "in Commvault eingetragen (nicht vom Helper)" : "✓")),
    ...st.missing.map((p) => row(p, "cv-no", "fehlt – wird beim Übertragen ergänzt")),
    ...st.remove.map((p) => row(p, "cv-no", "wird beim Übertragen entfernt")),
  ];
  box.innerHTML = `<p style="margin:2px 0 6px">Subclient <b>${esc(st.subclient)}</b>: ${st.in_sync
    ? '<span class="cv-ok">aktuell</span>' : '<span class="cv-no">weicht ab</span>'}${st.filters ? ` · ${st.filters} Filter/Ausnahmen bleiben erhalten` : ""}</p>
    <ul class="plain-list">${items.join("")}</ul>`;
}

async function loadCvContent() {
  if (!appState.status?.cv_configured) { renderCvContent(null, "Commvault ist nicht konfiguriert."); return; }
  $("#cvd-status").innerHTML = '<span class="muted">Lade …</span>';
  try { renderCvContent(await api("/api/cv/content")); } catch (err) { renderCvContent(null, err.message); }
}

async function saveCvDirect() {
  try {
    const r = await api("/api/rules", { method: "PUT", body: {
      cv_direct_paths: $("#cvd-paths").value, cv_synth_full_days: $("#cvd-synth").value || 0, cv_push: true } });
    $("#cvd-paths").value = (r.cv_direct_paths || []).join("\n");
    appState.plans = {};
    if (r.cv_content_error) {
      toast("Gespeichert, aber nicht an Commvault übertragen: " + r.cv_content_error, "err");
      renderCvContent(null, r.cv_content_error);
    } else {
      toast(r.cv_content?.changed ? "Gespeichert und an Commvault übertragen" : "Gespeichert – Commvault war schon aktuell", "ok");
      if (r.cv_content) renderCvContent(r.cv_content);
    }
    loadContainers();
  } catch (err) { toast(err.message, "err"); }
}

async function saveRules() {
  try {
    await api("/api/rules", { method: "PUT", body: {
      skip_network: $("#rf-network").checked, max_mount_gb: $("#rf-max").value || 0,
      exclude_volume_patterns: $("#rf-vol").value, exclude_dest_patterns: $("#rf-dest").value, exclude_globs: $("#rf-glob").value,
      own_registries: $("#rf-regs").value } });
    appState.plans = {};
    toast("Backup-Umfang gespeichert", "ok");
    loadContainers();
  } catch (err) { toast(err.message, "err"); }
}

async function loadCleanup() {
  try {
    const c = await api("/api/cleanup/incomplete");
    $("#cap-cleanup").innerHTML = c.count
      ? `<div class="notice">${icon("alert")}<span>${c.count} unvollständige Sicherung(en) ohne Manifest belegen ${esc(fmtSize(c.bytes))} (abgebrochene Läufe).</span><span class="spacer"></span><button class="btn sm" id="cleanup-run">Aufräumen</button></div>`
      : "";
  } catch (_err) { /* optional */ }
}

/* ───────── Kapazität & Prognose ───────── */
function fmtDays(d) {
  if (d === null || d === undefined) return "> 2 Jahre";
  if (d >= 60) return `ca. ${Math.round(d / 30)} Monate`;
  return `${d} Tage`;
}

function chartSvg(fc) {
  const W = 900, H = 290, L = 64, R = 18, T = 16, B = 34;
  const pts = fc.series;
  const limit = fc.free !== null ? fc.used_local + fc.free - fc.reserve : null;
  let maxV = Math.max(...pts.map((p) => p[1]), 1);
  const showLimit = limit !== null && limit <= maxV * 4;
  if (showLimit) maxV = Math.max(maxV, limit);
  maxV *= 1.1;
  const x = (t) => L + (t / 365) * (W - L - R);
  const y = (v) => T + (1 - v / maxV) * (H - T - B);
  const line = pts.map((p) => `${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join(" ");
  const area = `${L},${y(0)} ${line} ${x(365)},${y(0)}`;
  const grid = [0, 0.25, 0.5, 0.75, 1].map((f) => {
    const v = maxV * f / 1.1 * 1.1; const yy = y(v);
    return `<line x1="${L}" x2="${W - R}" y1="${yy}" y2="${yy}" class="grid"/><text x="${L - 8}" y="${yy + 4}" text-anchor="end">${esc(fmtSize(v))}</text>`;
  }).join("");
  const xt = [[0, "heute"], [90, "3 Mon."], [180, "6 Mon."], [270, "9 Mon."], [365, "1 Jahr"]].map(([t, l]) =>
    `<text x="${x(t)}" y="${H - 10}" text-anchor="${t === 0 ? "start" : t === 365 ? "end" : "middle"}">${l}</text>`).join("");
  const marks = [[30, "30 T."], [90, "90 T."], [365, "1 J."]].map(([t]) => {
    const v = pts.find((p) => p[0] === t)[1];
    return `<circle cx="${x(t)}" cy="${y(v)}" r="4" class="dot"><title>In ${t} Tagen: ${esc(fmtSize(v))}</title></circle>`;
  }).join("");
  const lim = showLimit ? `<line x1="${L}" x2="${W - R}" y1="${y(limit)}" y2="${y(limit)}" class="limit"/><text x="${W - R}" y="${y(limit) - 6}" text-anchor="end" class="limit-label">Speicher voll (frei − Reserve)</text>` : "";
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Prognose der lokalen Belegung">
    ${grid}${xt}<polygon points="${area}" class="area"/><polyline points="${line}" class="series"/>${lim}${marks}</svg>`;
}

async function loadForecast() {
  loadCleanup();
  const keep = $("#wi-keep").value, mult = $("#wi-runs").value;
  const btn = $("#cap-refresh");
  btn.classList.add("spin");
  try {
    const fc = await api(`/api/forecast?${keep ? `keep=${keep}&` : ""}runs_mult=${mult}`);
    appState.forecast = fc;
    renderForecast(fc);
  } catch (err) {
    toast(err.message, "err");
  } finally {
    btn.classList.remove("spin");
  }
}

function renderForecast(fc) {
  const t = fc.totals;
  $("#cap-now").textContent = fmtSize(t.now);
  $("#cap-free").textContent = fc.free !== null ? `${fmtSize(fc.free)} frei von ${fmtSize(fc.fs_total)}` : "";
  const delta = (v) => (v - t.now >= 0 ? "+" : "−") + fmtSize(Math.abs(v - t.now));
  $("#cap-30").textContent = fmtSize(t.d30); $("#cap-30d").textContent = `${delta(t.d30)} gegenüber heute`;
  $("#cap-90").textContent = fmtSize(t.d90); $("#cap-90d").textContent = `${delta(t.d90)} gegenüber heute`;
  $("#cap-full").textContent = fmtDays(fc.days_until_full);
  $("#cap-full-sub").textContent = fc.free !== null ? `Reserve für Restore: ${fmtSize(fc.reserve)}` : "";
  const tile = $("#cap-full-tile");
  tile.classList.toggle("crit", fc.days_until_full !== null && fc.days_until_full <= 30);
  tile.classList.toggle("warn", fc.days_until_full !== null && fc.days_until_full > 30 && fc.days_until_full <= 90);
  $("#cap-warnings").innerHTML = fc.warnings.map((w) =>
    `<div class="notice ${w.level === "error" ? "error" : w.level === "info" ? "info" : ""}">${icon("alert")}<span>${esc(w.text)}</span></div>`).join("");
  $("#cap-chart").innerHTML = chartSvg(fc);
  const rows = fc.containers;
  $("#cap-rows").innerHTML = rows.length ? rows.map((r) => `
    <tr class="item">
      <td><strong>${esc(r.name)}</strong>${r.scheduled ? "" : ' <span class="muted small">(kein Zeitplan)</span>'}${r.unlimited ? ' <span class="chip bind" title="Keine Aufbewahrung – wächst unbegrenzt">unbegrenzt</span>' : ""}</td>
      <td class="right">${r.copies}${r.cap ? ` <span class="muted small">/ max. ${Math.round(r.cap)}</span>` : ""}</td>
      <td class="right">${esc(fmtSize(r.size_per_copy))}</td>
      <td class="right" title="Konfidenz: ${esc(r.confidence)}">${r.growth_per_day > 0 ? "+" + esc(fmtSize(r.growth_per_day)) : "–"}${r.confidence === "niedrig" && r.scheduled ? ' <span class="muted small">(?)</span>' : ""}</td>
      <td class="right">${r.runs_per_day ? r.runs_per_day.toFixed(2).replace(/\.?0+$/, "") : "–"}</td>
      <td class="right">${esc(fmtSize(r.usage_now))}</td>
      <td class="right">${esc(fmtSize(r.usage_30))}</td>
      <td class="right">${esc(fmtSize(r.usage_90))}</td>
      <td class="right"><b>${esc(fmtSize(r.usage_365))}</b></td>
    </tr>`).join("") : `<tr><td colspan="9"><div class="empty">${icon("chart")}<strong>Noch keine Daten</strong>Sobald Sicherungen oder Zeitpläne existieren, erscheint hier die Prognose.</div></td></tr>`;
}

/* ───────── Benachrichtigungen ───────── */
async function loadNotify() {
  try {
    const c = await api("/api/notify");
    $("#nf-ev-failure").checked = c.events.failure;
    $("#nf-ev-success").checked = c.events.success;
    $("#nf-ev-missed").checked = c.events.missed;
    $("#nf-ev-digest").checked = c.events.digest;
    $("#nf-digest-always").checked = c.digest_always;
    $("#nf-digest-time").value = c.digest_time;
    $("#nf-rpo").value = c.rpo_hours;
    $("#nf-warn-days").value = c.storage_warn_days;
    $("#nf-warn-gb").value = c.storage_warn_free_gb;
    $("#nf-heartbeat").value = c.heartbeat_url || "";
    $("#nf-wh-on").checked = c.channels.webhook.enabled; $("#nf-wh-url").value = c.channels.webhook.url;
    $("#nf-nt-on").checked = c.channels.ntfy.enabled; $("#nf-nt-url").value = c.channels.ntfy.url;
    $("#nf-nt-token").placeholder = c.channels.ntfy.token_set ? "Token gesetzt (leer = unverändert)" : "Token (optional)";
    const m = c.channels.email;
    $("#nf-em-on").checked = m.enabled; $("#nf-em-host").value = m.host; $("#nf-em-port").value = m.port;
    $("#nf-em-sec").value = m.security; $("#nf-em-user").value = m.user; $("#nf-em-from").value = m.from; $("#nf-em-to").value = m.to;
    $("#nf-em-pass").placeholder = m.password_set ? "Passwort gesetzt (leer = unverändert)" : "Passwort";
  } catch (err) {
    toast(err.message, "err");
  }
}

function notifyBody() {
  return {
    events: { failure: $("#nf-ev-failure").checked, success: $("#nf-ev-success").checked,
      missed: $("#nf-ev-missed").checked, digest: $("#nf-ev-digest").checked },
    digest_always: $("#nf-digest-always").checked, digest_time: $("#nf-digest-time").value || "08:00",
    rpo_hours: $("#nf-rpo").value, storage_warn_days: $("#nf-warn-days").value, storage_warn_free_gb: $("#nf-warn-gb").value,
    heartbeat_url: $("#nf-heartbeat").value.trim(),
    channels: {
      webhook: { enabled: $("#nf-wh-on").checked, url: $("#nf-wh-url").value.trim() },
      ntfy: { enabled: $("#nf-nt-on").checked, url: $("#nf-nt-url").value.trim(), token: $("#nf-nt-token").value },
      email: { enabled: $("#nf-em-on").checked, host: $("#nf-em-host").value.trim(), port: $("#nf-em-port").value || 587,
        security: $("#nf-em-sec").value, user: $("#nf-em-user").value.trim(), password: $("#nf-em-pass").value,
        from: $("#nf-em-from").value.trim(), to: $("#nf-em-to").value.trim() },
    },
  };
}

async function saveNotify() {
  try {
    await api("/api/notify", { method: "PUT", body: notifyBody() });
    $("#nf-nt-token").value = ""; $("#nf-em-pass").value = "";
    toast("Benachrichtigungen gespeichert", "ok");
    $("#nf-result").textContent = "";
    loadNotify();
    loadContainers();   // Schutzziel kann sich geändert haben
    return true;
  } catch (err) {
    toast(err.message, "err");
    return false;
  }
}

async function testNotify() {
  if (!(await saveNotify())) return;
  $("#nf-result").textContent = "Sende …";
  try {
    const res = await api("/api/notify/test", { method: "POST", body: {} });
    $("#nf-result").innerHTML = res.map((r) => r.ok
      ? `<span class="cv-ok">${esc(r.channel)} ✓</span>` : `<span class="cv-no" title="${esc(r.error)}">${esc(r.channel)} ✗ ${esc(r.error.slice(0, 80))}</span>`).join(" · ");
  } catch (err) {
    $("#nf-result").textContent = "";
    toast(err.message, "err");
  }
}

/* ───────── Einstellungen ───────── */
async function loadSettings() {
  try {
    const data = await api("/api/settings");
    $("#set-cv-url").textContent = data.cv_url || "nicht gesetzt";
    $("#set-cv-path").textContent = data.cv_webservice_path;
    $("#set-cv-user").textContent = data.cv_username || "nicht gesetzt";
    $("#set-cv-pass").textContent = data.cv_password_set ? "gesetzt" : "NICHT gesetzt";
    $("#set-cv-tls").textContent = data.cv_verify_tls ? "ja" : "nein";
    $("#set-backup-root").textContent = data.backup_root;
    $("#set-staging-root").textContent = data.staging_root;
    $("#backup-root-hint").textContent = data.backup_root;
    $("#cv-test-result").innerHTML = "";
    loadNotify();
    loadRules();
  } catch (err) {
    toast(err.message, "err");
  }
}

async function loadClients() {
  const select = $("#client-select");
  select.innerHTML = '<option value="">Lade …</option>';
  try {
    appState.clients = await api("/api/settings/clients");
    const sorted = appState.clients.slice().sort((a, b) => cmp(a.client_name, b.client_name));
    select.innerHTML = '<option value="">-- Client wählen --</option>' + sorted.map((c) =>
      `<option value="${esc(c.client_id)}">${esc(c.client_name)} (${esc(c.host_name || c.display || "")})</option>`
    ).join("");
    toast("Clientliste geladen", "ok");
  } catch (err) {
    select.innerHTML = '<option value="">-- Fehler --</option>';
    toast(err.message, "err");
  }
}

async function loadSubclients(clientId) {
  const select = $("#subclient-select");
  select.innerHTML = '<option value="">Lade …</option>';
  try {
    appState.subclients = await api(`/api/settings/subclients?client_id=${clientId}`);
    select.innerHTML = '<option value="">-- Subclient wählen --</option>' + appState.subclients.map((s) =>
      `<option value="${esc(s.subclient_id)}">${esc(s.subclient_name)} (${esc(s.app_name || "")})</option>`
    ).join("");
  } catch (err) {
    select.innerHTML = '<option value="">-- Fehler --</option>';
    toast(err.message, "err");
  }
}

function showSubclientInfo(subclient) {
  $("#subclient-info").style.display = subclient ? "grid" : "none";
  if (!subclient) return;
  $("#sc-app").textContent = subclient.app_name || "-";
  $("#sc-backupset").textContent = subclient.backup_set_name || "-";
  $("#sc-instance").textContent = subclient.instance_name || "-";
  $("#sc-ids").textContent =
    `client=${subclient.client_id}, subclient=${subclient.subclient_id}, ` +
    `app=${subclient.application_id}, backupset=${subclient.backup_set_id}, instance=${subclient.instance_id}`;
}

async function saveIdentity() {
  const clientId = $("#client-select").value;
  const subclientId = $("#subclient-select").value;
  if (!clientId || !subclientId) {
    toast("Client und Subclient wählen.", "err");
    return;
  }
  const client = appState.clients.find((c) => String(c.client_id) === String(clientId));
  const subclient = appState.subclients.find((s) => String(s.subclient_id) === String(subclientId));
  try {
    await api("/api/settings", {
      method: "POST",
      body: {
        cv_client_id: Number(clientId),
        cv_client_name: client?.client_name || null,
        cv_subclient_id: Number(subclientId),
        cv_subclient_name: subclient?.subclient_name || null,
        cv_app_type_id: subclient?.application_id ?? null,
        cv_instance_id: subclient?.instance_id ?? null,
        cv_instance_name: subclient?.instance_name || null,
        cv_backup_set_id: subclient?.backup_set_id ?? null,
        cv_backup_set_name: subclient?.backup_set_name || null,
      },
    });
    toast("Auswahl gespeichert", "ok");
    loadStatus();
  } catch (err) {
    toast(err.message, "err");
  }
}

async function autoDetect() {
  try {
    const match = await api("/api/settings/auto-detect", { method: "POST" });
    toast(`Host erkannt: ${match.client_name} (ID ${match.client_id})`, "ok");
    await loadClients();
    $("#client-select").value = String(match.client_id);
    await loadSubclients(match.client_id);
  } catch (err) {
    toast(err.message, "err");
  }
}

async function testCv() {
  $("#cv-test-result").innerHTML = '<span class="muted">Teste …</span>';
  try {
    const result = await api("/api/settings/test-cv", { method: "POST" });
    $("#cv-test-result").innerHTML = `<div class="notice ok">${icon("check")}<span>Verbindung OK. Token: ${esc(result.token_prefix)}</span></div>`;
  } catch (err) {
    $("#cv-test-result").innerHTML = `<div class="notice error">${icon("alert")}<span>${esc(err.message)}</span></div>`;
  }
}

/* ───────── Events ───────── */
function setSeg(groupSel, value) {
  $$(`${groupSel} button`).forEach((b) => b.classList.toggle("on", b.dataset.v === value));
}

function resetContainerFilters() {
  appState.cFilter = { q: "", state: "all", dataOnly: false, noBackup: false, unprot: false, stack: "" };
  $("#c-unprot").checked = false;
  $("#c-search").value = "";
  $("#c-dataonly").checked = false;
  $("#c-nobackup").checked = false;
  setSeg("#c-state", "all");
  persistContainerPrefs();
  renderContainers();
}

document.addEventListener("click", (event) => {
  const t = event.target;
  const closer = t.closest("[data-close]");
  if (closer) closeModal(closer.dataset.close);
  if (t.classList && t.classList.contains("modal-backdrop")) closeModal(t.id);

  const tabBtn = t.closest("nav.tabs button");
  if (tabBtn) switchTab(tabBtn.dataset.tab);

  const actionBtn = t.closest("[data-action]");
  if (actionBtn) {
    const name = actionBtn.dataset.name;
    if (actionBtn.dataset.action === "backup") openBackupModal(name);
    if (actionBtn.dataset.action === "versions") openVersionsModal(name);
  }

  const selBox = t.closest("[data-sel]");
  if (selBox) {
    const name = selBox.dataset.sel;
    if (event.shiftKey && appState.lastClicked && appState.visible.includes(appState.lastClicked)) {
      const a = appState.visible.indexOf(appState.lastClicked);
      const b = appState.visible.indexOf(name);
      const [from, to] = a < b ? [a, b] : [b, a];
      for (const n of appState.visible.slice(from, to + 1)) setSelected(n, selBox.checked);
    } else {
      setSelected(name, selBox.checked);
    }
    appState.lastClicked = name;
    updateSelectionUi();
  }
  if (t.closest("#c-selall")) {
    const on = $("#c-selall").checked;
    for (const n of appState.visible) setSelected(n, on);
    updateSelectionUi();
  }

  const toggle = t.closest("[data-toggle]");
  if (toggle) {
    const name = toggle.dataset.toggle;
    if (appState.expanded.has(name)) appState.expanded.delete(name); else appState.expanded.add(name);
    persistContainerPrefs();
    renderContainers();
  }

  const th = t.closest("#c-table th.sortable");
  if (th && !t.closest(".sel-box")) {
    const key = th.dataset.sort;
    appState.cSort = {
      key,
      dir: appState.cSort.key === key && appState.cSort.dir === "asc" ? "desc" : "asc",
    };
    persistContainerPrefs();
    renderContainers();
  }

  const vth = t.closest("[data-vsort]");
  if (vth) {
    const [kind, key] = vth.dataset.vsort.split(":");
    const cur = appState.vSort[kind] || { key: "time", dir: "desc" };
    appState.vSort[kind] = { key, dir: cur.key === key && cur.dir === "desc" ? "asc" : "desc" };
    renderVersionTable(kind);
  }

  const restoreBtn = t.closest("[data-restore]");
  if (restoreBtn) openRestoreModal(appState.versionsFor, restoreBtn.dataset.restore, restoreBtn.dataset.kind, restoreBtn.dataset.folder, restoreBtn.dataset.time);

  if (t.closest("#reset-filters")) resetContainerFilters();

  const stackBtn = t.closest("[data-stack]");
  if (stackBtn) {
    const members = appState.containers.filter((c) => c.stack === stackBtn.dataset.stack && !c.clone_of).map((c) => c.name);
    const allOn = members.every((n) => appState.selected.has(n));
    members.forEach((n) => setSelected(n, !allOn));
    updateSelectionUi();
    toast(allOn ? `Stack '${stackBtn.dataset.stack}' abgewählt` : `Stack '${stackBtn.dataset.stack}': ${members.length} Container ausgewählt`, "ok");
  }
  const setBtn = t.closest("[data-save-settings]");
  if (setBtn) saveContainerSettings(setBtn.dataset.saveSettings);
  const tgtBtn = t.closest("#restore-target button[data-v]");
  if (tgtBtn) setRestoreTarget(tgtBtn.dataset.v);
  const promoteBtn = t.closest("[data-clone-promote]");
  if (promoteBtn) {
    const name = promoteBtn.dataset.clonePromote;
    const orig = appState.containers.find((c) => c.name === name)?.clone_of;
    if (confirm(`Den Sicherungsstand des Klons '${name}' ins Original '${orig}' übernehmen?\n\nDas Original wird mit diesem Stand überschrieben, danach wird der Klon samt Volumes entfernt.`)) {
      api(`/api/clones/${encodeURIComponent(name)}/promote`, { method: "POST", body: {} })
        .then((job) => { toast(`Übernahme gestartet (Job ${job.id})`, "ok"); switchTab("jobs"); })
        .catch((err) => toast(err.message, "err"));
    }
  }
  const dropBtn = t.closest("[data-clone-drop]");
  if (dropBtn) {
    const name = dropBtn.dataset.cloneDrop;
    if (confirm(`Klon '${name}' samt seinen Volumes löschen? Das Original bleibt unverändert.`)) {
      api(`/api/clones/${encodeURIComponent(name)}`, { method: "DELETE" })
        .then((r) => { toast(`Klon entfernt (${r.volumes.length} Volumes)`, "ok"); loadContainers(); })
        .catch((err) => toast(err.message, "err"));
    }
  }

  const ruleBtn = t.closest("[data-rule]");
  if (ruleBtn) setMountRule(ruleBtn.dataset.name, ruleBtn.dataset.dest, ruleBtn.dataset.rule);
  const globBtn = t.closest("[data-save-globs]");
  if (globBtn) saveGlobs(globBtn.dataset.saveGlobs);
  if (t.closest("#cleanup-run")) {
    api("/api/cleanup/incomplete", { method: "POST", body: {} })
      .then((r) => { toast(`${r.count} Ordner entfernt, ${fmtSize(r.bytes)} frei`, "ok"); loadCleanup(); loadStatus(); })
      .catch((err) => toast(err.message, "err"));
  }

  if (t.closest("#sched-new") || t.closest("#sched-new-empty")) openScheduleModal();
  for (const [attr, kind] of [["schedRun", "run"], ["schedToggle", "toggle"], ["schedEdit", "edit"], ["schedDel", "del"]]) {
    const btn = t.closest(`[data-sched-${attr.slice(5).toLowerCase()}]`);
    if (btn) scheduleAction(kind, btn.dataset[attr]);
  }
  const scopeBtn = t.closest("#sf-scope button[data-v]");
  if (scopeBtn) {
    setScope(scopeBtn.dataset.v);
  }
});

// Protokoll-Zustand merken, damit Auto-Refresh geöffnete Logs nicht zuklappt
document.addEventListener("toggle", (e) => {
  const d = e.target;
  if (d && d.matches && d.matches("details[data-job]")) {
    if (d.open) {
      const fresh = !appState.openLogs.has(d.dataset.job);   // nicht beim Neuzeichnen eines schon offenen Protokolls
      appState.openLogs.add(d.dataset.job);
      const box = $(".job-log", d);
      if (fresh && box) box.scrollTop = box.scrollHeight;   // frisch geöffnet: ans Ende springen
    } else appState.openLogs.delete(d.dataset.job);
  }
}, true);

$("#v-tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-v]");
  if (b) setVersionTab(b.dataset.v);
});

$("#c-search").addEventListener("input", debounce((e) => {
  appState.cFilter.q = e.target.value;
  renderContainers();
}, 80));
$("#c-search-clear").addEventListener("click", () => {
  $("#c-search").value = "";
  appState.cFilter.q = "";
  renderContainers();
  $("#c-search").focus();
});
$("#c-state").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-v]");
  if (!b) return;
  appState.cFilter.state = b.dataset.v;
  setSeg("#c-state", b.dataset.v);
  persistContainerPrefs();
  renderContainers();
});
$("#c-dataonly").addEventListener("change", (e) => { appState.cFilter.dataOnly = e.target.checked; persistContainerPrefs(); renderContainers(); });
$("#c-nobackup").addEventListener("change", (e) => { appState.cFilter.noBackup = e.target.checked; persistContainerPrefs(); renderContainers(); });
$("#c-unprot").addEventListener("change", (e) => { appState.cFilter.unprot = e.target.checked; persistContainerPrefs(); renderContainers(); });
$("#c-stack").addEventListener("change", (e) => { appState.cFilter.stack = e.target.value; persistContainerPrefs(); renderContainers(); });
$("#c-sort").addEventListener("change", (e) => {
  const [key, dir] = e.target.value.split(":");
  appState.cSort = { key, dir };
  persistContainerPrefs();
  renderContainers();
});

$("#j-search").addEventListener("input", debounce((e) => { appState.jFilter.q = e.target.value; renderJobs(); }, 120));
document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-job-act]");
  if (!b) return;
  const act = b.dataset.jobAct;
  if (act === "cancel" && !confirm("Auftrag wirklich abbrechen?\n\nBereits fertig gesicherte Container bleiben lokal erhalten, eine laufende Archivierung wird beendet und der betroffene Container wieder gestartet.")) return;
  b.disabled = true;
  try {
    await api(`/api/jobs/${b.dataset.id}/${act}`, { method: "POST" });
    toast(act === "pause" ? "Pause angefordert" : act === "resume" ? "Wird fortgesetzt" : "Abbruch angefordert", "ok");
    loadJobs();
  } catch (err) { toast(err.message, "err"); b.disabled = false; }
});
$("#j-status").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-v]"); if (!b) return;
  appState.jFilter.status = b.dataset.v; setSeg("#j-status", b.dataset.v); store.set("j-filter", appState.jFilter); renderJobs();
});
$("#j-kind").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-v]"); if (!b) return;
  appState.jFilter.kind = b.dataset.v; setSeg("#j-kind", b.dataset.v); store.set("j-filter", appState.jFilter); renderJobs();
});
$("#j-sort").addEventListener("change", (e) => { appState.jFilter.sort = e.target.value; store.set("j-filter", appState.jFilter); renderJobs(); });

$("#login-btn").addEventListener("click", async () => {
  try {
    await api("/api/login", { method: "POST", body: { password: $("#login-password").value } });
    showApp();
    await init();
  } catch (err) {
    toast(err.message, "err");
  }
});
$("#login-password").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#login-btn").click(); });
$("#logout-btn").addEventListener("click", async () => {
  try { await api("/api/logout", { method: "POST" }); } catch (_e) { /* ignore */ }
  showLogin();
});
$("#theme-btn").addEventListener("click", toggleTheme);
$("#refresh-containers").addEventListener("click", () => loadContainers(true));
$("#bulk-backup").addEventListener("click", () => {
  const names = appState.containers.map((c) => c.name).filter((n) => appState.selected.has(n));
  if (names.length) openBackupModal(names);
});
$("#bulk-clear").addEventListener("click", () => { appState.selected.clear(); updateSelectionUi(); });
$("#refresh-jobs").addEventListener("click", () => loadJobs(true));
$("#backup-submit").addEventListener("click", submitBackup);
$("#sf-save").addEventListener("click", saveSchedule);
$("#nf-save").addEventListener("click", saveNotify);
$("#rf-save").addEventListener("click", saveRules);
$("#cvd-save").addEventListener("click", saveCvDirect);
document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-cvd]");
  if (!b) return;
  const path = b.dataset.path;
  const adding = b.dataset.cvd === "add";
  const net = b.dataset.net ? `\n\nACHTUNG: Netzwerkspeicher (${b.dataset.net}). Commvault liest die Daten dann über den Docker-Host vom NAS – ` +
    "nur solange die Freigabe dort eingehängt ist (bei Docker-Volumes: solange ein Container sie nutzt). Besser ist meist, das NAS selbst mit Commvault zu sichern." : "";
  if (adding && !confirm(`${path}\n\nin den Commvault-Subclient aufnehmen? Commvault sichert den Pfad dann dateiweise direkt, der Helper lässt ihn in seinen Archiven weg.${net}`)) return;
  try {
    const cur = await api("/api/rules");
    const list = (cur.cv_direct_paths || []).filter((p) => p !== path);
    if (adding) list.push(path);
    else if (list.length === (cur.cv_direct_paths || []).length) { toast("Der Pfad liegt unter einem eingetragenen Ordner – bitte in den Einstellungen anpassen.", "err"); return; }
    const r = await api("/api/rules", { method: "PUT", body: { cv_direct_paths: list.join("\n"), cv_push: true } });
    toast(r.cv_content_error ? `Gespeichert, aber nicht an Commvault übertragen: ${r.cv_content_error}` : adding ? "Pfad in den Commvault-Subclient aufgenommen – Commvault sichert ihn jetzt direkt" : "Pfad aus dem Subclient entfernt – der Helper sichert ihn wieder", r.cv_content_error ? "err" : "ok");
    appState.plans = {};
    loadContainers();
  } catch (err) { toast(err.message, "err"); }
});
$("#cvd-refresh").addEventListener("click", loadCvContent);
$("#nf-test").addEventListener("click", testNotify);
$("#cap-refresh").addEventListener("click", loadForecast);
$("#wi-keep").addEventListener("change", loadForecast);
$("#wi-runs").addEventListener("change", loadForecast);
$("#sf-filter").addEventListener("input", debounce(() => {
  renderScheduleContainerPick(appState.pickSelected || new Set());
}, 80));
$("#sf-containers").addEventListener("change", (e) => {
  const input = e.target;
  if (!appState.pickSelected) appState.pickSelected = new Set();
  if (input.checked) appState.pickSelected.add(input.value); else appState.pickSelected.delete(input.value);
  updatePickAll();
});
$("#sf-all").addEventListener("change", (e) => {
  // wirkt auf die aktuell sichtbaren (gefilterten) Container
  if (!appState.pickSelected) appState.pickSelected = new Set();
  $$("#sf-containers input").forEach((i) => {
    i.checked = e.target.checked;
    if (e.target.checked) appState.pickSelected.add(i.value); else appState.pickSelected.delete(i.value);
  });
  updatePickAll();
});
$("#restore-submit").addEventListener("click", submitRestore);
$("#test-cv-btn").addEventListener("click", testCv);
$("#load-clients-btn").addEventListener("click", loadClients);
$("#autodetect-btn").addEventListener("click", autoDetect);
$("#client-select").addEventListener("change", (e) => {
  $("#subclient-info").style.display = "none";
  if (e.target.value) loadSubclients(e.target.value);
});
$("#subclient-select").addEventListener("change", (e) => {
  showSubclientInfo(appState.subclients.find((s) => String(s.subclient_id) === String(e.target.value)));
});
$("#save-identity-btn").addEventListener("click", saveIdentity);

document.addEventListener("keydown", (e) => {
  const ae = document.activeElement;
  const typing = !!ae && (ae.tagName === "TEXTAREA" || ae.tagName === "SELECT"
    || (ae.tagName === "INPUT" && /^(text|search|password|number)$/.test(ae.type)));
  if (e.key === "Escape") {
    const open = $$(".modal-backdrop.open");
    if (open.length) { closeModal(open[open.length - 1].id); return; }
    if (document.activeElement === $("#c-search") && $("#c-search").value) { $("#c-search-clear").click(); return; }
    if (appState.selected.size && !typing) { appState.selected.clear(); updateSelectionUi(); }
  }
  if (e.key === "/" && !typing && $("#tab-containers").classList.contains("active")) {
    e.preventDefault();
    $("#c-search").focus();
  }
});

/* ───────── Start ───────── */
function syncControlsFromState() {
  const f = appState.cFilter;
  setSeg("#c-state", f.state);
  $("#c-dataonly").checked = f.dataOnly;
  $("#c-nobackup").checked = f.noBackup;
  $("#c-unprot").checked = !!f.unprot;
  setSeg("#j-status", appState.jFilter.status);
  setSeg("#j-kind", appState.jFilter.kind);
  $("#j-sort").value = appState.jFilter.sort;
}

async function init() {
  syncControlsFromState();
  const tab = store.get("tab", "containers");
  if (tab && tab !== "containers") switchTab(tab);
  await Promise.all([loadStatus(), loadContainers(), loadJobs()]);
}

(async function boot() {
  applyThemeIcon();
  syncControlsFromState();
  try {
    await api("/api/status");   // ohne gültige Sitzung antwortet der Server mit 401 → Anmeldung
    showApp();
    await init();
  } catch (_err) {
    showLogin();
  }
})();

setInterval(() => {
  if ($("#app-view").style.display === "block" && !document.hidden && !document.activeElement?.closest?.(".plan")) { loadStatus(); loadContainers(); }
}, 15000);

setInterval(async () => {
  if ($("#app-view").style.display !== "block" || document.hidden) return;
  try {
    const list = await api("/api/jobs");
    const hadActive = appState.jobs.some(jobActive);
    const nowActive = list.some(jobActive);
    appState.jobs = list;
    renderJobs();
    if ($("#tab-schedules").classList.contains("active")) loadSchedules();
    if (hadActive && !nowActive) {
      loadContainers();
      if ($("#tab-backups").classList.contains("active")) loadBackups();
      if (list.some((j) => j.status === "failed")) toast("Ein Auftrag ist fehlgeschlagen – siehe Jobs.", "err");
    }
  } catch (_err) { /* ignore */ }
}, 4000);


/* ───────── Backups (lokal und nur noch in Commvault) ───────── */
const bState = { rows: [], sel: new Set(), where: store.get("b-where", "all") };
const bKey = (r) => `${r.container}\u0000${r.folder}`;

async function loadBackups() {
  try {
    const data = await api("/api/backups");
    bState.rows = data.backups;
    bState.total = data.total_bytes;
    bState.syncs = data.syncs;
    const alive = new Set(bState.rows.filter((r) => r.local).map(bKey));
    bState.sel = new Set([...bState.sel].filter((k) => alive.has(k)));
    renderBackups();
  } catch (e) { toast(e.message, "error"); }
}

function bVisible() {
  const q = $("#b-search").value.trim().toLowerCase();
  const [field, dir] = $("#b-sort").value.split(":");
  const rows = bState.rows.filter((r) => (bState.where === "all" || (bState.where === "local") === !!r.local) &&
    (!q || r.container.toLowerCase().includes(q) || r.folder.toLowerCase().includes(q)));
  const key = { created: (r) => r.created || 0, size: (r) => r.size_bytes, container: (r) => r.container.toLowerCase() }[field];
  rows.sort((a, b) => (key(a) < key(b) ? -1 : key(a) > key(b) ? 1 : 0) * (dir === "desc" ? -1 : 1));
  return rows;
}

function bCvCell(r) {
  const names = (r.targets || []).map(sourceLabel).join(", ");
  if (!r.local) return `<span class="pill cv" title="Liegt nur noch in: ${esc(names)}"><span class="dot"></span>nur in ${esc(names)}</span>`;
  return r.in_target ? `<span class="pill ok" title="Bestätigt in: ${esc(names)}"><span class="dot"></span>${esc(names)}</span>` : '<span class="pill"><span class="dot"></span>nicht bestätigt</span>';
}

function renderBackups() {
  setSeg("#b-where", bState.where);
  const rows = bVisible();
  const local = bState.rows.filter((r) => r.local);
  const cvOnly = bState.rows.length - local.length;
  const n = new Set(local.map((r) => r.container)).size;
  const syncTimes = Object.values(bState.syncs || {}).map((x) => x?.time).filter(Boolean);
  const sync = syncTimes.length ? ` · mit Zielen abgeglichen ${ago(new Date(Math.min(...syncTimes) * 1000))}` : "";
  $("#b-total").textContent = `${local.length} lokale Sicherungen von ${n} Containern · ${fmtSize(bState.total)} belegt` +
    (cvOnly ? ` · ${cvOnly} nur noch in Backup-Zielen` : "") + sync +
    (rows.length !== bState.rows.length ? ` · ${rows.length} angezeigt` : "");
  const empty = bState.where === "cv"
    ? "Keine Sicherungen, die nur noch in Backup-Zielen liegen. „Mit Zielen abgleichen“ sucht auch nach älteren."
    : bState.where === "local" ? "Keine lokalen Sicherungen." : "Keine Sicherungen.";
  $("#b-rows").innerHTML = rows.map((r) => {
    const k = bKey(r);
    const parts = [];
    if (r.image != null) parts.push(r.image ? "Daten + Image" : "Daten");
    if ((r.dumps || []).length) parts.push(r.dumps.map((t) => DUMP_LABEL[t] || t).join(", "));
    if (r.stack) parts.push(`Stack ${r.stack}`);
    if (r.skipped) parts.push(`${r.skipped} übersprungen`);
    if (!r.local && r.local_deleted) parts.push(`lokal gelöscht ${ago(new Date(r.local_deleted * 1000))}`);
    const box = r.local
      ? `<input type="checkbox" class="sel-box b-sel" ${bState.sel.has(k) ? "checked" : ""} aria-label="Auswählen">`
      : '<input type="checkbox" class="sel-box" disabled title="Liegt nur noch in Commvault – lokal nichts zu löschen" aria-label="Nicht auswählbar">';
    return `<tr data-k="${esc(k)}" class="${r.local ? "" : "cv-only"}"><td>${box}</td>
      <td><b>${esc(r.container)}</b></td><td>${r.created ? fmtTime(r.created) : esc(r.folder)}</td>
      <td class="right">${r.size_bytes ? fmtSize(r.size_bytes) : "–"}</td><td>${esc(parts.join(" · ") || "–")}</td>
      <td>${bCvCell(r)}</td>
      <td class="right"><button class="btn ghost sm" data-b-versions="${esc(r.container)}" title="Versionen in Commvault und lokal anzeigen, von dort wiederherstellen">${icon("restore")}Versionen</button></td></tr>`;
  }).join("") || `<tr><td colspan="7" class="empty">${esc(empty)}</td></tr>`;
  const sel = bState.rows.filter((r) => r.local && bState.sel.has(bKey(r)));
  $("#b-bulk").hidden = !sel.length;
  $("#b-count").textContent = `${sel.length} ausgewählt`;
  $("#b-sum").textContent = fmtSize(sel.reduce((a, r) => a + r.size_bytes, 0));
  const all = $("#b-selall");
  const selectable = rows.filter((r) => r.local);
  all.checked = selectable.length > 0 && selectable.every((r) => bState.sel.has(bKey(r)));
  all.disabled = !selectable.length;
}

async function deleteSelectedBackups() {
  const sel = bState.rows.filter((r) => r.local && bState.sel.has(bKey(r)));
  if (!sel.length) return;
  const unverified = sel.filter((r) => !r.in_target).length;
  const msg = `${sel.length} lokale Sicherung(en) (${fmtSize(sel.reduce((a, r) => a + r.size_bytes, 0))}) endgültig löschen?` +
    (unverified ? `\n\nACHTUNG: ${unverified} davon sind nicht als in Commvault gesichert bestätigt – sie sind danach evtl. nicht mehr wiederherstellbar.` : "") +
    "\n\nKopien in den Backup-Zielen bleiben unberührt und hier als „nur in …“ sichtbar.";
  if (!confirm(msg)) return;
  try {
    const res = await api("/api/backups/delete", { method: "POST", body: { items: sel.map((r) => ({ container: r.container, folder: r.folder })) } });
    toast(`${res.deleted} gelöscht, ${fmtSize(res.bytes)} frei` + (res.errors.length ? ` – Fehler: ${res.errors.join("; ")}` : ""), res.errors.length ? "error" : "");
    bState.sel.clear();
    loadBackups();
  } catch (e) { toast(e.message, "error"); }
}

$("#b-search").addEventListener("input", renderBackups);
$("#b-sort").addEventListener("change", renderBackups);
$("#b-refresh").addEventListener("click", loadBackups);
$("#b-clear").addEventListener("click", () => { bState.sel.clear(); renderBackups(); });
$("#b-delete").addEventListener("click", deleteSelectedBackups);
$("#b-selall").addEventListener("change", (e) => {
  bVisible().filter((r) => r.local).forEach((r) => (e.target.checked ? bState.sel.add(bKey(r)) : bState.sel.delete(bKey(r))));
  renderBackups();
});
$("#b-where").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-v]");
  if (!b) return;
  bState.where = b.dataset.v;
  store.set("b-where", bState.where);
  renderBackups();
});
$("#b-rows").addEventListener("click", (e) => {
  const b = e.target.closest("[data-b-versions]");
  if (b) openVersionsModal(b.dataset.bVersions);
});
$("#b-cvsync").addEventListener("click", async () => {
  const ready = activeTargets().filter((t) => t.ready && (t.capabilities.includes("versions") || t.capabilities.includes("state_check")));
  if (!ready.length) { toast("Kein Backup-Ziel bereit, das Stände auflisten kann.", "err"); return; }
  for (const t of ready) {
    try {
      const job = await api(`/api/targets/${t.id}/sync`, { method: "POST" });
      toast(`Abgleich mit ${t.label} gestartet (Job ${job.id}) – die Liste aktualisiert sich danach.`, "ok");
    } catch (e) { toast(e.message, "err"); }
  }
});
document.addEventListener("click", (e) => {
  const stkBackup = e.target.closest("[data-stk-backup]");
  if (stkBackup) stackBackup(stkBackup.dataset.stkBackup);
  const stkRuns = e.target.closest("[data-stk-runs]");
  if (stkRuns) toggleStackRuns(stkRuns.dataset.stkRuns);
  const stkRestore = e.target.closest("[data-stk-restore]");
  if (stkRestore) stackRestore(...stkRestore.dataset.stkRestore.split("|"));
  const save = e.target.closest("[data-cfg-save]");
  if (save) saveTargetConfig(save.dataset.cfgSave, !!save.dataset.test);
  const load = e.target.closest("[data-cfg-load]");
  if (load) loadChoices(...load.dataset.cfgLoad.split(":"));
  const b = e.target.closest("[data-target-test]");
  if (b) testTarget(b.dataset.targetTest, b.dataset.out);
  const go = e.target.closest("[data-goto-sec]");
  if (go) showSettingsSection(go.dataset.gotoSec);
  const nav = e.target.closest("#settings-nav button[data-sec]");
  if (nav) showSettingsSection(nav.dataset.sec);
});
$("#b-rows").addEventListener("change", (e) => {
  if (!e.target.classList.contains("b-sel")) return;
  const k = e.target.closest("tr").dataset.k;
  e.target.checked ? bState.sel.add(k) : bState.sel.delete(k);
  renderBackups();
});
