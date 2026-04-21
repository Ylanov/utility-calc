/**
 * МОДУЛЬ: ИНВЕНТАРИЗАЦИЯ
 *
 * Работает с endpoints /api/arsenal/inventory/*.
 * Основной поток:
 *   1. Оператор выбирает склад → POST /inventory (начало).
 *   2. Сканирует серийник → POST /quick-scan.
 *   3. Раз в 2 секунды (polling) обновляет report + pending.
 *   4. При закрытии → POST /close c опциональным auto_correct.
 *
 * Фокус всегда на поле ввода — оператор сканирует поточно без мыши.
 */

const API_BASE = '/api/arsenal';
let currentInventory = null;     // {id, object_id, ...} пока идёт
let pollTimer = null;
let scanHistory = [];            // для ленты

const closeModalEl = document.getElementById('closeModal');

function flash(el, type) {
    el.classList.remove('flash-ok', 'flash-warn', 'flash-err');
    void el.offsetWidth;                          // force reflow для повторного анимирования
    el.classList.add({ ok: 'flash-ok', warn: 'flash-warn', err: 'flash-err' }[type] || 'flash-ok');
}

async function api(path, opts = {}) {
    const res = await apiFetch(API_BASE + path, opts);
    if (!res) return null;
    if (res.status === 204) return {};
    try {
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || `Ошибка ${res.status}`);
        return data;
    } catch (e) {
        if (!res.ok) throw new Error(`Ошибка ${res.status}`);
        throw e;
    }
}

// ============================================================
// ЗАГРУЗКА СКЛАДОВ И НОМЕНКЛАТУР
// ============================================================
async function loadObjects() {
    try {
        const data = await api('/objects');
        const sel = document.getElementById('objectSelect');
        sel.innerHTML = '<option value="">Выберите склад…</option>' +
            (data || []).map(o => `<option value="${o.id}">${o.name}${o.obj_type ? ' · ' + o.obj_type : ''}</option>`).join('');
    } catch (e) {
        UI.showToast('Не удалось загрузить список объектов: ' + e.message, 'error');
    }
}

async function loadNomenclature() {
    try {
        const data = await api('/nomenclature?limit=500');
        const items = data.items || data || [];
        const sel = document.getElementById('batchNom');
        sel.innerHTML = '<option value="">Выберите номенклатуру…</option>' +
            items.filter(n => !n.is_numbered)
                 .map(n => `<option value="${n.id}">${n.name}</option>`).join('');
    } catch (e) {
        // Не критично — партионный ввод можно не использовать
    }
}

async function loadOpenInventories() {
    try {
        const data = await api('/inventory?status=open');
        const host = document.getElementById('openList');
        if (!data || !data.length) {
            host.innerHTML = '<div class="text-slate-400 text-xs italic">Нет открытых инвентаризаций.</div>';
            return;
        }
        host.innerHTML = data.map(i => `
            <div class="flex items-center justify-between bg-amber-50 border border-amber-200 rounded-lg px-3 py-2 text-sm">
                <div>
                    <b>Инв. #${i.id}</b> · объект ${i.object_id}
                    <span class="text-xs text-slate-500">начата ${new Date(i.started_at).toLocaleString('ru-RU')}</span>
                </div>
                <button onclick="resumeInventory(${i.id})" class="bg-rose-600 text-white px-3 py-1 rounded text-xs">
                    <i class="fa-solid fa-play"></i> Продолжить
                </button>
            </div>
        `).join('');
    } catch (e) {
        /* ignore */
    }
}

async function loadReasons() {
    try {
        const data = await api('/disposal-reasons');
        const sel = document.getElementById('reasonSelect');
        sel.innerHTML = '<option value="">По умолчанию (LOST — утрата)</option>' +
            (data || []).map(r => `<option value="${r.id}">${r.name} (${r.code})</option>`).join('');
    } catch (e) {
        // nothing
    }
}

