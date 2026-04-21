/**
 * МОДУЛЬ: Отчёты и аналитика.
 *
 * API:
 *   GET /reports/search-weapon?q=...
 *   GET /reports/timeline?weapon_id=...
 *   GET /reports/balance-summary[?object_id=...]
 *   GET /reports/balance-summary/export  → Excel
 *   GET /reports/by-mol
 *   GET /reports/turnover?date_from&date_to[&object_id]
 *   GET /reports/top-moving?days=...
 *   GET /objects
 */
const API = '/api/arsenal';

async function api(path, opts = {}) {
    const res = await apiFetch(API + path, opts);
    if (!res) return null;
    if (res.status === 204) return {};
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `Ошибка ${res.status}`);
    return data;
}

function fmtMoney(v) {
    return Number(v || 0).toLocaleString('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + ' ₽';
}
function fmtDate(iso) {
    if (!iso) return '—';
    try { return new Date(iso).toLocaleString('ru-RU', { day:'2-digit', month:'2-digit', year:'2-digit', hour:'2-digit', minute:'2-digit' }); }
    catch { return iso; }
}
function escapeHtml(s) {
    if (s == null) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// =====================================================================
// ТАБЫ
// =====================================================================
document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        const tab = btn.dataset.tab;
        document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b === btn));
        document.querySelectorAll('.tab-pane').forEach(p =>
            p.classList.toggle('hidden', p.id !== 'pane-' + tab)
        );
    });
});

// =====================================================================
// Загрузка списка объектов для селекторов (общая инициализация)
// =====================================================================
async function loadObjects() {
    try {
        const data = await api('/objects');
        const opts = '<option value="">Все объекты</option>'
            + (data || []).map(o => `<option value="${o.id}">${escapeHtml(o.name)}</option>`).join('');
        ['balObject', 'turnObject'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.innerHTML = opts;
        });
    } catch (e) {
        UI.showToast('Не удалось загрузить список объектов: ' + e.message, 'error');
    }
}

// =====================================================================
// БАЛАНСОВАЯ ВЕДОМОСТЬ
// =====================================================================
async function loadBalance() {
    const objId = document.getElementById('balObject').value;
    const params = objId ? '?object_id=' + objId : '';
    document.getElementById('btnExportBalance').href = API + '/reports/balance-summary/export' + params;
    const host = document.getElementById('balanceBody');
    host.innerHTML = '<div class="bg-white rounded-xl shadow-sm p-8 text-center text-slate-400"><i class="fa-solid fa-spinner fa-spin text-3xl"></i></div>';
    try {
        const data = await api('/reports/balance-summary' + params);
        renderBalance(data);
    } catch (e) {
        host.innerHTML = `<div class="bg-white rounded-xl shadow-sm p-6 text-rose-600">Ошибка: ${escapeHtml(e.message)}</div>`;
    }
}

function renderBalance(data) {
    const host = document.getElementById('balanceBody');
    const accounts = Object.keys(data.by_account || {});
    if (!accounts.length) {
        host.innerHTML = '<div class="bg-white rounded-xl shadow-sm p-8 text-center text-slate-400">Нет активных остатков.</div>';
        return;
    }
    const summary = `
        <div class="grid grid-cols-2 md:grid-cols-3 gap-3">
            <div class="bg-white rounded-lg shadow-sm p-4">
                <div class="text-xs text-slate-500 uppercase">Счетов</div>
                <div class="text-2xl font-bold">${accounts.length}</div>
            </div>
            <div class="bg-white rounded-lg shadow-sm p-4">
                <div class="text-xs text-slate-500 uppercase">Единиц</div>
                <div class="text-2xl font-bold text-blue-600">${data.grand_total_units}</div>
            </div>
            <div class="bg-white rounded-lg shadow-sm p-4">
                <div class="text-xs text-slate-500 uppercase">Итого стоимость</div>
                <div class="text-2xl font-bold text-emerald-600">${fmtMoney(data.grand_total_cost)}</div>
            </div>
        </div>`;
    const blocks = accounts.map(acc => {
        const b = data.by_account[acc];
        const rows = b.items.map(it => `
            <tr class="border-b border-slate-100">
                <td class="px-3 py-2">${escapeHtml(it.category)}</td>
                <td class="px-3 py-2">${escapeHtml(it.object_name)}</td>
                <td class="px-3 py-2 text-right">${it.units}</td>
                <td class="px-3 py-2 text-right">${it.quantity}</td>
                <td class="px-3 py-2 text-right font-mono">${fmtMoney(it.cost)}</td>
            </tr>`).join('');
        return `
            <div class="bg-white rounded-xl shadow-sm overflow-hidden">
                <div class="flex justify-between items-center px-4 py-3 bg-blue-50 border-b border-blue-200">
                    <div class="font-semibold text-blue-900">Счёт: ${escapeHtml(acc)}</div>
                    <div class="text-sm text-blue-900">
                        ${b.total_units} ед · <b>${fmtMoney(b.total_cost)}</b>
                    </div>
                </div>
                <table class="w-full text-sm">
                    <thead class="bg-slate-50 text-xs text-slate-500 uppercase">
                        <tr>
                            <th class="px-3 py-2 text-left">Категория</th>
                            <th class="px-3 py-2 text-left">Объект</th>
                            <th class="px-3 py-2 text-right">Ед.</th>
                            <th class="px-3 py-2 text-right">Кол-во</th>
                            <th class="px-3 py-2 text-right">Стоимость</th>
                        </tr>
                    </thead>
                    <tbody>${rows}</tbody>
                </table>
            </div>`;
    }).join('');
    host.innerHTML = summary + '<div class="space-y-4">' + blocks + '</div>';
}

