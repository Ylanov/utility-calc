// static/js/modules/analyzer.js
//
// «Центр анализа» — единый экран управления всеми анализаторами.
// Дёргает /api/admin/analyzer/* (см. app/modules/utility/routers/admin_analyzer.py).

import { api } from '../core/api.js';
import { toast } from '../core/dom.js';

const CATEGORY_META = {
    gsheets:  { label: 'Google Sheets матчер',     color: '#16a34a' },
    anomaly:  { label: 'Anomaly Detector',         color: '#dc2626' },
    rules:    { label: 'Дополнительные правила',   color: '#7c3aed' },
    approve:  { label: 'Авто-утверждение',         color: '#2563eb' },
    debt:     { label: 'Импорт долгов из 1С',      color: '#f59e0b' },
};

function escapeHtml(s) {
    if (s == null) return '';
    return String(s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function fmtDateTime(iso) {
    if (!iso) return '—';
    try {
        const d = new Date(iso);
        return d.toLocaleString('ru-RU', {
            day: '2-digit', month: '2-digit', year: 'numeric',
            hour: '2-digit', minute: '2-digit',
        });
    } catch { return iso; }
}

export const AnalyzerModule = {
    isInitialized: false,
    state: { period: 30, settings: [], dashboard: null, dismissals: [] },

    init() {
        if (this.isInitialized) {
            this.refresh();
            return;
        }
        this.cacheDOM();
        if (!this.dom.kpis) return;
        this.bindEvents();
        this.isInitialized = true;
        this.refresh();
    },

    cacheDOM() {
        this.dom = {
            kpis:           document.getElementById('analyzerKPIs'),
            topFlags:       document.getElementById('analyzerTopFlags'),
            gsheetsStats:   document.getElementById('analyzerGsheetsStats'),
            settings:       document.getElementById('analyzerSettings'),
            dismissals:     document.getElementById('analyzerDismissals'),
            period:         document.getElementById('analyzerPeriod'),
            btnRefresh:     document.getElementById('btnAnalyzerRefresh'),
            btnInvalidate:  document.getElementById('btnAnalyzerInvalidate'),
        };
    },

    bindEvents() {
        this.dom.period?.addEventListener('change', () => {
            this.state.period = Number(this.dom.period.value) || 30;
            this.loadDashboard();
        });
        this.dom.btnRefresh?.addEventListener('click', () => this.refresh());
        this.dom.btnInvalidate?.addEventListener('click', () => this.invalidateCaches());

        // Делегирование: inline-edit настроек + удаление dismissal
        this.dom.settings?.addEventListener('change', (e) => {
            const sw = e.target.closest('input[data-toggle-key]');
            if (sw) this.toggleEnabled(sw.dataset.toggleKey, sw.checked);
        });
        this.dom.settings?.addEventListener('click', (e) => {
            const btn = e.target.closest('button[data-save-key]');
            if (btn) this.saveSetting(btn.dataset.saveKey);
        });
        this.dom.dismissals?.addEventListener('click', (e) => {
            const btn = e.target.closest('button[data-delete-dismissal]');
            if (btn) this.deleteDismissal(Number(btn.dataset.deleteDismissal));
        });
    },

    async refresh() {
        await Promise.all([
            this.loadDashboard(),
            this.loadSettings(),
            this.loadDismissals(),
        ]);
    },

    // ====================================================================
    // DASHBOARD
    // ====================================================================
    async loadDashboard() {
        try {
            const data = await api.get(`/admin/analyzer/dashboard?days=${this.state.period}`);
            this.state.dashboard = data;
            this.renderKPIs(data);
            this.renderTopFlags(data);
            this.renderGsheetsStats(data);
        } catch (e) {
            this.dom.kpis.innerHTML = `<div style="color:var(--danger-color); padding:14px;">Ошибка: ${escapeHtml(e.message)}</div>`;
        }
    },

    renderKPIs(d) {
        const a = d.anomalies || {};
        const sev = a.by_severity || {};
        const gs = d.gsheets || {};
        const sl = d.self_learning || {};

        const kpi = (label, value, color, hint) => `
            <div style="background:var(--bg-card); border:1px solid var(--border-color); border-radius:10px; padding:14px;">
                <div style="font-size:11px; color:var(--text-secondary); text-transform:uppercase; letter-spacing:.5px;">${escapeHtml(label)}</div>
                <div style="font-size:26px; font-weight:700; color:${color}; margin:4px 0 2px;">${value}</div>
                ${hint ? `<div style="font-size:11px; color:var(--text-tertiary);">${escapeHtml(hint)}</div>` : ''}
            </div>`;

        this.dom.kpis.innerHTML = [
            kpi('Аномалий найдено', a.total_flagged_readings || 0, '#dc2626',
                `за последние ${d.period_days} дн.`),
            kpi('Критических', sev['critical (80-100)'] || 0, '#ef4444',
                'score ≥ 80 — требуют внимания'),
            kpi('GSheets auto-approved', gs.by_status?.auto_approved || 0, '#10b981',
                'автоматически утверждено'),
            kpi('GSheets конфликтов', (gs.by_status?.conflict || 0) + (gs.by_status?.unmatched || 0), '#f59e0b',
                'требуют ручного решения'),
            kpi('Алиасов всего', gs.aliases_total || 0, '#3b82f6',
                `+${gs.aliases_new_in_period || 0} за период`),
            kpi('Self-learning', sl.total_dismissals || 0, '#7c3aed',
                `${sl.global_dismissals || 0} глобальных`),
        ].join('');
    },

    renderTopFlags(d) {
        const top = d.anomalies?.top_flags || [];
        if (!top.length) {
            this.dom.topFlags.innerHTML = '<div style="color:var(--text-secondary); padding:8px;">За период аномалий не было.</div>';
            return;
        }
        const max = top[0].count;
        this.dom.topFlags.innerHTML = top.map(f => {
            const pct = Math.round((f.count / max) * 100);
            return `
                <div style="display:flex; align-items:center; gap:10px; margin-bottom:6px;">
                    <div style="width:160px; font-size:12px; font-family:monospace;" title="${escapeHtml(f.flag)}">${escapeHtml(f.flag)}</div>
                    <div style="flex:1; background:#f3f4f6; border-radius:4px; height:14px; overflow:hidden;">
                        <div style="width:${pct}%; height:100%; background:#dc2626;"></div>
                    </div>
                    <div style="width:38px; text-align:right; font-weight:600; font-size:12px;">${f.count}</div>
                </div>`;
        }).join('');
    },

    renderGsheetsStats(d) {
        const stats = d.gsheets?.by_status || {};
        const order = ['pending','conflict','unmatched','auto_approved','approved','rejected'];
        const colors = {
            pending: '#3b82f6', conflict: '#f59e0b', unmatched: '#ef4444',
            auto_approved: '#8b5cf6', approved: '#10b981', rejected: '#6b7280',
        };
        const total = Object.values(stats).reduce((a,b) => a + b, 0);
        if (!total) {
            this.dom.gsheetsStats.innerHTML = '<div style="color:var(--text-secondary); padding:8px;">За период не было импортов.</div>';
            return;
        }
        this.dom.gsheetsStats.innerHTML = order.map(st => {
            const c = stats[st] || 0;
            if (!c) return '';
            const pct = Math.round((c / total) * 100);
            return `
                <div style="display:flex; align-items:center; gap:10px; margin-bottom:6px;">
                    <div style="width:120px; font-size:12px;">${escapeHtml(st)}</div>
                    <div style="flex:1; background:#f3f4f6; border-radius:4px; height:14px; overflow:hidden;">
                        <div style="width:${pct}%; height:100%; background:${colors[st]};"></div>
                    </div>
                    <div style="width:38px; text-align:right; font-weight:600; font-size:12px;">${c}</div>
                </div>`;
        }).join('');
    },

    // ====================================================================
    // SETTINGS
    // ====================================================================
    async loadSettings() {
        try {
            const data = await api.get('/admin/analyzer/settings');
            this.state.settings = data.items || [];
            this.renderSettings();
        } catch (e) {
            this.dom.settings.innerHTML = `<div style="color:var(--danger-color);">Ошибка: ${escapeHtml(e.message)}</div>`;
        }
    },

    renderSettings() {
        // Группируем по category
        const byCat = {};
        for (const s of this.state.settings) {
            (byCat[s.category] ||= []).push(s);
        }
        const html = Object.keys(byCat).sort().map(cat => {
            const meta = CATEGORY_META[cat] || { label: cat, color: '#6b7280' };
            return `
                <div style="margin-bottom:18px; border:1px solid var(--border-color); border-radius:10px; overflow:hidden;">
                    <div style="background:${meta.color}11; color:${meta.color}; padding:10px 14px; font-weight:600; font-size:13px; border-bottom:1px solid var(--border-color);">
                        <i class="fa-solid fa-folder"></i> ${escapeHtml(meta.label)}
                        <span style="color:var(--text-secondary); font-weight:normal; margin-left:6px; font-size:11px;">(${byCat[cat].length})</span>
                    </div>
                    <table style="width:100%; border-collapse:collapse;">
                        <thead style="background:var(--bg-page); font-size:11px; color:var(--text-secondary); text-transform:uppercase;">
                            <tr>
                                <th style="text-align:left; padding:6px 10px;">Параметр</th>
                                <th style="text-align:left; padding:6px 10px;">Значение</th>
                                <th style="text-align:left; padding:6px 10px; width:120px;">Включено</th>
                                <th style="padding:6px 10px; width:80px;"></th>
                            </tr>
                        </thead>
                        <tbody>
                            ${byCat[cat].map(s => this._renderSettingRow(s)).join('')}
                        </tbody>
                    </table>
                </div>`;
        }).join('');
        this.dom.settings.innerHTML = html || '<div style="color:var(--text-secondary);">Настройки не найдены.</div>';
    },

    _renderSettingRow(s) {
        const isBool = s.value_type === 'bool';
        const valueInput = isBool
            ? `<select id="setting-val-${s.key}" style="font-size:12px; padding:4px 8px;">
                  <option value="true"${s.value === 'true' ? ' selected' : ''}>true</option>
                  <option value="false"${s.value === 'false' ? ' selected' : ''}>false</option>
               </select>`
            : `<input id="setting-val-${s.key}" type="${s.value_type === 'int' || s.value_type === 'float' ? 'number' : 'text'}"
                      value="${escapeHtml(s.value)}"
                      ${s.value_type === 'float' ? 'step="0.001"' : ''}
                      ${s.min_value ? `min="${escapeHtml(s.min_value)}"` : ''}
                      ${s.max_value ? `max="${escapeHtml(s.max_value)}"` : ''}
                      style="font-size:12px; padding:4px 8px; width:100%;">`;

        const range = (s.min_value !== null || s.max_value !== null) && !isBool
            ? `<span style="font-size:10px; color:var(--text-tertiary); margin-left:6px;">[${s.min_value ?? '—'} … ${s.max_value ?? '—'}]</span>`
            : '';

        return `
            <tr style="border-bottom:1px solid var(--border-color);">
                <td style="padding:8px 10px; font-family:monospace; font-size:12px; vertical-align:top;">
                    <div style="font-weight:600;">${escapeHtml(s.key)}</div>
                    ${s.description ? `<div style="color:var(--text-secondary); font-family:inherit; font-size:11px; margin-top:2px;">${escapeHtml(s.description)}</div>` : ''}
                </td>
                <td style="padding:8px 10px; vertical-align:top;">${valueInput}${range}</td>
                <td style="padding:8px 10px; vertical-align:top;">
                    <label style="display:inline-flex; align-items:center; gap:6px; font-size:12px; cursor:pointer;">
                        <input type="checkbox" data-toggle-key="${s.key}" ${s.is_enabled ? 'checked' : ''}>
                        <span>${s.is_enabled ? 'вкл.' : 'выкл.'}</span>
                    </label>
                </td>
                <td style="padding:8px 10px; text-align:right; vertical-align:top;">
                    <button class="action-btn primary-btn" data-save-key="${s.key}" style="padding:4px 10px; font-size:11px;">
                        <i class="fa-solid fa-floppy-disk"></i>
                    </button>
                </td>
            </tr>`;
    },

    async saveSetting(key) {
        const input = document.getElementById(`setting-val-${key}`);
        if (!input) return;
        const value = input.value;
        try {
            const r = await api.patch(`/admin/analyzer/settings/${encodeURIComponent(key)}`, { value });
            if (r.status === 'noop') {
                toast('Значение не изменилось', 'info');
            } else {
                toast(`Сохранено: ${key} = ${value}`, 'success');
                this.loadSettings();
            }
        } catch (e) {
            toast('Ошибка: ' + e.message, 'error');
        }
    },

    async toggleEnabled(key, isEnabled) {
        try {
            await api.patch(`/admin/analyzer/settings/${encodeURIComponent(key)}`, {
                is_enabled: isEnabled,
            });
            toast(`${key}: ${isEnabled ? 'включено' : 'выключено'}`, 'success');
            this.loadSettings();
        } catch (e) {
            toast('Ошибка: ' + e.message, 'error');
            this.loadSettings();  // откат UI
        }
    },

    async invalidateCaches() {
        try {
            await api.post('/admin/analyzer/cache/invalidate', {});
            toast('Кеш сброшен — все настройки активны прямо сейчас', 'success');
        } catch (e) {
            toast('Ошибка: ' + e.message, 'error');
        }
    },

    // ====================================================================
    // DISMISSALS
    // ====================================================================
    async loadDismissals() {
        try {
            const data = await api.get('/admin/analyzer/dismissals?limit=100');
            this.state.dismissals = data.items || [];
            this.renderDismissals();
        } catch (e) {
            this.dom.dismissals.innerHTML = `<tr><td colspan="6" style="color:var(--danger-color); padding:14px;">Ошибка: ${escapeHtml(e.message)}</td></tr>`;
        }
    },

    renderDismissals() {
        if (!this.state.dismissals.length) {
            this.dom.dismissals.innerHTML = `
                <tr><td colspan="6" style="text-align:center; padding:20px; color:var(--text-secondary);">
                    Пока пусто. Откройте подозрительное показание и нажмите «Это не аномалия для этого жильца» — добавится сюда.
                </td></tr>`;
            return;
        }
        this.dom.dismissals.innerHTML = this.state.dismissals.map(d => `
            <tr style="border-bottom:1px solid var(--border-color);">
                <td style="padding:8px;">${d.is_global
                    ? '<span style="background:#fee2e2; color:#991b1b; padding:2px 8px; border-radius:10px; font-size:11px; font-weight:600;">ВСЕ ЖИЛЬЦЫ</span>'
                    : escapeHtml(d.username || `#${d.user_id}`)}
                </td>
                <td style="padding:8px; font-family:monospace; font-size:12px;">${escapeHtml(d.flag_code)}</td>
                <td style="padding:8px; font-size:12px; color:var(--text-secondary);">${escapeHtml(d.reason || '—')}</td>
                <td style="padding:8px; font-size:12px;">${escapeHtml(d.created_by || '—')}</td>
                <td style="padding:8px; font-size:12px;">${escapeHtml(fmtDateTime(d.created_at))}</td>
                <td style="padding:8px; text-align:right;">
                    <button class="action-btn danger-btn" data-delete-dismissal="${d.id}" style="padding:3px 8px; font-size:11px;" title="Снять пометку — анализатор снова будет флагать эту аномалию">
                        <i class="fa-solid fa-trash"></i>
                    </button>
                </td>
            </tr>
        `).join('');
    },

    async deleteDismissal(id) {
        if (!confirm('Снять пометку «не аномалия»? Анализатор снова начнёт реагировать на эту ситуацию.')) return;
        try {
            await api.delete(`/admin/analyzer/dismissals/${id}`);
            toast('Удалено', 'success');
            this.loadDismissals();
        } catch (e) {
            toast('Ошибка: ' + e.message, 'error');
        }
    },
};