// ============================================================
// СТАРТ / ВОЗОБНОВЛЕНИЕ / ЗАКРЫТИЕ
// ============================================================
async function startInventory() {
    const objId = parseInt(document.getElementById('objectSelect').value);
    if (!objId) return UI.showToast('Выберите объект', 'error');
    try {
        const inv = await api('/inventory', {
            method: 'POST',
            body: JSON.stringify({ object_id: objId }),
        });
        currentInventory = inv;
        enterActiveMode();
    } catch (e) {
        UI.showToast(e.message, 'error');
    }
}

async function resumeInventory(id) {
    // У нас нет отдельного GET /inventory/{id}, но есть /report — достаточно.
    try {
        const r = await api(`/inventory/${id}/report`);
        currentInventory = r.inventory;
        enterActiveMode();
    } catch (e) {
        UI.showToast(e.message, 'error');
    }
}

function enterActiveMode() {
    document.getElementById('startCard').classList.add('hidden');
    document.getElementById('activeCard').classList.remove('hidden');
    document.getElementById('btnCancel').classList.remove('hidden');
    document.getElementById('btnClose').classList.remove('hidden');
    document.getElementById('invSubtitle').textContent =
        `Инв. #${currentInventory.id} · объект ${currentInventory.object_id} · начата ${new Date(currentInventory.started_at).toLocaleString('ru-RU')}`;
    document.getElementById('btnExportXlsx').href =
        `${API_BASE}/inventory/${currentInventory.id}/export`;

    // Стартуем поллинг отчёта раз в 2 сек (совпадает с ленивой записью)
    refresh();
    pollTimer = setInterval(refresh, 2500);
    // Фокусируем поле сканирования
    document.getElementById('scanInput').focus();
}

async function refresh() {
    if (!currentInventory) return;
    try {
        const [report, pending] = await Promise.all([
            api(`/inventory/${currentInventory.id}/report`),
            api(`/inventory/${currentInventory.id}/pending`),
        ]);
        renderProgress(report);
        renderPending(pending);
        renderReport(report);
        if (report.inventory.status !== 'open') {
            // Кто-то другой закрыл — выходим
            clearInterval(pollTimer);
            UI.showToast(`Инвентаризация переведена в статус ${report.inventory.status}`, 'info');
        }
    } catch (e) {
        // тихо игнорируем периодические неудачи
    }
}

function renderProgress(r) {
    const s = r.summary;
    document.getElementById('progPct').textContent = s.progress_pct || 0;
    document.getElementById('statFound').textContent = s.total_found_units;
    document.getElementById('statExpected').textContent = s.total_expected_units;
    document.getElementById('statPending').textContent = s.missing_count + s.surplus_count;
    document.getElementById('statSpeed').textContent = s.scans_per_minute ?? '—';
    document.getElementById('progBar').style.width = (s.progress_pct || 0) + '%';
}

function renderPending(p) {
    const tbody = document.getElementById('pendingBody');
    document.getElementById('pendingCount').textContent = p.pending_count;
    const filter = document.getElementById('pendingSearch').value.toLowerCase();
    const list = (p.pending || []).filter(x =>
        !filter ||
        (x.name || '').toLowerCase().includes(filter) ||
        (x.serial_number || '').toLowerCase().includes(filter)
    );
    if (!list.length) {
        tbody.innerHTML = `<tr><td colspan="3" class="text-center text-slate-400 py-6">
            ${filter ? 'Ничего по фильтру' : 'Всё найдено 🎉'}</td></tr>`;
        return;
    }
    tbody.innerHTML = list.map(x => `
        <tr class="border-b border-slate-100 hover:bg-slate-50">
            <td class="px-3 py-2">${x.name}</td>
            <td class="px-3 py-2 font-mono text-xs">${x.serial_number || '—'}</td>
            <td class="px-3 py-2 text-right">${x.remaining_quantity ?? x.expected_quantity}</td>
        </tr>
    `).join('');
}

