/**
 * МОДУЛЬ: Проверки и Журнал.
 *
 * Работает с endpoints:
 *   /api/arsenal/audit-log                 — журнал действий
 *   /api/arsenal/analyzer/anomalies        — список аномалий
 *   /api/arsenal/analyzer/anomalies/{id}/dismiss — отложить false-positive
 *   /api/arsenal/analyzer/run              — запустить проверку вручную
 *   /api/arsenal/analyzer/settings         — пороги правил
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

const fmtDate = (iso) => {
    if (!iso) return '—';
    try {
        return new Date(iso).toLocaleString('ru-RU', {
            day: '2-digit', month: '2-digit', year: '2-digit',
            hour: '2-digit', minute: '2-digit',
        });
    } catch { return iso; }
};

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
        if (tab === 'anomalies') loadAnomalies();
        else if (tab === 'audit') loadAudit();
        else if (tab === 'settings') loadSettings();
    });
});

// =====================================================================
// АНОМАЛИИ
// =====================================================================
async function loadAnomalies() {
    const showDismissed = document.getElementById('showDismissed').checked;
    const showResolved = document.getElementById('showResolved').checked;
    const params = new URLSearchParams({
        include_dismissed: showDismissed,
        include_resolved: showResolved,
        limit: 200,
    });
    try {
        const data = await api('/analyzer/anomalies?' + params);
        renderRuleCards(data);
        renderAnomaliesTable(data);
        document.getElementById('tabBadgeAnomalies').textContent = data.total || 0;
    } catch (e) {
        UI.showToast('Ошибка загрузки аномалий: ' + e.message, 'error');
    }
}

function renderRuleCards(data) {
    const host = document.getElementById('ruleCards');
    const summary = data.summary_by_rule || {};
    const catalog = data.catalog || [];
    host.innerHTML = catalog.map(rule => {
        const s = summary[rule.code] || { critical: 0, warning: 0, info: 0 };
        const total = s.critical + s.warning + s.info;
        const totalBadge = total > 0
            ? `<span class="ml-2 bg-rose-600 text-white text-xs px-2 py-0.5 rounded-full">${total}</span>`
            : `<span class="ml-2 text-emerald-500 text-xs"><i class="fa-solid fa-check"></i></span>`;
        const color = rule.severity === 'critical' ? 'border-rose-300'
                    : rule.severity === 'warning' ? 'border-amber-300'
                    : 'border-blue-300';
        return `
            <div class="bg-white rounded-lg border ${color} p-3 text-sm">
                <div class="font-semibold text-slate-800 mb-1 flex items-center">
                    ${rule.title} ${totalBadge}
                </div>
                <div class="text-xs text-slate-500">${rule.desc}</div>
                <div class="text-xs text-slate-400 mt-2 font-mono">${rule.code}</div>
            </div>`;
    }).join('');
}

function renderAnomaliesTable(data) {
    const tbody = document.getElementById('anomaliesBody');
    const items = data.items || [];
    document.getElementById('anomaliesCount').textContent = `Всего: ${data.total}`;
    if (!items.length) {
        tbody.innerHTML = `<tr><td colspan="5" class="px-3 py-10 text-center text-emerald-600">
            <i class="fa-solid fa-check-circle text-2xl"></i><br>Нарушений не найдено. Всё чисто.
        </td></tr>`;
        return;
    }
    tbody.innerHTML = items.map(a => {
        const sev = `<span class="inline-block px-2 py-0.5 rounded text-xs font-medium sev-${a.severity}">${a.rule_code}</span>`;
        const statusIcon = a.resolved_at
            ? '<i class="fa-solid fa-check text-emerald-500" title="Исправлено"></i>'
            : a.dismissed_at
              ? '<i class="fa-solid fa-eye-slash text-slate-400" title="Отложено"></i>'
              : '';
        const actions = (a.dismissed_at || a.resolved_at)
            ? `<span class="text-xs text-slate-400">—</span>`
            : `<button onclick="dismissAnomaly(${a.id})" class="text-xs bg-slate-100 hover:bg-slate-200 px-2 py-1 rounded">
                  <i class="fa-solid fa-eye-slash"></i> Отложить
               </button>`;
        const detailsHtml = a.details
            ? `<pre class="text-xs text-slate-500 max-w-md whitespace-pre-wrap">${escapeHtml(JSON.stringify(a.details, null, 2))}</pre>`
            : '';
        return `
            <tr class="border-b border-slate-100 ${a.dismissed_at || a.resolved_at ? 'opacity-60' : ''}">
                <td class="px-3 py-2 align-top whitespace-nowrap">${sev} ${statusIcon}</td>
                <td class="px-3 py-2 align-top">
                    <div class="font-medium">${escapeHtml(a.title)}</div>
                    ${detailsHtml}
                </td>
                <td class="px-3 py-2 align-top text-xs text-slate-500 whitespace-nowrap">${fmtDate(a.first_seen_at)}</td>
                <td class="px-3 py-2 align-top text-xs text-slate-500 whitespace-nowrap">${fmtDate(a.last_seen_at)}</td>
                <td class="px-3 py-2 align-top text-right">${actions}</td>
            </tr>`;
    }).join('');
}

function escapeHtml(s) {
    if (s == null) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

async function dismissAnomaly(id) {
    const reason = prompt('Причина (опционально):\n\nНапример: «проверено лично, штатный случай»');
    if (reason === null) return;
    try {
        await api(`/analyzer/anomalies/${id}/dismiss`, {
            method: 'POST',
            body: JSON.stringify({ reason: reason || null }),
        });
        UI.showToast('Отложено', 'success');
        loadAnomalies();
    } catch (e) {
        UI.showToast(e.message, 'error');
    }
}
window.dismissAnomaly = dismissAnomaly;

document.getElementById('showDismissed').addEventListener('change', loadAnomalies);
document.getElementById('showResolved').addEventListener('change', loadAnomalies);

document.getElementById('btnRunAnalyzer').addEventListener('click', async () => {
    const btn = document.getElementById('btnRunAnalyzer');
    btn.disabled = true;
    const orig = btn.innerHTML;
    btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Анализ…';
    try {
        const res = await api('/analyzer/run', { method: 'POST' });
        const total = Object.values(res.findings || {}).reduce((a, b) => a + (b > 0 ? b : 0), 0);
        UI.showToast(`Анализ завершён: ${total} активных флагов`, 'success');
        loadAnomalies();
    } catch (e) {
        UI.showToast('Ошибка: ' + e.message, 'error');
    } finally {
        btn.disabled = false;
        btn.innerHTML = orig;
    }
});

// =====================================================================
// ЖУРНАЛ
// =====================================================================
let auditDebounce = null;
function bindAuditFilters() {
    ['auditAction', 'auditEntity', 'auditDateFrom', 'auditDateTo'].forEach(id => {
        document.getElementById(id).addEventListener('input', () => {
            clearTimeout(auditDebounce);
            auditDebounce = setTimeout(loadAudit, 300);
        });
    });
}
bindAuditFilters();

async function loadAudit() {
    const params = new URLSearchParams({ limit: 200 });
    const action = document.getElementById('auditAction').value.trim();
    const entity = document.getElementById('auditEntity').value.trim();
    const from = document.getElementById('auditDateFrom').value;
    const to = document.getElementById('auditDateTo').value;
    if (action) params.set('action', action);
    if (entity) params.set('entity_type', entity);
    if (from) params.set('date_from', from + 'T00:00:00');
    if (to) params.set('date_to', to + 'T23:59:59');
    try {
        const data = await api('/audit-log?' + params);
        const tbody = document.getElementById('auditBody');
        if (!data.items.length) {
            tbody.innerHTML = `<tr><td colspan="5" class="px-3 py-10 text-center text-slate-400">
                Записей нет. Попробуйте другие фильтры.
            </td></tr>`;
            return;
        }
        tbody.innerHTML = data.items.map(r => `
            <tr class="border-b border-slate-100 hover:bg-slate-50">
                <td class="px-3 py-2 text-xs text-slate-500 whitespace-nowrap">${fmtDate(r.created_at)}</td>
                <td class="px-3 py-2 text-sm">${escapeHtml(r.username)}</td>
                <td class="px-3 py-2">
                    <span class="inline-block px-2 py-0.5 rounded text-xs bg-slate-100">${escapeHtml(r.action)}</span>
                </td>
                <td class="px-3 py-2 text-sm text-slate-600">
                    ${escapeHtml(r.entity_type)}${r.entity_id ? ` #${r.entity_id}` : ''}
                </td>
                <td class="px-3 py-2 text-xs text-slate-500">
                    ${r.details ? `<pre class="whitespace-pre-wrap max-w-md">${escapeHtml(JSON.stringify(r.details))}</pre>` : ''}
                </td>
            </tr>
        `).join('');
    } catch (e) {
        UI.showToast('Ошибка журнала: ' + e.message, 'error');
    }
}

// =====================================================================
// НАСТРОЙКИ ПРАВИЛ
// =====================================================================
async function loadSettings() {
    try {
        const data = await api('/analyzer/settings');
        const byCategory = {};
        (data || []).forEach(s => {
            (byCategory[s.category] = byCategory[s.category] || []).push(s);
        });
        const host = document.getElementById('settingsBody');
        host.innerHTML = Object.keys(byCategory).map(cat => `
            <div class="bg-white rounded-xl shadow-sm p-4">
                <h3 class="font-bold text-slate-700 mb-3 capitalize">${cat}</h3>
                <div class="space-y-2">
                    ${byCategory[cat].map(renderSetting).join('')}
                </div>
            </div>
        `).join('');
    } catch (e) {
        UI.showToast('Ошибка загрузки настроек: ' + e.message, 'error');
    }
}

function renderSetting(s) {
    const isBool = s.value_type === 'bool';
    const valueInput = isBool
        ? `<select id="val-${s.key}" class="border border-slate-300 rounded px-2 py-1 text-sm">
              <option value="true" ${s.value === 'true' ? 'selected' : ''}>Включено</option>
              <option value="false" ${s.value === 'false' ? 'selected' : ''}>Выключено</option>
           </select>`
        : `<input id="val-${s.key}" type="${s.value_type === 'int' || s.value_type === 'float' ? 'number' : 'text'}"
                  value="${escapeHtml(s.value)}"
                  ${s.min_value ? `min="${escapeHtml(s.min_value)}"` : ''}
                  ${s.max_value ? `max="${escapeHtml(s.max_value)}"` : ''}
                  class="border border-slate-300 rounded px-2 py-1 text-sm w-32">`;
    return `
        <div class="flex items-center gap-3 py-2 border-b border-slate-100 last:border-0">
            <div class="flex-1">
                <code class="text-xs text-slate-700 font-mono">${s.key}</code>
                <div class="text-xs text-slate-500 mt-0.5">${escapeHtml(s.description || '')}</div>
            </div>
            ${valueInput}
            <label class="flex items-center gap-1 text-xs">
                <input type="checkbox" id="en-${s.key}" ${s.is_enabled ? 'checked' : ''}>
                <span>вкл.</span>
            </label>
            <button onclick="saveSetting('${s.key}')" class="bg-slate-700 hover:bg-slate-800 text-white px-3 py-1 rounded text-xs">
                <i class="fa-solid fa-floppy-disk"></i>
            </button>
        </div>`;
}

async function saveSetting(key) {
    const val = document.getElementById(`val-${key}`).value;
    const enabled = document.getElementById(`en-${key}`).checked;
    try {
        const res = await api(`/analyzer/settings/${encodeURIComponent(key)}`, {
            method: 'PATCH',
            body: JSON.stringify({ value: val, is_enabled: enabled }),
        });
        if (res.status === 'noop') {
            UI.showToast('Не изменилось', 'info');
        } else {
            UI.showToast('Сохранено', 'success');
        }
    } catch (e) {
        UI.showToast(e.message, 'error');
    }
}
window.saveSetting = saveSetting;

// Стартуем на вкладке аномалий
loadAnomalies();
