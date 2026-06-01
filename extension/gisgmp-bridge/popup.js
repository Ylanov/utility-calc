// extension/gisgmp-bridge/popup.js
function $(id) { return document.getElementById(id); }

function show(text, cls = "") {
    const el = $("status");
    el.textContent = text;
    el.className = cls;
}

async function render() {
    const { last_status, last_status_at } = await chrome.storage.local.get(
        ["last_status", "last_status_at"]);
    if (!last_status) { show("Ещё не синхронизировано."); return; }
    const when = last_status_at ? new Date(last_status_at).toLocaleString("ru-RU") : "";
    const cls = last_status.kind === "ok" ? "ok"
        : (last_status.kind === "config" || last_status.kind === "warn" ? "" : "err");
    show(`${last_status.message}\n${when}`, cls);
}

$("syncNow").addEventListener("click", () => {
    show("Синхронизация…");
    chrome.runtime.sendMessage({ type: "sync_now" }, () => {
        if (chrome.runtime.lastError) {
            show("Ошибка: " + chrome.runtime.lastError.message, "err");
            return;
        }
        render();
    });
});

$("openOptions").addEventListener("click", () => chrome.runtime.openOptionsPage());

render();