function renderReport(r) {
    const host = document.getElementById('reportBlock');
    const row = (x, extra = '') => `
        <div class="flex justify-between border-b border-slate-100 py-1">
            <div>
                <b>${x.name}</b>
                ${x.serial_number ? `· <span class="font-mono text-xs">${x.serial_number}</span>` : ''}
            </div>
            <div class="text-slate-700">${extra}</div>
        </div>`;
    host.innerHTML = `
        <div>
            <div class="text-sm font-semibold text-emerald-700 mb-1">
                <i class="fa-solid fa-check"></i> Совпадения · ${r.matched.length}
            </div>
            <div class="pl-3">${r.matched.slice(0, 20).map(x => row(x, `×${x.found_quantity}`)).join('')}</div>
            ${r.matched.length > 20 ? `<div class="text-xs text-slate-500 pl-3">...и ещё ${r.matched.length - 20}</div>` : ''}
        </div>
        <div>
            <div class="text-sm font-semibold text-rose-700 mb-1">
                <i class="fa-solid fa-triangle-exclamation"></i> Недостача · ${r.missing.length}
            </div>
            <div class="pl-3">${r.missing.map(x => row(x, `дефицит: ${x.deficit}`)).join('') || '<div class="text-xs text-slate-400 italic pl-3">нет</div>'}</div>
        </div>
        <div>
            <div class="text-sm font-semibold text-amber-700 mb-1">
                <i class="fa-solid fa-plus"></i> Излишек · ${r.surplus.length}
            </div>
            <div class="pl-3">${r.surplus.map(x => row(x, `избыток: ${x.excess}`)).join('') || '<div class="text-xs text-slate-400 italic pl-3">нет</div>'}</div>
        </div>`;
}

// ============================================================
// СКАНИРОВАНИЕ
// ============================================================
async function handleScan() {
    const input = document.getElementById('scanInput');
    const serial = input.value.trim();
    if (!serial || !currentInventory) return;
    input.value = '';
    const lastBox = document.getElementById('lastScan');
    lastBox.classList.remove('hidden');

    try {
        const res = await api(`/inventory/${currentInventory.id}/quick-scan`, {
            method: 'POST',
            body: JSON.stringify({ serial_number: serial }),
        });
        let flashType = 'ok';
        let statusText = '';
        let icon = 'fa-check-circle text-emerald-600';
        if (!res.saved) {
            // Серийник не нашёлся в реестре — излишек, серверу не удалось автоматически сохранить (без nomenclature_id)
            flashType = 'err';
            statusText = res.warning || 'Не найден в реестре';
            icon = 'fa-triangle-exclamation text-rose-600';
        } else if (res.warning) {
            flashType = 'warn';
            statusText = res.warning;
            icon = 'fa-triangle-exclamation text-amber-600';
        } else {
            statusText = `✓ ${res.nomenclature_name}`;
        }
        lastBox.innerHTML = `
            <div class="flex items-center gap-3">
                <i class="fa-solid ${icon} text-xl"></i>
                <div>
                    <div class="font-mono text-sm">${serial}</div>
                    <div class="text-xs text-slate-600">${statusText}</div>
                </div>
            </div>`;
        flash(lastBox, flashType);
        pushScanFeed(serial, res.nomenclature_name || '?', flashType, statusText);
        refresh();
    } catch (e) {
        // 409 duplicate, 400 validation, etc.
        lastBox.innerHTML = `
            <div class="flex items-center gap-3">
                <i class="fa-solid fa-ban text-rose-600 text-xl"></i>
                <div>
                    <div class="font-mono text-sm">${serial}</div>
                    <div class="text-xs text-rose-700">${e.message}</div>
                </div>
            </div>`;
        flash(lastBox, 'err');
        pushScanFeed(serial, '—', 'err', e.message);
    } finally {
        input.focus();
    }
}

function pushScanFeed(serial, name, type, note) {
    scanHistory.unshift({ serial, name, type, note, at: new Date() });
    if (scanHistory.length > 200) scanHistory.pop();
    const host = document.getElementById('scanFeed');
    document.getElementById('scanCount').textContent = scanHistory.length;
    const cls = { ok: 'bg-emerald-50 text-emerald-900', warn: 'bg-amber-50 text-amber-900', err: 'bg-rose-50 text-rose-900' }[type];
    host.innerHTML = scanHistory.slice(0, 50).map(s => `
        <div class="${cls} rounded px-2 py-1 flex justify-between gap-2">
            <span class="font-mono truncate">${s.serial}</span>
            <span class="truncate">${s.name}</span>
            <span class="text-slate-500">${s.at.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</span>
        </div>
    `).join('');
}