document.getElementById('btnLoadBalance').addEventListener('click', loadBalance);

// =====================================================================
// ПО МОЛ
// =====================================================================
async function loadMol() {
    const host = document.getElementById('molBody');
    host.innerHTML = '<div class="bg-white rounded-xl shadow-sm p-8 text-center text-slate-400"><i class="fa-solid fa-spinner fa-spin text-3xl"></i></div>';
    try {
        const data = await api('/reports/by-mol');
        renderMol(data);
    } catch (e) {
        host.innerHTML = `<div class="bg-white rounded-xl shadow-sm p-6 text-rose-600">Ошибка: ${escapeHtml(e.message)}</div>`;
    }
}

function renderMol(data) {
    const host = document.getElementById('molBody');
    const mols = Object.keys(data.by_mol || {});
    if (!mols.length) {
        host.innerHTML = '<div class="bg-white rounded-xl shadow-sm p-8 text-center text-slate-400">МОЛ не назначены.</div>';
        return;
    }
    host.innerHTML = mols.map(mol => {
        const b = data.by_mol[mol];
        const rows = b.objects.map(o => `
            <tr class="border-b border-slate-100">
                <td class="px-3 py-2">${escapeHtml(o.object_name)}</td>
                <td class="px-3 py-2 text-right">${o.units}</td>
                <td class="px-3 py-2 text-right">${o.quantity}</td>
                <td class="px-3 py-2 text-right font-mono">${fmtMoney(o.cost)}</td>
            </tr>`).join('');
        return `
            <div class="bg-white rounded-xl shadow-sm overflow-hidden">
                <div class="flex justify-between items-center px-4 py-3 bg-purple-50 border-b border-purple-200">
                    <div class="font-semibold text-purple-900">
                        <i class="fa-solid fa-user-tie"></i> ${escapeHtml(mol)}
                    </div>
                    <div class="text-sm text-purple-900">
                        ${b.total_units} ед · <b>${fmtMoney(b.total_cost)}</b>
                    </div>
                </div>
                <table class="w-full text-sm">
                    <thead class="bg-slate-50 text-xs text-slate-500 uppercase">
                        <tr>
                            <th class="px-3 py-2 text-left">Объект</th>
                            <th class="px-3 py-2 text-right">Ед.</th>
                            <th class="px-3 py-2 text-right">Кол-во</th>
                            <th class="px-3 py-2 text-right">Стоимость</th>
                        </tr>
                    </thead>
                    <tbody>${rows}</tbody>
                </table>
            </div>`;
    }).join('');
}

document.getElementById('btnLoadMol').addEventListener('click', loadMol);

// =====================================================================
// ОБОРОТ
// =====================================================================
// Дефолт: последний месяц
const today = new Date();
const monthAgo = new Date(today); monthAgo.setDate(today.getDate() - 30);
document.getElementById('turnFrom').value = monthAgo.toISOString().slice(0, 10);
document.getElementById('turnTo').value = today.toISOString().slice(0, 10);

