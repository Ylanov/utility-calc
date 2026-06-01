// extension/gisgmp-bridge/options.js
// Чтение/сохранение настроек + ручной запуск синхронизации.

const FIELDS = ["jkh_url", "jkh_token", "registry_url", "period_hours"];
const DEFAULTS = {
    jkh_url: "https://asy-tk.ru",
    registry_url: "https://gisgmp.cgu.mchs.ru",
    period_hours: 12,
};

function $(id) { return document.getElementById(id); }

async function load() {
    const stored = await chrome.storage.local.get(FIELDS);
    for (const f of FIELDS) {
        $(f).value = stored[f] ?? DEFAULTS[f] ?? "";
    }
    renderStatus();
}

async function save() {
    const data = {
        jkh_url:      $("jkh_url").value.trim() || DEFAULTS.jkh_url,
        jkh_token:    $("jkh_token").value.trim(),
        registry_url: $("registry_url").value.trim() || DEFAULTS.registry_url,
        period_hours: Math.max(1, parseInt($("period_hours").value, 10) || 12),
    };
    await chrome.storage.local.set(data);
    // Пересоздаём alarm с новым периодом.
    const minutes = Math.max(60, data.period_hours * 60);
    chrome.alarms.create("gisgmp-sync", { periodInMinutes: minutes, delayInMinutes: 1 });
    showStatus("Сохранено.", "ok");
}

async function syncNow() {
    showStatus("Синхронизация…");
    chrome.runtime.sendMessage({ type: "sync_now" }, (resp) => {
        if (chrome.runtime.lastError) {
            showStatus("Ошибка: " + chrome.runtime.lastError.message, "err");
            return;
        }
        renderStatus();
    });
}

async function renderStatus() {
    const { last_status, last_status_at } = await chrome.storage.local.get(
        ["last_status", "last_status_at"]);
    if (!last_status) { showStatus("Ещё не синхронизировано."); return; }
    const when = last_status_at ? new Date(last_status_at).toLocaleString("ru-RU") : "";
    const cls = last_status.kind === "ok" ? "ok" : (last_status.kind === "config" || last_status.kind === "warn" ? "" : "err");
    showStatus(`${last_status.message}\n${when}`, cls);
}

function showStatus(text, cls = "") {
    const el = $("status");
    el.textContent = text;
    el.className = cls;
}

$("save").addEventListener("click", save);
$("syncNow").addEventListener("click", syncNow);
load();
