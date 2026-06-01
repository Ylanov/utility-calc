// extension/gisgmp-bridge/background.js
//
// Service worker MV3. Раз в 12 часов (или по кнопке из popup) под уже-
// залогиненной ЭЦП-сессией пользователя обходит раздел «Начисления» реестра
// ГИС ГМП, парсит все страницы и отправляет начисления в ЖКХ-биллинг.
//
// Ничего не хранит локально кроме настроек (URL ЖКХ + токен + URL реестра)
// и статуса последней синхронизации. Никаких действий в реестре — только GET.

import { extractCharges, isLoggedIn } from "./parser.js";

const ALARM = "gisgmp-sync";

const DEFAULT_SETTINGS = {
    jkh_url:      "https://asy-tk.ru",          // куда шлём долги
    jkh_token:    "",                            // GISGMP_SYNC_TOKEN из .env ЖКХ
    registry_url: "https://gisgmp.cgu.mchs.ru", // реестр ГИС ГМП
    period_hours: 12,                            // как часто синхронизировать
};

// Защита от бесконечной пагинации: 300 страниц × ~20 строк = ~6000 начислений.
const MAX_PAGES = 300;
// Пауза между страницами — не давим реестр.
const PAGE_SLEEP_MS = 400;


// ─── Settings / status ──────────────────────────────────────────────────────

async function getSettings() {
    const stored = await chrome.storage.local.get(
        ["jkh_url", "jkh_token", "registry_url", "period_hours"]);
    return { ...DEFAULT_SETTINGS, ...stored };
}

async function saveStatus(status) {
    await chrome.storage.local.set({
        last_status:    status,
        last_status_at: Date.now(),
    });
}

async function setBadge(text, color) {
    try {
        if (color) await chrome.action.setBadgeBackgroundColor({ color });
        await chrome.action.setBadgeText({ text: text || "" });
    } catch { /* нет иконки — не критично */ }
}


// ─── Lifecycle ────────────────────────────────────────────────────────────────

async function ensureAlarm() {
    const s = await getSettings();
    const minutes = Math.max(60, (s.period_hours || 12) * 60);
    chrome.alarms.create(ALARM, {
        periodInMinutes: minutes,
        delayInMinutes:  1,   // первый прогон через минуту после старта
    });
}

chrome.runtime.onInstalled.addListener(() => { ensureAlarm(); });
chrome.runtime.onStartup.addListener(() => { ensureAlarm(); });

chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name === ALARM) {
        syncNow().catch(err => console.error("[gisgmp-bridge] sync failed:", err));
    }
});

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg?.type === "sync_now") {
        syncNow().then(r => sendResponse({ ok: true, ...r }))
                 .catch(e => sendResponse({ ok: false, error: String(e?.message || e) }));
        return true;   // async
    }
    if (msg?.type === "get_status") {
        chrome.storage.local.get(["last_status", "last_status_at", "last_result"])
              .then(sendResponse);
        return true;
    }
});


// ─── Core ──────────────────────────────────────────────────────────────────────

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

async function fetchChargePage(registry, page) {
    const url = `${registry.replace(/\/+$/, "")}/charge/?page=${page}`;
    const r = await fetch(url, {
        method:      "GET",
        credentials: "include",     // подставит cookie ЭЦП-сессии реестра
        redirect:    "follow",
        headers:     { "Accept": "text/html,application/xhtml+xml" },
        cache:       "no-store",
    });
    if (!r.ok) throw new Error(`реестр ${url} → ${r.status}`);
    return await r.text();
}

async function collectAllCharges(registry) {
    // Первая страница — заодно проверяем залогиненность.
    const firstHtml = await fetchChargePage(registry, 1);
    if (!isLoggedIn(firstHtml)) {
        return { loggedIn: false, charges: [] };
    }

    const all = [];
    let page = 1;
    let html = firstHtml;
    while (page <= MAX_PAGES) {
        const rows = extractCharges(html);
        if (rows.length === 0) break;          // дошли до конца пагинации
        all.push(...rows);
        page += 1;
        await sleep(PAGE_SLEEP_MS);
        try {
            html = await fetchChargePage(registry, page);
        } catch (err) {
            console.warn("[gisgmp-bridge] page", page, "failed:", err);
            break;
        }
        if (!isLoggedIn(html)) break;          // сессия отвалилась посреди обхода
    }
    return { loggedIn: true, charges: all };
}

async function syncNow() {
    const settings = await getSettings();

    if (!settings.jkh_token) {
        await saveStatus({ kind: "config", message: "Не указан токен ЖКХ (см. настройки расширения)." });
        await setBadge("!", "#b85450");
        return { sent: false, reason: "no_token" };
    }

    // 1. Тянем все начисления из реестра.
    let collected;
    try {
        collected = await collectAllCharges(settings.registry_url);
    } catch (err) {
        await saveStatus({ kind: "error", message: `Реестр недоступен: ${err.message}` });
        await setBadge("!", "#b85450");
        return { sent: false, reason: "registry_failed" };
    }

    if (!collected.loggedIn) {
        await saveStatus({
            kind: "auth",
            message: "Не залогинены в ГИС ГМП — откройте gisgmp.cgu.mchs.ru и войдите по ЭЦП.",
        });
        await setBadge("⏸", "#b85450");
        return { sent: false, reason: "not_logged_in" };
    }

    if (collected.charges.length === 0) {
        await saveStatus({ kind: "warn", message: "Реестр открылся, но начислений не найдено." });
        await setBadge("0");
        return { sent: false, reason: "empty" };
    }

    // 2. Отправляем в ЖКХ.
    let resp;
    try {
        resp = await fetch(`${settings.jkh_url.replace(/\/+$/, "")}/api/financier/gisgmp/sync`, {
            method:  "POST",
            headers: {
                "Content-Type":  "application/json",
                "Authorization": `Bearer ${settings.jkh_token}`,
            },
            body: JSON.stringify({ charges: collected.charges }),
        });
    } catch (err) {
        await saveStatus({ kind: "error", message: `ЖКХ недоступен: ${err.message}` });
        await setBadge("!", "#b85450");
        return { sent: false, reason: "jkh_unreachable" };
    }

    if (resp.status === 401 || resp.status === 403) {
        await saveStatus({ kind: "auth_jkh", message: `ЖКХ ответил ${resp.status}. Проверьте токен GISGMP_SYNC_TOKEN.` });
        await setBadge("!", "#b85450");
        return { sent: false, reason: "jkh_auth" };
    }
    if (!resp.ok) {
        const txt = await resp.text().catch(() => "");
        await saveStatus({ kind: "error", message: `ЖКХ: HTTP ${resp.status} ${txt.slice(0, 200)}` });
        await setBadge("!", "#b85450");
        return { sent: false, reason: "jkh_error" };
    }

    const result = await resp.json().catch(() => ({}));
    await chrome.storage.local.set({ last_result: result });
    await saveStatus({
        kind: "ok",
        message: `Отправлено начислений: ${collected.charges.length}. `
               + `Жильцов обновлено: ${result.updated ?? "?"}, создано: ${result.created ?? "?"}, `
               + `не найдено: ${(result.not_found_209 ?? 0) + (result.not_found_205 ?? 0)}.`,
    });
    await setBadge("✓", "#5a9");
    return { sent: true, charges: collected.charges.length, result };
}