async function loadTurnover() {
    const from = document.getElementById('turnFrom').value;
    const to = document.getElementById('turnTo').value;
    const obj = document.getElementById('turnObject').value;
    if (!from || !to) return UI.showToast('Укажите период', 'error');
    const params = new URLSearchParams({
        date_from: from + 'T00:00:00',
        date_to: to + 'T23:59:59',
    });
    if (obj) params.set('object_id', obj);
    const host = document.getElementById('turnoverBody');
    host.innerHTML = '<div class="bg-white rounded-xl shadow-sm p-8 text-center text-slate-400"><i class="fa-solid fa-spinner fa-spin text-3xl"></i></div>';
    try {
        const data = await api('/reports/turnover?' + params);
        renderTurnover(data);
    } catch (e) {
        host.innerHTML = `<div class="bg-white rounded-xl shadow-sm p-6 text-rose-600">Ошибка: ${escapeHtml(e.message)}</div>`;
    }
}

function renderTurnover(data) {
    const host = document.getElementById('turnoverBody');
    const ops = Object.keys(data.by_operation || {});
    const s = data.summary;
    if (!ops.length) {
        host.innerHTML = '<div class="bg-white rounded-xl shadow-sm p-8 text-center text-slate-400">За этот период документов не было.</div>';
        return;
    }
    const netClass = s.net_cost >= 0 ? 'text-emerald-600' : 'text-rose-600';
    const cards = `
        <div class="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
            <div class="bg-white rounded-lg shadow-sm p-4">
                <div class="text-xs text-slate-500 uppercase">Документов</div>
                <div class="text-2xl font-bold">${s.total_docs}</div>
            </div>
            <div class="bg-white rounded-lg shadow-sm p-4">
                <div class="text-xs text-slate-500 uppercase">Приход, ₽</div>
                <div class="text-xl font-bold text-emerald-600">${fmtMoney(s.inbound_cost)}</div>
            </div>
            <div class="bg-white rounded-lg shadow-sm p-4">
                <div class="text-xs text-slate-500 uppercase">Расход, ₽</div>
                <div class="text-xl font-bold text-rose-600">${fmtMoney(s.outbound_cost)}</div>
            </div>
            <div class="bg-white rounded-lg shadow-sm p-4">
                <div class="text-xs text-slate-500 uppercase">Сальдо</div>
                <div class="text-xl font-bold ${netClass}">${fmtMoney(s.net_cost)}</div>
            </div>
        </div>`;
    const rows = ops.map(op => {
        const b = data.by_operation[op];
        return `
            <tr class="border-b border-slate-100">
                <td class="px-3 py-2">${escapeHtml(op)}</td>
                <td class="px-3 py-2 text-right">${b.docs}</td>
                <td class="px-3 py-2 text-right">${b.quantity}</td>
                <td class="px-3 py-2 text-right font-mono">${fmtMoney(b.cost)}</td>
            </tr>`;
    }).join('');
    host.innerHTML = cards + `
        <div class="bg-white rounded-xl shadow-sm overflow-hidden">
            <table class="w-full text-sm">
                <thead class="bg-slate-50 text-xs text-slate-500 uppercase">
                    <tr>
                        <th class="px-3 py-2 text-left">Операция</th>
                        <th class="px-3 py-2 text-right">Документов</th>
                        <th class="px-3 py-2 text-right">Количество</th>
                        <th class="px-3 py-2 text-right">Стоимость</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        </div>`;
}

document.getElementById('btnLoadTurnover').addEventListener('click', loadTurnover);

// =====================================================================
// ТОП ПЕРЕМЕЩАЮЩИХСЯ
// =====================================================================
async function loadTop() {
    const days = document.getElementById('topDays').value;
    const host = document.getElementById('topBody');
    host.innerHTML = '<div class="text-center py-10 text-slate-400"><i class="fa-solid fa-spinner fa-spin text-3xl"></i></div>';
    try {
        const data = await api('/reports/top-moving?days=' + days + '&limit=20');
        const items = data.items || [];
        if (!items.length) {
            host.innerHTML = '<div class="text-center py-10 text-slate-400">За выбранный период движений не было.</div>';
            return;
        }
        const max = items[0].movements;
        host.innerHTML = items.map((it, i) => {
            const pct = Math.round((it.movements / max) * 100);
            return `
                <div class="flex items-center gap-3 py-2 border-b border-slate-100 last:border-0">
                    <div class="w-8 text-center font-bold text-slate-400">${i + 1}</div>
                    <div class="flex-1 min-w-0">
                        <div class="font-medium truncate">${escapeHtml(it.name)}</div>
                        <div class="text-xs text-slate-500">${escapeHtml(it.category || 'Без категории')}</div>
                    </div>
                    <div class="w-52 bg-slate-100 rounded-full h-2 overflow-hidden">
                        <div class="bg-purple-500 h-2" style="width:${pct}%"></div>
                    </div>
                    <div class="w-24 text-right text-sm">
                        <span class="font-bold">${it.movements}</span>
                        <span class="text-slate-400"> движ.</span>
                    </div>
                    <div class="w-20 text-right text-xs text-slate-500">${it.total_quantity} шт</div>
                </div>`;
        }).join('');
    } catch (e) {
        host.innerHTML = `<div class="text-rose-600 p-4">Ошибка: ${escapeHtml(e.message)}</div>`;
    }
}

