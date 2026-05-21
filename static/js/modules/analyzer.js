// static/js/modules/analyzer.js
//
// «Центр анализа» — единый экран управления всеми анализаторами.
// Дёргает /api/admin/analyzer/* (см. app/modules/utility/routers/admin_analyzer.py).

import { api } from '../core/api.js';
import { toast } from '../core/dom.js';
import { SummaryModule } from './summary.js';

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
    state: {
        period: 30,
        settings: [],
        dashboard: null,
        dismissals: [],
        // Активный под-таб периода: 'preview'|'compare'
        periodsTab: 'preview',
        // Кэш списка периодов для compare-селекторов
        periodsCache: [],
    },

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
        // Периоды нужны для вкладки «Анализ периода» → загружаем в фоне
        this.loadPeriodsForCompare();
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

            // Топ-табы (dashboard/period/housing/maintenance)
            tabs:           document.querySelectorAll('[data-analyzer-tab]'),
            panes:          document.querySelectorAll('[data-analyzer-pane]'),

            // Таб «Анализ периода»
            tabPreview:     document.getElementById('tabPreview'),
            tabCompare:     document.getElementById('tabCompare'),
            paneClosePreview: document.getElementById('paneClosePreview'),
            paneCompare:    document.getElementById('paneCompare'),
            btnLoadPreview: document.getElementById('btnLoadPreview'),
            closePreviewContainer: document.getElementById('closePreviewContainer'),
            comparePeriodA: document.getElementById('comparePeriodA'),
            comparePeriodB: document.getElementById('comparePeriodB'),
            btnCompare:     document.getElementById('btnCompare'),
            compareContainer: document.getElementById('compareContainer'),

            // Таб «Анализ жилфонда»
            btnHousingRun:  document.getElementById('btnAnalyzerHousingRun'),
            housingResults: document.getElementById('analyzerHousingResults'),

            // Таб «Обслуживание»
            cleanupDays:    document.getElementById('analyzerCleanupDays'),
            btnCleanupNow:  document.getElementById('btnAnalyzerCleanupNow'),
            cleanupResult:  document.getElementById('analyzerCleanupResult'),
            btnFindDuplicates: document.getElementById('btnFindDuplicates'),
            duplicatesResult:  document.getElementById('duplicatesResult'),

            // Таб «Сверка 1С»
            btnReconcileRun: document.getElementById('btnReconcileRun'),
            reconcileResults: document.getElementById('reconcileResults'),

            // Таб «Заблокированные показания»
            btnLoadStuck:    document.getElementById('btnLoadStuck'),
            stuckContainer:  document.getElementById('stuckDraftsContainer'),
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

        // Клик по KPI-карточке с data-inbox-filter — открывает Inbox с
        // соответствующим фильтром. Карточки без атрибута (Алиасы /
        // Self-learning) — не реагируют.
        this.dom.kpis?.addEventListener('click', (e) => {
            const card = e.target.closest('[data-inbox-filter]');
            if (card) this.openInbox(card.dataset.inboxFilter);
        });

        // Верхнеуровневые табы
        this.dom.tabs?.forEach(btn => {
            btn.addEventListener('click', () => this._setTab(btn.dataset.analyzerTab));
        });

        // Под-табы «Период»
        this.dom.tabPreview?.addEventListener('click', () => this._setPeriodsTab('preview'));
        this.dom.tabCompare?.addEventListener('click', () => this._setPeriodsTab('compare'));
        this.dom.btnLoadPreview?.addEventListener('click', () => this.loadClosePreview());
        this.dom.btnCompare?.addEventListener('click', () => this.runComparison());

        // Таб «Жилфонд»
        this.dom.btnHousingRun?.addEventListener('click', () => this.runHousingAnalysis());

        // Таб «Обслуживание»
        this.dom.btnCleanupNow?.addEventListener('click', () => this.runGsheetsCleanup());
        this.dom.btnFindDuplicates?.addEventListener('click', () => this.findDuplicates());
        // Делегирование клика «Удалить дубликат» внутри результата
        this.dom.duplicatesResult?.addEventListener('click', (e) => {
            const btn = e.target.closest('button[data-delete-dup-id]');
            if (btn) {
                e.preventDefault();
                this.deleteDuplicateReading(Number(btn.dataset.deleteDupId));
            }
        });

        // Таб «Сверка 1С»
        this.dom.btnReconcileRun?.addEventListener('click', () => this.runReconcile());

        // Таб «Заблокированные показания»
        this.dom.btnLoadStuck?.addEventListener('click', () => this.loadStuckDrafts());
        this.dom.stuckContainer?.addEventListener('click', (e) => {
            const delBtn = e.target.closest('button[data-stuck-delete]');
            if (delBtn) {
                e.preventDefault();
                this.deleteStuckReading(Number(delBtn.dataset.stuckDelete));
                return;
            }
            const apprBtn = e.target.closest('button[data-stuck-approve]');
            if (apprBtn) {
                e.preventDefault();
                this.approveStuckReading(Number(apprBtn.dataset.stuckApprove));
                return;
            }
            const detailBtn = e.target.closest('button[data-stuck-detail]');
            if (detailBtn) {
                e.preventDefault();
                // Открываем существующий модал «Проверка расчёта» (см. admin.html
                // #explainModal + summary.js:openExplainModal). Там админ видит
                // breakdown тарифа и формулу — можно понять что не так и решить
                // через action-кнопки в модале.
                const rid = Number(detailBtn.dataset.stuckDetail);
                try {
                    SummaryModule.openExplainModal(rid);
                } catch (err) {
                    toast('Не удалось открыть модал: ' + err.message, 'error');
                }
            }
        });
    },

    _setTab(tabId) {
        this.dom.tabs.forEach(btn => {
            const active = btn.dataset.analyzerTab === tabId;
            btn.classList.toggle('primary-btn', active);
            btn.classList.toggle('secondary-btn', !active);
        });
        this.dom.panes.forEach(pane => {
            pane.style.display = pane.dataset.analyzerPane === tabId ? '' : 'none';
        });
    },

    _setPeriodsTab(tab) {
        if (this.state.periodsTab === tab) return;
        this.state.periodsTab = tab;
        const isPreview = tab === 'preview';
        if (this.dom.paneClosePreview) this.dom.paneClosePreview.style.display = isPreview ? '' : 'none';
        if (this.dom.paneCompare) this.dom.paneCompare.style.display = isPreview ? 'none' : '';
        const set = (btn, active) => {
            if (!btn) return;
            btn.classList.toggle('primary-btn', active);
            btn.classList.toggle('secondary-btn', !active);
        };
        set(this.dom.tabPreview, isPreview);
        set(this.dom.tabCompare, !isPreview);
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

        // 5-й параметр inboxFilter — если задан, карточка кликабельная и
        // открывает Inbox с этим фильтром. Иначе обычная статичная
        // карточка (например, «Алиасов всего» — кликать некуда).
        const kpi = (label, value, color, hint, inboxFilter) => {
            const clickable = inboxFilter
                ? `data-inbox-filter="${escapeHtml(inboxFilter)}" style="background:var(--bg-card); border:1px solid var(--border-color); border-radius:10px; padding:14px; cursor:pointer; transition:transform 0.1s, box-shadow 0.15s; position:relative;" title="Открыть список и решить"`
                : `style="background:var(--bg-card); border:1px solid var(--border-color); border-radius:10px; padding:14px;"`;
            const arrow = inboxFilter
                ? `<i class="fa-solid fa-arrow-right" style="position:absolute; top:14px; right:14px; color:var(--text-tertiary); font-size:11px;"></i>`
                : '';
            return `
                <div ${clickable}>
                    ${arrow}
                    <div style="font-size:11px; color:var(--text-secondary); text-transform:uppercase; letter-spacing:.5px;">${escapeHtml(label)}</div>
                    <div style="font-size:26px; font-weight:700; color:${color}; margin:4px 0 2px;">${value}</div>
                    ${hint ? `<div style="font-size:11px; color:var(--text-tertiary);">${escapeHtml(hint)}</div>` : ''}
                </div>`;
        };

        this.dom.kpis.innerHTML = [
            kpi('Аномалий найдено', a.total_flagged_readings || 0, '#dc2626',
                `за последние ${d.period_days} дн.`, 'anomalies'),
            kpi('Критических', sev['critical (80-100)'] || 0, '#ef4444',
                'score ≥ 80 — требуют внимания', 'critical'),
            kpi('GSheets auto-approved', gs.by_status?.auto_approved || 0, '#10b981',
                'автоматически утверждено'),
            kpi('GSheets конфликтов', (gs.by_status?.conflict || 0) + (gs.by_status?.unmatched || 0), '#f59e0b',
                'требуют ручного решения', 'gsheets'),
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
        const html = Object.keys(byCat).sort((a, b) => a.localeCompare(b, 'ru')).map(cat => {
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

    // ====================================================================
    // ТАБ «АНАЛИЗ ПЕРИОДА» — перенесено из summary.js
    // Backend endpoints те же: /admin/periods/close-preview, /admin/periods/compare.
    // ====================================================================
    async loadPeriodsForCompare() {
        try {
            const periods = await api.get('/admin/periods/history');
            this.state.periodsCache = periods || [];
            const fill = (sel) => {
                if (!sel) return;
                sel.innerHTML = '<option value="">Выберите период…</option>';
                this.state.periodsCache.forEach(p => {
                    const o = document.createElement('option');
                    o.value = p.id;
                    o.textContent = p.name + (p.is_active ? ' (Акт.)' : '');
                    sel.appendChild(o);
                });
            };
            fill(this.dom.comparePeriodA);
            fill(this.dom.comparePeriodB);
            if (this.state.periodsCache.length >= 2) {
                this.dom.comparePeriodB.value = this.state.periodsCache[0].id;
                this.dom.comparePeriodA.value = this.state.periodsCache[1].id;
            }
        } catch (e) {
            // тихо — селекторы останутся пустыми, админ увидит «Выберите период…»
        }
    },

    async loadClosePreview() {
        if (!this.dom.closePreviewContainer) return;
        const btn = this.dom.btnLoadPreview;
        if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Анализ…'; }
        this.dom.closePreviewContainer.innerHTML =
            '<div style="padding:20px; text-align:center; color:var(--text-secondary);">Сканируем данные…</div>';
        try {
            const data = await api.get('/admin/periods/close-preview');
            this._renderClosePreview(data);
        } catch (e) {
            this.dom.closePreviewContainer.innerHTML =
                `<div style="padding:16px; color:var(--danger-color);">Ошибка: ${escapeHtml(e.message)}</div>`;
        } finally {
            if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-eye"></i> Загрузить отчёт'; }
        }
    },

    _renderClosePreview(data) {
        const pct = data.total_occupied_rooms > 0
            ? Math.round(data.rooms_with_readings / data.total_occupied_rooms * 100) : 0;
        const progressColor = pct >= 80 ? '#10b981' : pct >= 50 ? '#f59e0b' : '#ef4444';
        const money = (v) => Number(v || 0).toLocaleString('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + ' ₽';

        const dormHtml = data.dormitories?.length ? `
            <table style="width:100%; border-collapse:collapse; margin-top:14px; font-size:13px;">
                <thead>
                    <tr style="background:var(--bg-page); color:var(--text-secondary); font-size:11px; text-transform:uppercase;">
                        <th style="text-align:left; padding:8px;">Общежитие</th>
                        <th style="text-align:center; padding:8px;">Сдали</th>
                        <th style="text-align:center; padding:8px;">Не сдали</th>
                        <th style="text-align:center; padding:8px;">%</th>
                    </tr>
                </thead>
                <tbody>${data.dormitories.map(d => `
                    <tr style="border-bottom:1px solid var(--border-color);">
                        <td style="padding:8px; font-weight:500;">${escapeHtml(d.name)}</td>
                        <td style="text-align:center; padding:8px; color:#10b981; font-weight:600;">${d.submitted}</td>
                        <td style="text-align:center; padding:8px; color:${d.missing > 0 ? '#ef4444' : 'var(--text-tertiary)'}; font-weight:600;">${d.missing}</td>
                        <td style="text-align:center; padding:8px;">
                            <div style="background:#e5e7eb; border-radius:4px; height:8px; width:80px; display:inline-block; vertical-align:middle;">
                                <div style="background:${d.percent >= 80 ? '#10b981' : d.percent >= 50 ? '#f59e0b' : '#ef4444'}; height:100%; width:${d.percent}%; border-radius:4px;"></div>
                            </div>
                            <span style="margin-left:6px; font-size:12px;">${d.percent}%</span>
                        </td>
                    </tr>`).join('')}
                </tbody>
            </table>` : '';

        const card = (label, value, color, bg) => `
            <div style="background:${bg}; padding:14px; border-radius:8px; text-align:center;">
                <div style="font-size:24px; font-weight:700; color:${color};">${value}</div>
                <div style="font-size:12px; color:var(--text-secondary);">${label}</div>
            </div>`;

        this.dom.closePreviewContainer.innerHTML = `
            <div style="display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:10px; margin-bottom:14px;">
                ${card('Комнат сдали', data.rooms_with_readings, '#10b981', '#f0fdf4')}
                ${card('Авто-генерация', data.rooms_without_readings, data.rooms_without_readings > 0 ? '#ef4444' : '#10b981', data.rooms_without_readings > 0 ? '#fef2f2' : '#f0fdf4')}
                ${card('Аномалий', data.anomalies_count, data.anomalies_count > 0 ? '#f59e0b' : '#10b981', data.anomalies_count > 0 ? '#fffbeb' : '#f0fdf4')}
                ${card('Авто-утв.', data.safe_drafts, '#3b82f6', '#eff6ff')}
                ${card('Предв. итого', money(data.estimated_total), '#1f2937', '#f9fafb')}
            </div>
            <div style="background:var(--bg-page); padding:10px 14px; border-radius:6px; margin-bottom:8px; display:flex; align-items:center; gap:12px;">
                <div style="flex:1; background:#e5e7eb; border-radius:4px; height:12px;">
                    <div style="background:${progressColor}; height:100%; width:${pct}%; border-radius:4px; transition: width 0.5s;"></div>
                </div>
                <span style="font-weight:600; font-size:14px; color:${progressColor};">${pct}%</span>
                <span style="font-size:12px; color:var(--text-secondary);">комнат сдали показания</span>
            </div>
            ${dormHtml}`;
    },

    async runComparison() {
        const idA = this.dom.comparePeriodA?.value;
        const idB = this.dom.comparePeriodB?.value;
        if (!idA || !idB) return toast('Выберите оба периода', 'warning');
        if (idA === idB) return toast('Периоды должны быть разными', 'warning');

        const btn = this.dom.btnCompare;
        if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Анализ…'; }
        this.dom.compareContainer.innerHTML =
            '<div style="padding:30px; text-align:center; color:var(--text-secondary);">Сравниваем данные…</div>';
        try {
            const data = await api.get(`/admin/periods/compare?period_a=${idA}&period_b=${idB}`);
            this._renderComparison(data);
        } catch (e) {
            this.dom.compareContainer.innerHTML =
                `<div style="padding:16px; color:var(--danger-color);">Ошибка: ${escapeHtml(e.message)}</div>`;
        } finally {
            if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-scale-balanced"></i> Сравнить'; }
        }
    },

    _renderComparison(data) {
        const LABELS = {
            cost_hot_water: 'ГВС', cost_cold_water: 'ХВС', cost_sewage: 'Водоотв.',
            cost_electricity: 'Электр.', cost_maintenance: 'Содержание', cost_social_rent: 'Наём',
            cost_waste: 'ТКО', cost_fixed_part: 'Отопление', total_cost: 'ИТОГО'
        };
        const deltaCell = (val, pct) => {
            const color = val > 0 ? '#ef4444' : val < 0 ? '#10b981' : '#9ca3af';
            const arrow = val > 0 ? '▲' : val < 0 ? '▼' : '—';
            const sign = val > 0 ? '+' : '';
            return `<span style="color:${color}; font-weight:600;">${arrow} ${sign}${val.toFixed(2)}</span>
                    <span style="color:${color}; font-size:11px; margin-left:4px;">(${sign}${pct}%)</span>`;
        };

        let html = `
            <div style="padding:12px 16px; background:#eff6ff; border-radius:8px; margin-bottom:14px; font-size:13px; display:flex; gap:20px; align-items:center; flex-wrap:wrap;">
                <span><strong>A:</strong> ${escapeHtml(data.period_a.name)}</span>
                <span style="color:var(--text-secondary);">→</span>
                <span><strong>B:</strong> ${escapeHtml(data.period_b.name)}</span>
                <span style="color:var(--text-secondary); margin-left:auto; font-size:12px;">Красный = рост, зелёный = экономия</span>
            </div>`;

        data.dormitories.forEach(dorm => {
            const tc = dorm.details.total_cost;
            const dColor = tc.delta > 0 ? '#ef4444' : tc.delta < 0 ? '#10b981' : '#6b7280';
            html += `
                <div style="margin-bottom:14px; border:1px solid var(--border-color); border-radius:8px; overflow:hidden;">
                    <div style="display:flex; justify-content:space-between; align-items:center; padding:10px 14px; background:var(--bg-page); border-bottom:1px solid var(--border-color);">
                        <strong style="font-size:14px;">🏢 ${escapeHtml(dorm.dormitory)}</strong>
                        <span style="color:${dColor}; font-weight:700; font-size:14px;">
                            ${tc.delta > 0 ? '+' : ''}${tc.delta.toFixed(2)} ₽ (${tc.delta > 0 ? '+' : ''}${tc.percent}%)
                        </span>
                    </div>
                    <table style="width:100%; border-collapse:collapse; font-size:13px;">
                        <thead>
                            <tr style="background:var(--bg-page); color:var(--text-secondary); font-size:11px; text-transform:uppercase;">
                                <th style="text-align:left; padding:6px 10px;">Ресурс</th>
                                <th style="text-align:right; padding:6px 10px;">${escapeHtml(data.period_a.name)}</th>
                                <th style="text-align:right; padding:6px 10px;">${escapeHtml(data.period_b.name)}</th>
                                <th style="text-align:right; padding:6px 10px;">Изменение</th>
                            </tr>
                        </thead>
                        <tbody>`;
            for (const [key, label] of Object.entries(LABELS)) {
                const dt = dorm.details[key];
                if (!dt) continue;
                const isTotal = key === 'total_cost';
                const rs = isTotal ? 'background:#f0f9ff; font-weight:700;' : 'border-bottom:1px solid var(--border-color);';
                html += `
                    <tr style="${rs}">
                        <td style="padding:6px 10px;">${label}</td>
                        <td style="text-align:right; padding:6px 10px;">${dt.period_a.toFixed(2)}</td>
                        <td style="text-align:right; padding:6px 10px;">${dt.period_b.toFixed(2)}</td>
                        <td style="text-align:right; padding:6px 10px;">${deltaCell(dt.delta, dt.percent)}</td>
                    </tr>`;
            }
            html += '</tbody></table></div>';
        });

        const gt = data.totals.details.total_cost;
        const gtColor = gt.delta > 0 ? '#ef4444' : gt.delta < 0 ? '#10b981' : '#6b7280';
        html += `
            <div style="padding:16px; background:${gt.delta > 0 ? '#fef2f2' : gt.delta < 0 ? '#f0fdf4' : 'var(--bg-page)'}; border-radius:8px; border:2px solid ${gtColor}40; display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:12px;">
                <div>
                    <div style="font-size:13px; color:var(--text-secondary);">Общий итог по всем объектам</div>
                    <div style="font-size:13px; margin-top:4px;">
                        ${escapeHtml(data.period_a.name)}: <strong>${gt.period_a.toFixed(2)} ₽</strong>
                        &nbsp;→&nbsp;
                        ${escapeHtml(data.period_b.name)}: <strong>${gt.period_b.toFixed(2)} ₽</strong>
                    </div>
                </div>
                <div style="text-align:right;">
                    <div style="font-size:24px; font-weight:700; color:${gtColor};">${gt.delta > 0 ? '+' : ''}${gt.delta.toFixed(2)} ₽</div>
                    <div style="font-size:14px; color:${gtColor};">${gt.delta > 0 ? '+' : ''}${gt.percent}%</div>
                </div>
            </div>`;
        this.dom.compareContainer.innerHTML = html;
    },

    // ====================================================================
    // ТАБ «АНАЛИЗ ЖИЛФОНДА» — перенесено из housing.js
    // Backend endpoint тот же: /rooms/analyze.
    // ====================================================================
    async runHousingAnalysis() {
        if (!this.dom.housingResults) return;
        const btn = this.dom.btnHousingRun;
        if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Сканируем…'; }
        this.dom.housingResults.innerHTML = `
            <div style="text-align:center; padding: 40px; color:#666;">
                <i class="fa-solid fa-spinner fa-spin" style="font-size:24px; color:#f59e0b;"></i>
                <div style="margin-top:10px;">Сканируем базу данных…</div>
            </div>`;
        try {
            const data = await api.get('/rooms/analyze');
            this._renderHousingAnalysis(data);
        } catch (e) {
            this.dom.housingResults.innerHTML =
                `<div style="color:red; text-align:center; padding: 20px;">Ошибка получения данных: ${escapeHtml(e.message)}</div>`;
        } finally {
            if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-magnifying-glass-chart"></i> Запустить анализ'; }
        }
    },

    _renderHousingAnalysis(data) {
        const sections = [
            { key: 'unattached_users', icon: '👻', title: 'Жильцы без комнаты (Ошибки привязки)', color: '#dc2626', bg: '#fef2f2' },
            { key: 'shared_billing',   icon: '👥', title: 'Совместное проживание (Раздельные Л/С в одной комнате)', color: '#3b82f6', bg: '#eff6ff' },
            { key: 'overcrowded',      icon: '⚠️', title: 'Перенаселение (Платят за большее кол-во человек, чем есть мест)', color: '#ea580c', bg: '#fff7ed' },
            { key: 'zero_area',        icon: '📏', title: 'Нулевая площадь (Ошибка заполнения)', color: '#b45309', bg: '#fef3c7' },
            { key: 'underpopulated',   icon: '🛏️', title: 'Свободные места (Платят за меньшее кол-во человек, чем мест)', color: '#10b981', bg: '#ecfdf5' },
            { key: 'empty_rooms',      icon: '🚪', title: 'Пустые комнаты (Никто не прописан)', color: '#6b7280', bg: '#f3f4f6' },
        ];

        let html = '';
        let totalIssues = 0;
        sections.forEach(sec => {
            const items = data[sec.key];
            if (items && items.length > 0) {
                totalIssues += items.length;
                html += `
                    <div style="margin-bottom: 20px; border: 1px solid ${sec.color}40; border-radius: 8px; overflow: hidden;">
                        <div style="background: ${sec.bg}; padding: 12px 15px; border-bottom: 1px solid ${sec.color}40; font-weight: bold; color: ${sec.color}; display: flex; align-items: center; gap: 10px;">
                            <span style="font-size: 20px;">${sec.icon}</span> ${sec.title} (${items.length})
                        </div>
                        <ul style="list-style: none; padding: 0; margin: 0; background: white; max-height: 250px; overflow-y: auto;">
                            ${items.map(item => `
                                <li style="padding: 12px 15px; border-bottom: 1px solid #f3f4f6; font-size: 13px;">
                                    <strong style="color: #1f2937; font-size: 14px;">${escapeHtml(item.title)}</strong>
                                    <div style="color: #6b7280; margin-top: 4px; line-height: 1.4;">${escapeHtml(item.desc)}</div>
                                </li>`).join('')}
                        </ul>
                    </div>`;
            }
        });

        if (!totalIssues) {
            html = `
                <div style="text-align:center; padding: 60px 20px; background: white; border-radius: 8px; border: 1px solid #e5e7eb;">
                    <div style="font-size: 40px; margin-bottom: 15px;">✅</div>
                    <div style="color:#10b981; font-size: 18px; font-weight: bold;">Аномалий не обнаружено!</div>
                    <div style="color:#6b7280; font-size: 14px; margin-top: 5px;">Жилфонд и пользователи в идеальном состоянии.</div>
                </div>`;
        }
        this.dom.housingResults.innerHTML = html;
    },

    // ====================================================================
    // ТАБ «ОБСЛУЖИВАНИЕ» — ручная очистка старых строк gsheets
    // Запускается синхронно через /admin/analyzer/gsheets/cleanup-now.
    // Автоочистка идёт по расписанию (Celery beat 03:00 ежедневно).
    // ====================================================================
    // ====================================================================
    // ТАБ «СВЕРКА 1С» — расхождения readings vs 1С-долги в активном периоде
    // Backend endpoint: GET /financier/debts/reconcile
    // ====================================================================
    async runReconcile() {
        if (!this.dom.reconcileResults) return;
        const btn = this.dom.btnReconcileRun;
        if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Сверяем…'; }
        this.dom.reconcileResults.innerHTML = `
            <div style="text-align:center; padding: 40px; color:#666;">
                <i class="fa-solid fa-spinner fa-spin" style="font-size:24px; color:#f59e0b;"></i>
                <div style="margin-top:10px;">Сравниваем показания и долги…</div>
            </div>`;
        try {
            const data = await api.get('/financier/debts/reconcile');
            this._renderReconcile(data);
        } catch (e) {
            this.dom.reconcileResults.innerHTML =
                `<div style="color:var(--danger-color); padding: 20px;">Ошибка: ${escapeHtml(e.message)}</div>`;
        } finally {
            if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-arrows-rotate"></i> Запустить сверку'; }
        }
    },

    _renderReconcile(data) {
        if (!data.period) {
            this.dom.reconcileResults.innerHTML = `
                <div style="padding:24px; text-align:center; background:#f3f4f6; border-radius:8px; color:var(--text-secondary);">
                    Нет активного периода — сверка невозможна.
                </div>`;
            return;
        }

        const fmtMoney = (v) => Number(v || 0).toLocaleString('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + ' ₽';

        const r1 = data.readings_without_debts || [];
        const r2 = data.debts_without_readings || [];
        const r3 = data.last_import_not_found || [];

        const sec1 = `
            <div style="margin-bottom: 20px; border: 1px solid #bbf7d040; border-radius: 8px; overflow: hidden;">
                <div style="background: #ecfdf5; padding: 12px 15px; border-bottom: 1px solid #bbf7d0; font-weight: bold; color: #059669; display: flex; align-items: center; gap: 10px;">
                    <span style="font-size: 20px;">✅</span> Начислено — без долга в 1С (оплачено?) (${r1.length})
                </div>
                ${r1.length ? `
                <div style="max-height:260px; overflow-y:auto; background:white;">
                    <table style="width:100%; border-collapse:collapse; font-size:13px;">
                        <thead style="position:sticky; top:0; background:#f9fafb; z-index:1;">
                            <tr style="text-align:left;">
                                <th style="padding:8px 12px;">Жилец</th>
                                <th style="padding:8px 12px;">Общ. / комн.</th>
                                <th style="padding:8px 12px; text-align:right;">Начислено</th>
                            </tr>
                        </thead>
                        <tbody>${r1.map(x => `
                            <tr style="border-bottom:1px solid #f3f4f6;">
                                <td style="padding:7px 12px; font-weight:600;">${escapeHtml(x.username)}</td>
                                <td style="padding:7px 12px; color:var(--text-secondary); font-size:12px;">${escapeHtml(x.dormitory || '—')} / ${escapeHtml(x.room_number || '—')}</td>
                                <td style="padding:7px 12px; text-align:right; font-family:monospace;">${fmtMoney(x.total_cost)}</td>
                            </tr>`).join('')}</tbody>
                    </table>
                </div>` : `<div style="padding:14px; color:var(--text-secondary); background:white; font-size:12px;">Все начисленные получили долг из 1С.</div>`}
            </div>`;

        const sec2 = `
            <div style="margin-bottom: 20px; border: 1px solid #fecaca40; border-radius: 8px; overflow: hidden;">
                <div style="background: #fef2f2; padding: 12px 15px; border-bottom: 1px solid #fecaca; font-weight: bold; color: #dc2626; display: flex; align-items: center; gap: 10px;">
                    <span style="font-size: 20px;">⚠️</span> Долги в БД — без утверждённого показания (${r2.length})
                </div>
                ${r2.length ? `
                <div style="max-height:260px; overflow-y:auto; background:white;">
                    <table style="width:100%; border-collapse:collapse; font-size:13px;">
                        <thead style="position:sticky; top:0; background:#f9fafb; z-index:1;">
                            <tr style="text-align:left;">
                                <th style="padding:8px 12px;">Жилец</th>
                                <th style="padding:8px 12px;">Общ. / комн.</th>
                                <th style="padding:8px 12px; text-align:right;">Долг 209</th>
                                <th style="padding:8px 12px; text-align:right;">Долг 205</th>
                            </tr>
                        </thead>
                        <tbody>${r2.map(x => `
                            <tr style="border-bottom:1px solid #f3f4f6;">
                                <td style="padding:7px 12px; font-weight:600;">${escapeHtml(x.username)}</td>
                                <td style="padding:7px 12px; color:var(--text-secondary); font-size:12px;">${escapeHtml(x.dormitory || '—')} / ${escapeHtml(x.room_number || '—')}</td>
                                <td style="padding:7px 12px; text-align:right; font-family:monospace; color:#c0392b;">${fmtMoney(x.debt_209)}</td>
                                <td style="padding:7px 12px; text-align:right; font-family:monospace; color:#d35400;">${fmtMoney(x.debt_205)}</td>
                            </tr>`).join('')}</tbody>
                    </table>
                </div>` : `<div style="padding:14px; color:var(--text-secondary); background:white; font-size:12px;">Все долги в БД соответствуют утверждённым показаниям.</div>`}
            </div>`;

        const sec3 = `
            <div style="margin-bottom: 20px; border: 1px solid #fde68a40; border-radius: 8px; overflow: hidden;">
                <div style="background: #fef3c7; padding: 12px 15px; border-bottom: 1px solid #fde68a; font-weight: bold; color: #92400e; display: flex; align-items: center; gap: 10px;">
                    <span style="font-size: 20px;">👻</span> Не найдены в 1С — последний импорт (${r3.length})
                </div>
                ${r3.length ? `
                <div style="max-height:260px; overflow-y:auto; background:white; padding:10px 14px; font-size:13px;">
                    ${r3.map(fio => `<div style="padding:4px 0; border-bottom:1px solid #f3f4f6;">${escapeHtml(fio)}</div>`).join('')}
                    ${data.last_import_id ? `
                    <div style="margin-top:10px; text-align:right;">
                        <a href="#" onclick="event.preventDefault(); window.__openDebtsNotFound && window.__openDebtsNotFound(${data.last_import_id});" style="color:#7c3aed; font-size:12px;">
                            Открыть во вкладке «Долги 1С» →
                        </a>
                    </div>` : ''}
                </div>` : `<div style="padding:14px; color:var(--text-secondary); background:white; font-size:12px;">Все ФИО из последнего импорта привязаны.</div>`}
            </div>`;

        const totalIssues = r1.length + r2.length + r3.length;
        const header = `
            <div style="padding:10px 14px; background:#eff6ff; border:1px solid #bfdbfe; border-radius:6px; margin-bottom:14px; font-size:13px;">
                Период: <b>${escapeHtml(data.period.name)}</b>
                · Всего расхождений: <b style="color:${totalIssues ? '#d97706' : '#059669'};">${totalIssues}</b>
            </div>`;

        this.dom.reconcileResults.innerHTML = header + sec1 + sec2 + sec3;
    },

    async runGsheetsCleanup() {
        const days = Number(this.dom.cleanupDays?.value) || 365;
        if (days < 30) {
            toast('Минимум 30 дней — защита от случайной полной очистки', 'warning');
            return;
        }
        if (!confirm(`Удалить завершённые строки импорта старше ${days} дней?\nДействие необратимо (pending / conflict / unmatched не затрагиваются).`)) return;

        const btn = this.dom.btnCleanupNow;
        if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Очистка…'; }
        this.dom.cleanupResult.innerHTML = '';
        try {
            const res = await api.post('/admin/analyzer/gsheets/cleanup-now', { retention_days: days });
            this.dom.cleanupResult.innerHTML = `
                <div style="padding:12px 16px; background:#ecfdf5; border:1px solid #a7f3d0; border-radius:8px; color:#065f46;">
                    <b><i class="fa-solid fa-check"></i> Готово.</b>
                    Удалено строк: <b>${res.deleted}</b>. Порог: старше ${res.retention_days} дней (до ${new Date(res.cutoff).toLocaleDateString('ru-RU')}).
                </div>`;
            toast(`Удалено ${res.deleted} строк`, 'success');
        } catch (e) {
            this.dom.cleanupResult.innerHTML =
                `<div style="padding:12px 16px; background:#fef2f2; border:1px solid #fecaca; border-radius:8px; color:#991b1b;">Ошибка: ${escapeHtml(e.message)}</div>`;
        } finally {
            if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-trash"></i> Очистить сейчас'; }
        }
    },

    // ====================================================================
    // ДВОЙНИКИ — поиск и удаление дубликатов approved-квитанций в периоде
    // ====================================================================
    async findDuplicates() {
        const btn = this.dom.btnFindDuplicates;
        if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Поиск…'; }
        this.dom.duplicatesResult.innerHTML = '';
        try {
            const res = await api.get('/admin/analyzer/duplicate-readings');
            this._renderDuplicates(res);
        } catch (e) {
            this.dom.duplicatesResult.innerHTML =
                `<div style="padding:12px 16px; background:#fef2f2; border:1px solid #fecaca; border-radius:8px; color:#991b1b;">Ошибка: ${escapeHtml(e.message)}</div>`;
        } finally {
            if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-magnifying-glass"></i> Найти двойников (активный период)'; }
        }
    },

    _renderDuplicates(data) {
        const groups = data.duplicates || [];
        if (!groups.length) {
            this.dom.duplicatesResult.innerHTML = `
                <div style="padding:12px 16px; background:#ecfdf5; border:1px solid #a7f3d0; border-radius:8px; color:#065f46;">
                    <i class="fa-solid fa-check-circle"></i> Дубликатов не найдено — у каждого жильца в этом периоде ровно одна утверждённая квитанция.
                </div>`;
            return;
        }
        const header = `
            <div style="padding:10px 14px; background:#fef3c7; border:1px solid #fde68a; border-radius:8px; color:#92400e; margin-bottom:12px;">
                <b><i class="fa-solid fa-triangle-exclamation"></i> Найдено ${groups.length} ${groups.length === 1 ? 'жилец' : 'жильцов'}</b>
                с дубликатами квитанций. Всего лишних reading-ов: ${data.total_extra_readings}.
                Просмотрите и удалите ненужные через кнопку «Удалить» (нельзя отменить).
            </div>`;
        const groupsHtml = groups.map(g => `
            <details style="margin-bottom:10px; border:1px solid var(--border-color); border-radius:8px; background:#fff;">
                <summary style="padding:10px 14px; cursor:pointer; background:#f9fafb; font-weight:600;">
                    ${escapeHtml(g.username)}
                    <span style="font-size:12px; color:var(--text-secondary); font-weight:normal; margin-left:6px;">
                        · ${escapeHtml(g.room_label)} · ${g.readings.length} квитанций
                    </span>
                </summary>
                <div style="padding:10px 14px;">
                    <table style="width:100%; border-collapse:collapse; font-size:12px;">
                        <thead style="background:var(--bg-page); font-size:11px; color:var(--text-secondary); text-transform:uppercase;">
                            <tr>
                                <th style="text-align:left; padding:6px 8px;">ID</th>
                                <th style="text-align:left; padding:6px 8px;">Создан</th>
                                <th style="text-align:left; padding:6px 8px;">Флаги</th>
                                <th style="text-align:right; padding:6px 8px;">ГВС</th>
                                <th style="text-align:right; padding:6px 8px;">ХВС</th>
                                <th style="text-align:right; padding:6px 8px;">Свет</th>
                                <th style="text-align:right; padding:6px 8px;">209</th>
                                <th style="text-align:right; padding:6px 8px;">205</th>
                                <th style="text-align:right; padding:6px 8px;">Итого</th>
                                <th style="padding:6px 8px;"></th>
                            </tr>
                        </thead>
                        <tbody>
                            ${g.readings.map(r => `
                                <tr style="border-bottom:1px solid var(--border-color);">
                                    <td style="padding:6px 8px; font-family:monospace;">#${r.id}</td>
                                    <td style="padding:6px 8px; font-size:11px;">${escapeHtml(fmtDateTime(r.created_at))}</td>
                                    <td style="padding:6px 8px;">
                                        <span style="font-family:monospace; font-size:11px; background:#f3f4f6; padding:1px 5px; border-radius:3px;">${escapeHtml(r.anomaly_flags || '—')}</span>
                                    </td>
                                    <td style="padding:6px 8px; text-align:right; font-family:monospace;">${r.hot_water != null ? r.hot_water.toFixed(3) : '—'}</td>
                                    <td style="padding:6px 8px; text-align:right; font-family:monospace;">${r.cold_water != null ? r.cold_water.toFixed(3) : '—'}</td>
                                    <td style="padding:6px 8px; text-align:right; font-family:monospace;">${r.electricity != null ? r.electricity.toFixed(3) : '—'}</td>
                                    <td style="padding:6px 8px; text-align:right; font-family:monospace;">${r.total_209.toFixed(2)}</td>
                                    <td style="padding:6px 8px; text-align:right; font-family:monospace;">${r.total_205.toFixed(2)}</td>
                                    <td style="padding:6px 8px; text-align:right; font-family:monospace; font-weight:600;">${r.total_cost.toFixed(2)}</td>
                                    <td style="padding:6px 8px; text-align:right;">
                                        <button data-delete-dup-id="${r.id}" class="action-btn danger-btn" style="padding:3px 8px; font-size:11px;" title="Удалить эту квитанцию">
                                            <i class="fa-solid fa-trash"></i>
                                        </button>
                                    </td>
                                </tr>
                            `).join('')}
                        </tbody>
                    </table>
                </div>
            </details>
        `).join('');
        this.dom.duplicatesResult.innerHTML = header + groupsHtml;
    },

    async deleteDuplicateReading(readingId) {
        if (!readingId) return;
        if (!confirm(
            `Удалить квитанцию #${readingId}?\n\n` +
            'Reading и связанные данные будут удалены безвозвратно. ' +
            'Используйте только если знаете что это дубликат — оригинал останется.'
        )) return;
        try {
            await api.delete(`/admin/readings/${readingId}`);
            toast(`Квитанция #${readingId} удалена`, 'success');
            // Перезагружаем список — удалённый ряд уйдёт, оставшиеся группы
            // могут перестать быть «дубликатами» если в группе осталась 1.
            this.findDuplicates();
        } catch (e) {
            toast('Ошибка: ' + e.message, 'error');
        }
    },

    // ====================================================================
    // INBOX — модальный экран «список всех проблем требующих внимания»
    //
    // Открывается кликом по KPI-карточке с data-inbox-filter. Показывает
    // таблицу из /api/admin/analyzer/inbox: на каждой строке кнопка
    // «Решить» делает рекомендованное действие, плюс альтернативы.
    // ====================================================================

    async openInbox(filter) {
        // Маппинг с UI-фильтра карточки на параметры /inbox API.
        // 'anomalies' / 'gsheets' идут как kind, 'critical' — как severity.
        const params = new URLSearchParams({
            period_days: String(this.state.period || 30),
            limit: '200',
        });
        if (filter === 'gsheets') {
            params.set('kind', 'gsheets');
        } else if (filter === 'critical') {
            params.set('kind', 'anomalies');
            params.set('severity', 'critical');
        } else if (filter === 'anomalies') {
            params.set('kind', 'anomalies');
        }
        // Сначала открываем модалку с loading-state, чтобы было видно
        // что клик сработал — потом подменяем содержимое на данные.
        this._renderInboxLoading(filter);
        try {
            const data = await api.get(`/admin/analyzer/inbox?${params.toString()}`);
            this._renderInboxModal(data, filter);
        } catch (e) {
            this._renderInboxError(e.message);
        }
    },

    _inboxOverlayId: 'analyzerInboxOverlay',

    _renderInboxLoading(filter) {
        this._ensureInboxOverlay();
        const body = document.getElementById('analyzerInboxBody');
        if (body) body.innerHTML = `
            <div style="text-align:center; padding:60px; color:var(--text-secondary);">
                <i class="fa-solid fa-spinner fa-spin" style="font-size:24px;"></i>
                <div style="margin-top:12px;">Загрузка списка проблем…</div>
            </div>`;
    },

    _renderInboxError(msg) {
        const body = document.getElementById('analyzerInboxBody');
        if (body) body.innerHTML = `
            <div style="padding:16px; background:#fef2f2; border:1px solid #fecaca; border-radius:8px; color:#991b1b;">
                Ошибка загрузки: ${escapeHtml(msg)}
            </div>`;
    },

    _ensureInboxOverlay() {
        if (document.getElementById(this._inboxOverlayId)) return;
        const overlay = document.createElement('div');
        overlay.id = this._inboxOverlayId;
        overlay.style.cssText = `
            position:fixed; inset:0; background:rgba(0,0,0,0.45); z-index:1000;
            display:flex; align-items:center; justify-content:center; padding:20px;
        `;
        overlay.innerHTML = `
            <div style="background:var(--bg-card); border-radius:12px; max-width:1200px; width:100%;
                        max-height:90vh; display:flex; flex-direction:column; box-shadow:0 12px 40px rgba(0,0,0,0.3);">
                <div style="padding:14px 20px; border-bottom:1px solid var(--border-color);
                            display:flex; align-items:center; justify-content:space-between; gap:12px;">
                    <div style="display:flex; align-items:center; gap:10px;">
                        <i class="fa-solid fa-inbox" style="color:#dc2626;"></i>
                        <h3 id="analyzerInboxTitle" style="margin:0; font-size:16px;">Список проблем</h3>
                        <span id="analyzerInboxSummary" style="font-size:12px; color:var(--text-secondary);"></span>
                    </div>
                    <button id="analyzerInboxClose" class="secondary-btn" style="padding:6px 12px;">
                        <i class="fa-solid fa-xmark"></i> Закрыть
                    </button>
                </div>
                <div id="analyzerInboxBody" style="padding:14px 20px; overflow:auto; flex:1;"></div>
            </div>`;
        document.body.appendChild(overlay);
        // Закрытие по клику на overlay (но не на содержимое) + кнопке + Escape.
        overlay.addEventListener('click', (e) => {
            if (e.target === overlay) this._closeInboxModal();
        });
        document.getElementById('analyzerInboxClose').addEventListener('click', () => this._closeInboxModal());
        this._inboxEscHandler = (e) => {
            if (e.key === 'Escape' && document.getElementById(this._inboxOverlayId)) {
                this._closeInboxModal();
            }
        };
        document.addEventListener('keydown', this._inboxEscHandler);

        // Делегация click для resolve-кнопок внутри tablицы.
        document.getElementById('analyzerInboxBody').addEventListener('click', (e) => {
            const btn = e.target.closest('button[data-resolve-action]');
            if (!btn) return;
            const issueId = btn.dataset.issueId;
            const action = btn.dataset.resolveAction;
            this._resolveIssue(issueId, action, btn);
        });
    },

    _closeInboxModal() {
        const el = document.getElementById(this._inboxOverlayId);
        if (el) el.remove();
        if (this._inboxEscHandler) {
            document.removeEventListener('keydown', this._inboxEscHandler);
            this._inboxEscHandler = null;
        }
        // После закрытия — рефрешим KPI: счётчики могли поменяться.
        this.loadDashboard();
    },

    _renderInboxModal(data, filter) {
        const titleMap = {
            anomalies: 'Аномалии — список проблем',
            critical:  'Критические аномалии (score ≥ 80)',
            gsheets:   'GSheets — конфликты сопоставления',
        };
        const title = titleMap[filter] || 'Список проблем';
        document.getElementById('analyzerInboxTitle').textContent = title;
        const sum = data.summary || {};
        document.getElementById('analyzerInboxSummary').textContent =
            `Всего: ${data.total} · anomalies: ${sum.anomalies || 0} · gsheets: ${sum.gsheets || 0} · critical: ${sum.critical || 0}`;

        const body = document.getElementById('analyzerInboxBody');
        if (!data.issues || !data.issues.length) {
            body.innerHTML = `
                <div style="text-align:center; padding:60px; color:var(--text-secondary);">
                    <i class="fa-solid fa-check-circle" style="font-size:32px; color:#10b981;"></i>
                    <div style="margin-top:12px; font-size:14px;">За период проблем не найдено — всё чисто.</div>
                </div>`;
            return;
        }
        body.innerHTML = `
            <table style="width:100%; border-collapse:collapse; font-size:13px;">
                <thead style="position:sticky; top:0; background:var(--bg-page); z-index:1;">
                    <tr style="font-size:11px; color:var(--text-secondary); text-transform:uppercase; letter-spacing:.3px;">
                        <th style="text-align:left; padding:8px 10px; width:90px;">Severity</th>
                        <th style="text-align:left; padding:8px 10px;">Проблема</th>
                        <th style="text-align:left; padding:8px 10px;">Контекст</th>
                        <th style="text-align:right; padding:8px 10px; width:300px;">Действия</th>
                    </tr>
                </thead>
                <tbody>
                    ${data.issues.map(i => this._inboxRowHtml(i)).join('')}
                </tbody>
            </table>`;
    },

    _inboxRowHtml(issue) {
        const sev = issue.severity || 'medium';
        const sevColors = {
            critical: { bg: '#fef2f2', fg: '#991b1b', border: '#fecaca' },
            high:     { bg: '#fff7ed', fg: '#9a3412', border: '#fed7aa' },
            medium:   { bg: '#fefce8', fg: '#854d0e', border: '#fde68a' },
            low:      { bg: '#f9fafb', fg: '#6b7280', border: '#e5e7eb' },
        };
        const c = sevColors[sev] || sevColors.medium;
        const sevBadge = `<span style="display:inline-block; padding:3px 8px; border-radius:4px; background:${c.bg}; color:${c.fg}; border:1px solid ${c.border}; font-size:10px; font-weight:600; text-transform:uppercase;">${escapeHtml(sev)}</span>`;

        const ctx = issue.context || {};
        let contextHtml = '';
        if (issue.kind === 'anomaly') {
            const flag = `<span style="font-family:monospace; font-size:11px; background:#f3f4f6; padding:1px 5px; border-radius:3px;">${escapeHtml(issue.flag || '')}</span>`;
            const cost = ctx.total_cost != null
                ? `<span style="color:var(--text-secondary);"> · ${(ctx.total_cost).toLocaleString('ru-RU', {minimumFractionDigits:2, maximumFractionDigits:2})} ₽</span>`
                : '';
            contextHtml = `
                <div><b>${escapeHtml(ctx.username || '—')}</b>${cost}</div>
                <div style="color:var(--text-secondary); font-size:11px;">
                    ${escapeHtml(ctx.dormitory || '—')} · комн. ${escapeHtml(ctx.room_number || '—')} · ${escapeHtml(ctx.period_name || '—')}
                </div>
                <div style="margin-top:3px;">${flag} <span style="color:var(--text-tertiary); font-size:11px;">score=${issue.score}</span></div>`;
        } else if (issue.kind === 'gsheets') {
            contextHtml = `
                <div><b>${escapeHtml(ctx.fio_raw || '—')}</b></div>
                <div style="color:var(--text-secondary); font-size:11px;">
                    ${escapeHtml(ctx.dormitory_raw || '—')} · комн. ${escapeHtml(ctx.room_raw || '—')}
                </div>
                ${ctx.conflict_reason ? `<div style="color:#9a3412; font-size:11px; margin-top:3px;"><i class="fa-solid fa-triangle-exclamation"></i> ${escapeHtml(ctx.conflict_reason)}</div>` : ''}`;
        }

        // Кнопка suggested action — primary, остальные available — secondary.
        const actionLabels = {
            dismiss: { ru: 'Не аномалия', icon: 'check', desc: 'Жилец живёт нормально, флаг ложный — больше не показывать' },
            verify:  { ru: 'Открыть запись', icon: 'eye',  desc: 'Открыть reading в реестре для ручной проверки' },
            ignore:  { ru: 'Пропустить',    icon: 'forward', desc: 'Видел, оставляю как есть' },
            reject:  { ru: 'Отклонить',     icon: 'ban',  desc: 'Отбросить строку из импорта' },
            open:    { ru: 'Открыть',       icon: 'arrow-up-right-from-square', desc: 'Открыть форму сопоставления' },
        };
        const suggested = issue.suggested_action;
        const available = issue.available_actions || [];

        const renderBtn = (act, primary) => {
            const meta = actionLabels[act] || { ru: act, icon: 'play', desc: '' };
            const cls = primary ? 'primary-btn' : 'secondary-btn';
            return `
                <button class="${cls}" data-resolve-action="${escapeHtml(act)}" data-issue-id="${escapeHtml(issue.issue_id)}"
                        title="${escapeHtml(meta.desc)}" style="padding:5px 10px; font-size:12px; margin-left:4px;">
                    <i class="fa-solid fa-${meta.icon}"></i> ${escapeHtml(meta.ru)}
                </button>`;
        };
        const buttons = [
            renderBtn(suggested, true),
            ...available.filter(a => a !== suggested).map(a => renderBtn(a, false)),
        ].join('');

        return `
            <tr style="border-bottom:1px solid var(--border-color);">
                <td style="padding:10px;">${sevBadge}</td>
                <td style="padding:10px;">${escapeHtml(issue.title || '')}</td>
                <td style="padding:10px;">${contextHtml}</td>
                <td style="padding:10px; text-align:right; white-space:nowrap;">${buttons}</td>
            </tr>`;
    },

    async _resolveIssue(issueId, action, btn) {
        // Действия 'open' / 'verify' — пока показываем подсказку с ID
        // записи, чтобы админ нашёл её в реестре «Сверка показаний» или
        // в форме GSheets-матчинга. Deep-link на конкретный reading
        // потребует поддержку в router'е app.js — сделаем отдельным шагом.
        if (action === 'verify' || action === 'open') {
            const [kind, rawId] = issueId.split(':');
            if (kind === 'anomaly') {
                toast(`Откройте «Сверка показаний» и найдите reading #${rawId}`, 'info');
                return;
            }
            if (kind === 'gsheets') {
                toast(`Откройте GSheets-импорт и найдите строку #${rawId}`, 'info');
                return;
            }
        }

        const origHtml = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';
        try {
            const res = await api.post('/admin/analyzer/inbox/resolve', {
                issue_id: issueId,
                action: action,
            });
            toast(`✓ ${action}: применено`, 'success');
            // Перерисуем модалку чтобы убрать обработанную строку.
            // Простой путь: убрать строку из DOM локально.
            const row = btn.closest('tr');
            if (row) {
                row.style.transition = 'opacity 0.25s, background 0.25s';
                row.style.background = '#d1fae5';
                setTimeout(() => {
                    row.style.opacity = '0';
                    setTimeout(() => row.remove(), 250);
                }, 200);
            }
        } catch (e) {
            toast(`Ошибка: ${e.message}`, 'error');
            btn.disabled = false;
            btn.innerHTML = origHtml;
        }
    },

    // ============================================================
    // ЗАБЛОКИРОВАННЫЕ ПОКАЗАНИЯ (DATA_OVERFLOW_RESET)
    // Endpoint: GET /admin/analyzer/stuck-drafts
    // Действия: DELETE /admin/readings/{id} | модал «Проверка расчёта»
    // ============================================================
    async loadStuckDrafts() {
        if (!this.dom.stuckContainer) return;
        this.dom.stuckContainer.innerHTML =
            '<p style="color:var(--text-secondary); padding:20px;">Загружаем…</p>';
        try {
            const data = await api.get('/admin/analyzer/stuck-drafts');
            this._renderStuck(data);
        } catch (e) {
            this.dom.stuckContainer.innerHTML =
                `<p style="color:var(--danger-color);">Ошибка: ${escapeHtml(e.message)}</p>`;
        }
    },

    _renderStuck(data) {
        const items = data.items || [];
        if (items.length === 0) {
            this.dom.stuckContainer.innerHTML = `
                <div style="text-align:center; padding:30px; color:var(--success-color);">
                    <i class="fa-solid fa-check-circle" style="font-size:24px;"></i>
                    <div style="margin-top:8px; font-weight:600;">Заблокированных показаний нет</div>
                    <div style="margin-top:4px; font-size:12px; color:var(--text-secondary);">
                        Все нереалистичные подачи разобраны или их пока не было.
                    </div>
                </div>`;
            return;
        }
        const rows = items.map(it => {
            const addr = it.dormitory_name
                ? `${escapeHtml(it.dormitory_name)}/${escapeHtml(String(it.room_number || ''))}`
                : '—';
            return `
                <tr style="border-bottom:1px solid var(--border-color);">
                    <td style="padding:10px 12px;">
                        <div style="font-weight:600;">${escapeHtml(it.full_name || it.username || '—')}</div>
                        <div style="font-size:11px; color:var(--text-secondary);">${escapeHtml(it.username || '')} · ${addr}</div>
                    </td>
                    <td style="padding:10px 12px; font-size:12px; color:var(--text-secondary);">${escapeHtml(it.period_name || '—')}</td>
                    <td style="padding:10px 12px; text-align:right; font-family:monospace; font-size:12px;">
                        <div>${Number(it.hot_water).toFixed(3)} / ${Number(it.cold_water).toFixed(3)}</div>
                        <div style="color:var(--text-tertiary);">${Number(it.electricity).toFixed(3)}</div>
                    </td>
                    <td style="padding:10px 12px; text-align:right;">
                        <span style="color:var(--danger-color); font-weight:700;">
                            ${Number(it.total_cost_was).toLocaleString('ru-RU', {minimumFractionDigits:2, maximumFractionDigits:2})} ₽
                        </span>
                        <div style="font-size:11px; color:var(--text-tertiary);">(до сброса)</div>
                    </td>
                    <td style="padding:10px 12px; text-align:right; white-space:nowrap;">
                        <button class="action-btn secondary-btn" data-stuck-detail="${it.reading_id}"
                                style="padding:5px 10px; font-size:12px;" title="Открыть «Проверка расчёта»">
                            <i class="fa-solid fa-magnifying-glass"></i>
                        </button>
                        <button class="action-btn success-btn" data-stuck-approve="${it.reading_id}"
                                style="padding:5px 10px; font-size:12px;" title="Утвердить — пересчитать с актуальной формулой и принять">
                            <i class="fa-solid fa-check"></i>
                        </button>
                        <button class="action-btn danger-btn" data-stuck-delete="${it.reading_id}"
                                style="padding:5px 10px; font-size:12px;" title="Удалить запись">
                            <i class="fa-regular fa-trash-can"></i>
                        </button>
                    </td>
                </tr>`;
        }).join('');

        this.dom.stuckContainer.innerHTML = `
            <div style="overflow-x:auto; border:1px solid var(--border-color); border-radius:8px;">
                <table style="width:100%; min-width:680px;">
                    <thead>
                        <tr style="background:var(--bg-page);">
                            <th style="text-align:left; padding:10px 12px;">Жилец / адрес</th>
                            <th style="text-align:left; padding:10px 12px;">Период</th>
                            <th style="text-align:right; padding:10px 12px;">ГВС / ХВС<br><span style="font-weight:400; color:var(--text-tertiary); font-size:11px;">Электр.</span></th>
                            <th style="text-align:right; padding:10px 12px;">Итог был</th>
                            <th style="text-align:right; padding:10px 12px;">Действия</th>
                        </tr>
                    </thead>
                    <tbody>${rows}</tbody>
                </table>
            </div>
            <p class="hint-text" style="margin-top:10px; font-size:12px;">
                Всего: <b>${items.length}</b>. Лупа — открыть детали и принять с коррекцией;
                корзина — удалить запись (жилец сможет переподать).
            </p>`;
    },

    async deleteStuckReading(readingId) {
        if (!readingId) return;
        const confirmed = window.confirm(
            'Удалить эту запись? Жилец/админ сможет затем переподать корректные показания. ' +
            'Действие необратимо.'
        );
        if (!confirmed) return;
        try {
            await api.delete(`/admin/readings/${readingId}`);
            toast('Запись удалена', 'success');
            this.loadStuckDrafts();
        } catch (e) {
            toast('Ошибка удаления: ' + e.message, 'error');
        }
    },

    /** Утвердить заблокированный черновик — approve_single пересчитает
     *  cost_* через текущую формулу (с фильтрацией synth-prev — см.
     *  is_meaningful_prev), снимет DATA_OVERFLOW_RESET флаг, поставит
     *  is_approved=True. После этого reading становится «реальным
     *  предыдущим» для следующего периода — баг с delta +1468 кубов
     *  исчезает (см. Капранов май 2026). */
    async approveStuckReading(readingId) {
        if (!readingId) return;
        const confirmed = window.confirm(
            'Утвердить эту запись? Система пересчитает стоимость по текущей формуле ' +
            'с актуальным тарифом и текущим «предыдущим» (с пропуском AUTO_GENERATED / ' +
            'DATA_OVERFLOW_RESET). Если итог получится разумным (≤ 100 000 ₽) — запись ' +
            'станет утверждённой и появится в истории жильца как «реальное предыдущее» ' +
            'для следующего периода.'
        );
        if (!confirmed) return;
        try {
            // approve_single требует correction_data (ApproveRequest) — передаём нули.
            // Все коррекции = 0 → используется raw delta.
            await api.post(`/admin/approve/${readingId}`, {
                hot_correction: 0,
                cold_correction: 0,
                electricity_correction: 0,
                sewage_correction: 0,
            });
            toast('Запись утверждена. Рекомендую запустить «Перерасчёт периода» для следующего месяца — там delta станет нормальной.', 'success');
            this.loadStuckDrafts();
        } catch (e) {
            // Sanity check может отказать если итог > 100k — в этом случае
            // approve_single бросает 400 с понятным сообщением.
            toast('Не удалось утвердить: ' + e.message, 'error');
        }
    },
};