async function handleBatchAdd() {
    const nomId = parseInt(document.getElementById('batchNom').value);
    const serial = document.getElementById('batchSerial').value.trim();
    const qty = parseInt(document.getElementById('batchQty').value) || 1;
    if (!nomId) return UI.showToast('Выберите номенклатуру', 'error');
    try {
        await api(`/inventory/${currentInventory.id}/scan`, {
            method: 'POST',
            body: JSON.stringify({
                nomenclature_id: nomId,
                serial_number: serial || null,
                found_quantity: qty,
            }),
        });
        UI.showToast(`+${qty} добавлено`, 'success');
        refresh();
    } catch (e) {
        UI.showToast(e.message, 'error');
    }
}

// ============================================================
// ЗАКРЫТИЕ / ОТМЕНА
// ============================================================
function openCloseModal() {
    // Достаём текущую сводку для текста
    api(`/inventory/${currentInventory.id}/report`).then(r => {
        const s = r.summary;
        document.getElementById('closeStats').innerHTML = `
            Совпадений: <b>${s.matched_count}</b> · Недостача: <b class="text-rose-700">${s.missing_count}</b> ·
            Излишек: <b class="text-amber-700">${s.surplus_count}</b>`;
    });
    closeModalEl.style.display = 'flex';
}

document.getElementById('chkAutoCorrect').addEventListener('change', (e) => {
    document.getElementById('reasonBlock').classList.toggle('hidden', !e.target.checked);
});

async function confirmClose() {
    const auto = document.getElementById('chkAutoCorrect').checked;
    const reasonId = parseInt(document.getElementById('reasonSelect').value) || null;
    const note = document.getElementById('closeNote').value.trim() || null;
    try {
        const res = await api(`/inventory/${currentInventory.id}/close`, {
            method: 'POST',
            body: JSON.stringify({ auto_correct: auto, disposal_reason_id: reasonId, note }),
        });
        const msg = res.correction
            ? `Инвентаризация закрыта. Корректирующие документы созданы (doc #${res.correction.correction_document_id}).`
            : 'Инвентаризация закрыта.';
        UI.showToast(msg, 'success');
        closeModalEl.style.display = 'none';
        clearInterval(pollTimer);
        setTimeout(() => window.location.reload(), 1200);
    } catch (e) {
        UI.showToast(e.message, 'error');
    }
}

async function cancelInventory() {
    if (!confirm('Отменить инвентаризацию? Все сканирования будут проигнорированы.')) return;
    try {
        await api(`/inventory/${currentInventory.id}/cancel`, { method: 'POST' });
        UI.showToast('Инвентаризация отменена', 'info');
        clearInterval(pollTimer);
        setTimeout(() => window.location.reload(), 900);
    } catch (e) {
        UI.showToast(e.message, 'error');
    }
}

// ============================================================
// INIT
// ============================================================
document.addEventListener('DOMContentLoaded', () => {
    loadObjects();
    loadNomenclature();
    loadOpenInventories();
    loadReasons();

    document.getElementById('btnStart').addEventListener('click', startInventory);
    document.getElementById('btnClose').addEventListener('click', openCloseModal);
    document.getElementById('btnCancel').addEventListener('click', cancelInventory);
    document.getElementById('btnBatchAdd').addEventListener('click', handleBatchAdd);
    document.getElementById('btnConfirmClose').addEventListener('click', confirmClose);

    const input = document.getElementById('scanInput');
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            handleScan();
        }
    });
    // Вернуть фокус на инпут при клике мимо — чтобы сканер продолжал работать
    document.addEventListener('click', (e) => {
        if (!currentInventory) return;
        const t = e.target;
        if (t.matches('input, select, textarea, button')) return;
        if (t.closest('details, #closeModal')) return;
        input.focus();
    });
    document.getElementById('pendingSearch').addEventListener('input', refresh);
});