document.getElementById('btnLoadTop').addEventListener('click', loadTop);

// =====================================================================
// ПОИСК + TIMELINE
// =====================================================================
async function searchWeapon() {
    const q = document.getElementById('searchQ').value.trim();
    if (q.length < 2) return UI.showToast('Введите хотя бы 2 символа', 'error');
    const host = document.getElementById('searchResults');
    host.innerHTML = '<div class="text-center py-6 text-slate-400"><i class="fa-solid fa-spinner fa-spin"></i></div>';
    try {
        const items = await api('/reports/search-weapon?q=' + encodeURIComponent(q));
        if (!items.length) {
            host.innerHTML = '<div class="bg-white rounded-xl shadow-sm p-4 text-slate-400 text-center">Ничего не найдено.</div>';
            return;
        }
        host.innerHTML = `
            <div class="bg-white rounded-xl shadow-sm divide-y">
                ${items.map(r => `
                    <button onclick="loadTimeline(${r.id}, '${escapeHtml(r.serial_number).replace(/'/g, "\\'")}')"
                            class="w-full text-left px-4 py-3 hover:bg-slate-50 block">
                        <div class="font-medium">${escapeHtml(r.nomenclature_name)}</div>
                        <div class="text-xs text-slate-500 font-mono">
                            ${escapeHtml(r.serial_number || '—')}${r.inventory_number ? ' · инв. ' + escapeHtml(r.inventory_number) : ''}
                        </div>
                    </button>`).join('')}
            </div>`;
    } catch (e) {
        host.innerHTML = `<div class="text-rose-600 p-4">Ошибка: ${escapeHtml(e.message)}</div>`;
    }
}

async function loadTimeline(id, serial) {
    const host = document.getElementById('timelineBody');
    host.innerHTML = '<div class="text-center py-6 text-slate-400"><i class="fa-solid fa-spinner fa-spin text-2xl"></i></div>';
    try {
        const data = await api(`/reports/timeline?weapon_id=${id}`);
        const items = data.history || [];
        const badge = data.status.includes('В наличии')
            ? 'bg-emerald-100 text-emerald-800'
            : data.status.includes('ремонте') ? 'bg-amber-100 text-amber-800'
            : 'bg-slate-200 text-slate-700';
        host.innerHTML = `
            <div class="bg-white rounded-xl shadow-sm p-4 mb-3">
                <div class="text-sm text-slate-500">Серийник: <b>${escapeHtml(serial)}</b></div>
                <div class="mt-1"><span class="inline-block px-3 py-1 rounded-full text-sm ${badge}">${escapeHtml(data.status)}</span></div>
            </div>
            <div class="bg-white rounded-xl shadow-sm overflow-hidden">
                ${items.length ? items.map(ev => `
                    <div class="px-4 py-3 border-b border-slate-100 last:border-0">
                        <div class="flex justify-between">
                            <div>
                                <span class="font-medium">${escapeHtml(ev.operation_type)}</span>
                                <span class="text-xs text-slate-500 ml-2">${escapeHtml(ev.doc_number)}</span>
                            </div>
                            <div class="text-xs text-slate-500">${fmtDate(ev.operation_date)}</div>
                        </div>
                        <div class="text-xs text-slate-600 mt-1">
                            ${escapeHtml(ev.source || '—')} → ${escapeHtml(ev.target || '—')}
                        </div>
                    </div>`).join('') : '<div class="p-6 text-center text-slate-400">История пуста.</div>'}
            </div>`;
    } catch (e) {
        host.innerHTML = `<div class="text-rose-600 p-4">Ошибка: ${escapeHtml(e.message)}</div>`;
    }
}
window.loadTimeline = loadTimeline;

document.getElementById('btnSearch').addEventListener('click', searchWeapon);
document.getElementById('searchQ').addEventListener('keydown', e => {
    if (e.key === 'Enter') searchWeapon();
});

// =====================================================================
// INIT
// =====================================================================
loadObjects().then(loadBalance);
