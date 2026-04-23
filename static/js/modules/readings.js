// static/js/modules/readings.js
//
// «Реестр показаний 2.0» (волны 1 + 2):
//   * KPI-строка над таблицей (всего / чистые / подозрительные / критичные / сумма)
//   * Фильтры: период, уровень риска, источник, тип флага, только аномалии
//   * Новые колонки: источник подачи, дельты vs прошлой
//   * Сортировка по клику на колонки (username, anomaly_score, total_cost)
//   * Цветовая полоса слева по уровню риска
//   * Раскрытие строки → панель решения с историей, соседями, флагами и рекомендацией

import { api } from '../core/api.js';
import { el, toast, setLoading, showPrompt } from '../core/dom.js';
import { TableController } from '../core/table-controller.js';
import { createBadges, showHistoryModal, showImportResultModal, openApproveModal } from './readings-ui.js';

function escapeHtml(s) {
    if (s == null) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
function fmtMoney(v) {
    return Number(v || 0).toLocaleString('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function fmtNum(v, digits = 3) {
    return Number(v || 0).toFixed(digits);
}
function fmtDate(iso) {
    if (!iso) return '—';
    try { return new Date(iso).toLocaleDateString('ru-RU'); } catch { return iso; }
}

// Риск → цвет для бордера слева и бейджа
function riskStyle(score) {
    const s = Number(score || 0);
    if (s >= 80) return { color: '#dc2626', label: 'Критично', bg: '#fee2e2' };
    if (s >= 30) return { color: '#f59e0b', label: 'Подозр.',  bg: '#fef3c7' };
    return { color: '#10b981', label: 'Норма', bg: '#d1fae5' };
}

const SOURCE_META = {
    user:           { icon: '📱', label: 'Приложение' },
    gsheets:        { icon: '📊', label: 'GSheets' },
    auto:           { icon: '🤖', label: 'Авто-ген' },
    one_time:       { icon: '💰', label: 'Одноразовый' },
    meter_replace:  { icon: '🔧', label: 'Замена' },
};

export const ReadingsModule = {
    table: null,
    isInitialized: false,
    // Раскрытые строки — в Set по reading_id, чтобы сохранять состояние между refresh
    expanded: new Set(),
    // Кэш топ-флагов для выпадающего фильтра
    topFlagsCache: [],

    init() {
        this.cacheDOM();
        if (!this.isInitialized) {
            this.bindEvents();
            this.isInitialized = true;
        }
        this.loadActivePeriod();
        this.loadStats();
        this.loadPeriodsList();
        this.initTable();
    },

    cacheDOM() {
        this.dom = {
            btnRefresh: document.getElementById('btnRefreshReadings'),
            btnBulk: document.getElementById('btnBulkApprove'),
            filterAnomalies: document.getElementById('filterAnomalies'),
            filterPeriod: document.getElementById('readingsFilterPeriod'),
            filterRisk: document.getElementById('readingsFilterRisk'),
            filterSource: document.getElementById('readingsFilterSource'),
            filterFlag: document.getElementById('readingsFilterFlag'),
            search: document.getElementById('readingsSearch'),
            kpis: document.getElementById('readingsKPIs'),
            periodActive: document.getElementById('periodActiveState'),
            periodClosed: document.getElementById('periodClosedState'),
            periodLabel: document.getElementById('activePeriodLabel'),
            btnImport: document.getElementById('btnImportReadings'),
            inputImport: document.getElementById('importReadingsFile'),
            tableBody: document.getElementById('readingsTableBody'),
        };
    },

    bindEvents() {
        this.dom.btnRefresh?.addEventListener('click', () => this.refreshAll());
        this.dom.btnImport?.addEventListener('click', () => this.importReadings());
        this.dom.btnBulk?.addEventListener('click', () => this.bulkApprove());

        const applyFilters = () => {
            if (!this.table) return;
            this.expanded.clear();
            this.table.state.page = 1;
            this.table.load();
            this.loadStats();
        };
        this.dom.filterAnomalies?.addEventListener('change', applyFilters);
        this.dom.filterPeriod?.addEventListener('change', applyFilters);
        this.dom.filterRisk?.addEventListener('change', applyFilters);
        this.dom.filterSource?.addEventListener('change', applyFilters);
        this.dom.filterFlag?.addEventListener('change', applyFilters);

        // Поиск по ФИО / комнате (общий TableController уже делает дебаунс,
        // но здесь мы дёргаем state.search вручную, т.к. у нас собственный input).
        let searchTimer;
        this.dom.search?.addEventListener('input', (e) => {
            clearTimeout(searchTimer);
            searchTimer = setTimeout(() => {
                if (!this.table) return;
                this.table.state.search = e.target.value || '';
                this.table.state.page = 1;
                this.table.load();
            }, 400);
        });

        // Сортировка по заголовкам
        document.querySelectorAll('[data-reading-sort]').forEach(th => {
            th.addEventListener('click', () => {
                if (!this.table) return;
                const field = th.dataset.readingSort;
                if (this.table.state.sortBy === field) {
                    this.table.state.sortDir = this.table.state.sortDir === 'asc' ? 'desc' : 'asc';
                } else {
                    this.table.state.sortBy = field;
                    this.table.state.sortDir = field === 'anomaly_score' ? 'desc' : 'asc';
                }
                this.table.state.page = 1;
                this.table.load();
            });
        });

        // Делегирование на tbody — клик по шеврону или действиям.
        this.dom.tableBody?.addEventListener('click', (e) => {
            const toggleBtn = e.target.closest('button[data-reading-expand]');
            if (toggleBtn) {
                const id = Number(toggleBtn.dataset.readingExpand);
                this.toggleExpand(id);
                return;
            }
        });
    },

    refreshAll() {
        this.expanded.clear();
        this.table?.refresh();
        this.loadStats();
    },

    async loadActivePeriod() {
        try {
            const data = await api.get('/admin/periods/active');
            if (data && data.name) {
                if (this.dom.periodActive) this.dom.periodActive.style.display = 'flex';
                if (this.dom.periodClosed) this.dom.periodClosed.style.display = 'none';
                if (this.dom.periodLabel) this.dom.periodLabel.textContent = data.name;
            } else {
                if (this.dom.periodActive) this.dom.periodActive.style.display = 'none';
                if (this.dom.periodClosed) this.dom.periodClosed.style.display = 'flex';
            }
        } catch (e) { /* тихо */ }
    },

    async loadPeriodsList() {
        try {
            const periods = await api.get('/admin/periods/history');
            if (!this.dom.filterPeriod || !periods?.length) return;
            const prev = this.dom.filterPeriod.value;
            this.dom.filterPeriod.innerHTML = '<option value="">Активный период</option>';
            periods.forEach(p => {
                const o = document.createElement('option');
                o.value = p.id;
                o.textContent = p.name + (p.is_active ? ' (акт.)' : '');
                this.dom.filterPeriod.appendChild(o);
            });
            if (prev) this.dom.filterPeriod.value = prev;
        } catch (e) { /* тихо */ }
    },

    // ===========================================================
    // KPI
    // ===========================================================
    async loadStats() {
        if (!this.dom.kpis) return;
        try {
            const params = new URLSearchParams();
            if (this.dom.filterPeriod?.value) params.set('period_id', this.dom.filterPeriod.value);
            const s = await api.get(`/admin/readings/stats?${params}`);
            this.renderStats(s);
            this.fillFlagsFilter(s.top_flags || []);
        } catch (e) {
            this.dom.kpis.innerHTML =
                `<div style="padding:12px; color:var(--danger-color); grid-column:1/-1;">Ошибка аналитики: ${escapeHtml(e.message)}</div>`;
        }
    },

    renderStats(s) {
        // Компактные «pill»-карточки: значок + число + подпись в ОДНУ строку.
        // Раньше были вертикальные карточки ~90px высоты — для реестра над
        // таблицей это было слишком громоздко. Теперь ~36px.
        const pill = (bg, color, icon, value, label) => `
            <div style="background:${bg}; border:1px solid ${color}33; border-radius:8px; padding:6px 12px;
                        display:flex; align-items:center; gap:8px; white-space:nowrap; line-height:1.2;">
                <span style="font-size:14px;">${icon}</span>
                <span style="font-size:15px; font-weight:700; color:${color};">${value}</span>
                <span style="font-size:11px; color:var(--text-secondary); text-transform:uppercase; letter-spacing:0.3px;">${escapeHtml(label)}</span>
            </div>
        `;
        this.dom.kpis.innerHTML = [
            pill('#eff6ff', '#2563eb', '📋', s.total, 'ждут'),
            pill('#ecfdf5', '#10b981', '🟢', s.clean, 'чистые'),
            pill('#fef3c7', '#f59e0b', '🟡', s.suspicious, 'подозр.'),
            pill('#fef2f2', '#dc2626', '🔴', s.critical, 'крит.'),
            pill('#fff7ed', '#ea580c', '⚠️', s.anomalies, 'с флагами'),
            pill('#f5f3ff', '#7c3aed', '💰', fmtMoney(s.sum_cost) + ' ₽', 'к начисл.'),
        ].join('');
    },

    fillFlagsFilter(topFlags) {
        if (!this.dom.filterFlag) return;
        this.topFlagsCache = topFlags;
        const prev = this.dom.filterFlag.value;
        this.dom.filterFlag.innerHTML = '<option value="">Все флаги</option>';
        topFlags.forEach(f => {
            const o = document.createElement('option');
            o.value = f.code;
            o.textContent = `${f.label} (${f.count})`;
            this.dom.filterFlag.appendChild(o);
        });
        if (prev) this.dom.filterFlag.value = prev;
    },

    // ===========================================================
    // Table
    // ===========================================================
    initTable() {
        this.table = new TableController({
            endpoint: '/admin/readings',
            dom: { tableBody: 'readingsTableBody', prevBtn: 'btnPrev', nextBtn: 'btnNext', pageInfo: 'pageIndicator' },

            getExtraParams: () => {
                const p = {};
                if (this.dom.filterAnomalies?.checked) p.anomalies_only = true;
                if (this.dom.filterPeriod?.value) p.period_id = this.dom.filterPeriod.value;
                if (this.dom.filterRisk?.value) p.risk_level = this.dom.filterRisk.value;
                if (this.dom.filterSource?.value) p.source = this.dom.filterSource.value;
                if (this.dom.filterFlag?.value) p.flag_code = this.dom.filterFlag.value;
                return p;
            },

            renderRow: (r) => this.renderRow(r),
        });
        this.table.init();
    },

    renderRow(r) {
        const risk = riskStyle(r.anomaly_score);
        const src = SOURCE_META[r.source] || { icon: '❓', label: r.source };
        const isOpen = this.expanded.has(r.id);

        // Бейдж редактирований
        let editBadge = null;
        if (r.edit_count > 1 && r.edit_history && r.edit_history.length > 0) {
            const lastEdit = r.edit_history[r.edit_history.length - 1].date;
            editBadge = el('span', {
                title: `Последняя правка: ${lastEdit}`,
                style: { marginLeft: '8px', fontSize: '11px', background: '#fef08a', color: '#b45309', padding: '2px 6px', borderRadius: '12px', fontWeight: 'bold', cursor: 'help' }
            }, `⚠️ ×${r.edit_count}`);
        }

        // Статус ячейка
        const statusCell = el('td', { style: { borderLeft: `3px solid ${risk.color}` } });
        if (r.anomaly_flags === 'PENDING') {
            statusCell.appendChild(el('div', { style: { fontSize: '12px', color: '#6b7280', fontStyle: 'italic' } }, '⏳ Считаем риски...'));
        } else if (r.anomaly_score > 0 || (r.anomaly_flags && r.anomaly_flags !== '')) {
            statusCell.appendChild(el('div', {
                style: { fontSize: '12px', fontWeight: 'bold', color: risk.color, marginBottom: '4px' }
            }, `${r.anomaly_score}/100 · ${risk.label}`));
        } else {
            statusCell.appendChild(el('div', { style: { fontSize: '12px', color: '#10b981' } }, '✅ Норма'));
        }
        const badges = createBadges(r.anomaly_details, r.anomaly_flags);
        if (badges) statusCell.appendChild(badges);

        // Δ-ячейка — растёт ли счётчик
        const fmtDelta = (v) => {
            const n = Number(v || 0);
            if (n === 0) return '<span style="color:#9ca3af; font-size:11px;">0</span>';
            const color = n > 0 ? '#16a34a' : '#dc2626';
            const sign = n > 0 ? '+' : '';
            return `<span style="color:${color}; font-size:11px; font-weight:600;">${sign}${n.toFixed(2)}</span>`;
        };
        const deltaCell = el('td', { class: 'text-right' });
        deltaCell.innerHTML = `
            <div style="display:flex; flex-direction:column; gap:1px; line-height:1.2;">
                <span>🔥 ${fmtDelta(r.delta_hot)}</span>
                <span>💧 ${fmtDelta(r.delta_cold)}</span>
                <span>⚡ ${fmtDelta(r.delta_elect)}</span>
            </div>
        `;

        // Строка
        const tr = el('tr', { 'data-reading-row': String(r.id) },
            // Чеврон раскрытия
            el('td', {},
                el('button', {
                    class: 'btn-icon',
                    'data-reading-expand': String(r.id),
                    style: { background: 'transparent', border: 'none', cursor: 'pointer', fontSize: '13px', color: 'var(--text-secondary)' },
                    title: isOpen ? 'Свернуть' : 'Детали и рекомендация',
                }, isOpen ? '▼' : '▶')
            ),
            // Жилец / Объект
            el('td', {},
                el('div', { style: { fontWeight: '600', display: 'flex', alignItems: 'center' } }, r.username, editBadge),
                el('div', { style: { fontSize: '11px', color: '#888' } },
                    (r.dormitory || 'Общ. —') + (r.room_number ? `, ${r.room_number}` : '') +
                    (r.period_name ? ` · ${r.period_name}` : '')
                )
            ),
            statusCell,
            // Источник
            el('td', { style: { fontSize: '12px', whiteSpace: 'nowrap' } },
                `${src.icon} ${escapeHtml(src.label)}`),
            el('td', { class: 'text-right' }, fmtNum(r.cur_hot ?? r.hot_water)),
            el('td', { class: 'text-right' }, fmtNum(r.cur_cold ?? r.cold_water)),
            el('td', { class: 'text-right' }, fmtNum(r.cur_elect ?? r.electricity)),
            deltaCell,
            el('td', { class: 'text-right', style: { color: '#27ae60', fontWeight: 'bold' } }, `${fmtMoney(r.total_cost)} ₽`),
            el('td', { class: 'text-center' },
                el('button', {
                    class: 'btn-icon', title: 'История правок',
                    style: { marginRight: '4px', background: '#f3f4f6', borderColor: '#d1d5db' },
                    onclick: () => showHistoryModal(r)
                }, '🕒'),
                el('button', {
                    class: 'btn-icon', title: 'Корректировка', style: { marginRight: '4px' },
                    onclick: () => this.openAdjustmentModal(r.user_id, r.username, r.dormitory)
                }, '±'),
                el('button', {
                    class: 'btn-icon btn-check', title: 'Проверить и утвердить',
                    onclick: () => openApproveModal(r, () => this.refreshAll())
                }, '✓')
            )
        );

        // Если раскрыта — добавим детальную строку СРАЗУ под основной.
        // TableController рендерит через renderRow → возвращаем фрагмент из 2 строк.
        if (isOpen) {
            const frag = document.createDocumentFragment();
            frag.appendChild(tr);
            const detailRow = document.createElement('tr');
            detailRow.className = 'reading-detail-row';
            detailRow.dataset.detailFor = String(r.id);
            detailRow.innerHTML = `<td colspan="10" style="background:#f9fafb; padding:16px 24px; border-left:3px solid ${risk.color};"><div style="text-align:center; color:var(--text-secondary);"><i class="fa-solid fa-spinner fa-spin"></i> Загрузка…</div></td>`;
            frag.appendChild(detailRow);
            // Асинхронно подгружаем контекст — сразу после вставки в DOM.
            setTimeout(() => this.loadDecisionContext(r.id), 0);
            return frag;
        }
        return tr;
    },

    async toggleExpand(readingId) {
        if (this.expanded.has(readingId)) {
            this.expanded.delete(readingId);
        } else {
            this.expanded.add(readingId);
        }
        // Проще всего — перерисовать текущую страницу. Пагинация и фильтры сохранятся.
        this.table?.render(this.table.lastItems || []);
    },

    async loadDecisionContext(readingId) {
        const detailRow = document.querySelector(`tr.reading-detail-row[data-detail-for="${readingId}"]`);
        if (!detailRow) return;
        try {
            const ctx = await api.get(`/admin/readings/${readingId}/decision-context`);
            detailRow.innerHTML = `<td colspan="10" style="background:#f9fafb; padding:16px 22px; border-left:3px solid ${ctx.recommendation.color};">${this.renderDecisionPanel(ctx)}</td>`;
        } catch (e) {
            detailRow.innerHTML = `<td colspan="10" style="background:#fef2f2; padding:12px 22px; color:var(--danger-color);">Ошибка загрузки контекста: ${escapeHtml(e.message)}</td>`;
        }
    },

    renderDecisionPanel(ctx) {
        const rec = ctx.recommendation;
        const cur = ctx.current;
        const n = ctx.neighbors;
        const hist = ctx.history || [];

        // Рекомендация — главный баннер сверху панели
        const recIcon = { approve: '✅', review: '⚠️', reject: '🛑' }[rec.verdict] || 'ℹ️';
        const recBanner = `
            <div style="background:${rec.color}14; border:1px solid ${rec.color}44; border-left:4px solid ${rec.color}; border-radius:8px; padding:12px 16px; margin-bottom:14px;">
                <div style="display:flex; align-items:center; gap:10px;">
                    <span style="font-size:20px;">${recIcon}</span>
                    <div style="flex:1;">
                        <div style="font-weight:700; color:${rec.color}; font-size:14px;">${escapeHtml(rec.label)}</div>
                        <div style="font-size:12px; color:var(--text-secondary); margin-top:2px;">${escapeHtml(rec.reason)}</div>
                    </div>
                </div>
            </div>
        `;

        // История — мини-таблица 4-х предыдущих показаний
        const histHtml = hist.length ? `
            <table style="width:100%; border-collapse:collapse; font-size:12px; background:white; border:1px solid var(--border-color); border-radius:6px; overflow:hidden;">
                <thead style="background:#f3f4f6;">
                    <tr>
                        <th style="padding:6px 10px; text-align:left; font-size:11px; color:var(--text-secondary);">Дата</th>
                        <th style="padding:6px 10px; text-align:right; font-size:11px; color:var(--text-secondary);">🔥 ГВС</th>
                        <th style="padding:6px 10px; text-align:right; font-size:11px; color:var(--text-secondary);">💧 ХВС</th>
                        <th style="padding:6px 10px; text-align:right; font-size:11px; color:var(--text-secondary);">⚡ Свет</th>
                        <th style="padding:6px 10px; text-align:right; font-size:11px; color:var(--text-secondary);">Сумма</th>
                    </tr>
                </thead>
                <tbody>${hist.map(h => `
                    <tr>
                        <td style="padding:6px 10px; font-size:12px;">${escapeHtml(fmtDate(h.created_at))}</td>
                        <td style="padding:6px 10px; text-align:right; font-family:monospace;">${fmtNum(h.hot_water)}</td>
                        <td style="padding:6px 10px; text-align:right; font-family:monospace;">${fmtNum(h.cold_water)}</td>
                        <td style="padding:6px 10px; text-align:right; font-family:monospace;">${fmtNum(h.electricity)}</td>
                        <td style="padding:6px 10px; text-align:right; font-family:monospace; font-weight:600;">${fmtMoney(h.total_cost)} ₽</td>
                    </tr>`).join('')}</tbody>
            </table>
        ` : `<div style="color:var(--text-secondary); font-style:italic; padding:10px;">Предыдущих утверждённых показаний нет.</div>`;

        // Соседи
        const nbrs = n.sample_size > 0 ? `
            <div style="display:grid; grid-template-columns:repeat(auto-fit,minmax(120px,1fr)); gap:8px; margin-top:4px;">
                <div style="background:white; border:1px solid var(--border-color); border-radius:6px; padding:8px 10px;">
                    <div style="font-size:10px; color:var(--text-secondary); text-transform:uppercase;">🔥 ГВС сред.</div>
                    <div style="font-size:13px; font-weight:600;">${fmtNum(n.avg_hot)}</div>
                </div>
                <div style="background:white; border:1px solid var(--border-color); border-radius:6px; padding:8px 10px;">
                    <div style="font-size:10px; color:var(--text-secondary); text-transform:uppercase;">💧 ХВС сред.</div>
                    <div style="font-size:13px; font-weight:600;">${fmtNum(n.avg_cold)}</div>
                </div>
                <div style="background:white; border:1px solid var(--border-color); border-radius:6px; padding:8px 10px;">
                    <div style="font-size:10px; color:var(--text-secondary); text-transform:uppercase;">⚡ Свет сред.</div>
                    <div style="font-size:13px; font-weight:600;">${fmtNum(n.avg_elect)}</div>
                </div>
                <div style="background:white; border:1px solid var(--border-color); border-radius:6px; padding:8px 10px;">
                    <div style="font-size:10px; color:var(--text-secondary); text-transform:uppercase;">💰 ₽ сред.</div>
                    <div style="font-size:13px; font-weight:600;">${fmtMoney(n.avg_total_cost)}</div>
                </div>
            </div>
            <div style="font-size:11px; color:var(--text-secondary); margin-top:6px;">
                по ${n.sample_size} ${n.sample_size === 1 ? 'жильцу' : (n.sample_size < 5 ? 'жильцам' : 'жильцам')} в общежитии «${escapeHtml(n.dormitory)}»
            </div>
        ` : `<div style="color:var(--text-secondary); font-style:italic; padding:6px 0;">Не с кем сравнить — единственная подача в общежитии за этот период.</div>`;

        // Флаги с описаниями
        const sevColor = { high: '#dc2626', medium: '#f59e0b', low: '#6b7280' };
        const sevLabel = { high: 'критично', medium: 'важно', low: 'инфо' };
        const flagsHtml = ctx.flags.length ? ctx.flags.map(f => `
            <div style="display:flex; align-items:flex-start; gap:8px; padding:6px 10px; border:1px solid ${sevColor[f.severity]}33; border-left:3px solid ${sevColor[f.severity]}; background:white; border-radius:6px; margin-bottom:5px;">
                <span style="background:${sevColor[f.severity]}14; color:${sevColor[f.severity]}; font-size:10px; font-weight:700; padding:2px 6px; border-radius:8px; flex-shrink:0;">${escapeHtml(sevLabel[f.severity])}</span>
                <div style="flex:1;">
                    <div style="font-weight:600; font-size:12px;">${escapeHtml(f.label)}</div>
                    <div style="font-size:11px; color:var(--text-secondary); font-family:monospace;">${escapeHtml(f.code)}</div>
                </div>
            </div>
        `).join('') : `<div style="color:var(--text-secondary); font-style:italic;">Флагов анализатора нет.</div>`;

        // Ключевые числа текущей подачи
        const cur_stats = `
            <div style="display:grid; grid-template-columns:repeat(auto-fit,minmax(110px,1fr)); gap:8px;">
                <div><div style="font-size:10px; color:var(--text-secondary);">🔥 ГВС</div><div style="font-weight:700;">${fmtNum(cur.hot_water)} <span style="font-size:10px; color:#6b7280;">(Δ${cur.delta_hot >= 0 ? '+' : ''}${cur.delta_hot.toFixed(2)})</span></div></div>
                <div><div style="font-size:10px; color:var(--text-secondary);">💧 ХВС</div><div style="font-weight:700;">${fmtNum(cur.cold_water)} <span style="font-size:10px; color:#6b7280;">(Δ${cur.delta_cold >= 0 ? '+' : ''}${cur.delta_cold.toFixed(2)})</span></div></div>
                <div><div style="font-size:10px; color:var(--text-secondary);">⚡ Свет</div><div style="font-weight:700;">${fmtNum(cur.electricity)} <span style="font-size:10px; color:#6b7280;">(Δ${cur.delta_elect >= 0 ? '+' : ''}${cur.delta_elect.toFixed(2)})</span></div></div>
                <div><div style="font-size:10px; color:var(--text-secondary);">Начислено</div><div style="font-weight:700; color:#059669;">${fmtMoney(cur.total_cost)} ₽</div></div>
                <div><div style="font-size:10px; color:var(--text-secondary);">209 / 205</div><div style="font-weight:700; font-size:11px;">${fmtMoney(cur.total_209)} / ${fmtMoney(cur.total_205)}</div></div>
            </div>
        `;

        return `
            ${recBanner}
            <div style="display:grid; grid-template-columns:1fr 1fr; gap:16px;">
                <div>
                    <h5 style="margin:0 0 8px 0; font-size:12px; color:var(--text-secondary); text-transform:uppercase;"><i class="fa-solid fa-chart-column"></i> Текущая подача</h5>
                    ${cur_stats}
                </div>
                <div>
                    <h5 style="margin:0 0 8px 0; font-size:12px; color:var(--text-secondary); text-transform:uppercase;"><i class="fa-solid fa-people-group"></i> Соседи по общежитию</h5>
                    ${nbrs}
                </div>
            </div>
            <h5 style="margin:16px 0 8px 0; font-size:12px; color:var(--text-secondary); text-transform:uppercase;"><i class="fa-solid fa-clock-rotate-left"></i> Последние 4 утверждённые подачи</h5>
            ${histHtml}
            ${ctx.flags.length > 0 ? `
                <h5 style="margin:16px 0 8px 0; font-size:12px; color:var(--text-secondary); text-transform:uppercase;"><i class="fa-solid fa-triangle-exclamation"></i> Найденные аномалии</h5>
                ${flagsHtml}
            ` : ''}
        `;
    },

    // ===========================================================
    // Actions
    // ===========================================================
    async importReadings() {
        const file = this.dom.inputImport?.files?.[0];
        if (!file) return toast('Сначала выберите файл Excel', 'info');
        const formData = new FormData();
        formData.append('file', file);
        setLoading(this.dom.btnImport, true, 'Загрузка...');
        try {
            const res = await api.post('/admin/readings/import', formData);
            showImportResultModal(res);
            if (this.dom.inputImport) this.dom.inputImport.value = '';
            this.refreshAll();
        } catch (e) {
            toast('Ошибка импорта: ' + e.message, 'error');
        } finally {
            setLoading(this.dom.btnImport, false, '📥 Загрузить');
        }
    },

    async openAdjustmentModal(userId, username, dormitory) {
        const displayInfo = dormitory ? `${username} (${dormitory})` : username;
        const amountStr = await showPrompt(`Корректировка: ${displayInfo}`, 'Введите сумму (например -500 для скидки или 1000 для долга):');
        if (!amountStr) return;
        const amount = parseFloat(amountStr.replace(',', '.'));
        if (isNaN(amount)) return toast('Нужно ввести корректное число!', 'error');
        const desc = await showPrompt('Причина', 'Укажите основание:', 'Перерасчет');
        if (!desc) return;
        try {
            await api.post('/admin/adjustments', { user_id: userId, amount, description: desc });
            toast('Корректировка сохранена', 'success');
            this.refreshAll();
        } catch (e) { toast(e.message, 'error'); }
    },

    async bulkApprove() {
        if (!confirm('Утвердить все безопасные черновики (anomaly_score < порога)?')) return;
        setLoading(this.dom.btnBulk, true);
        try {
            const res = await api.post('/admin/approve-bulk', {});
            toast(`Утверждено записей: ${res.approved_count}`, 'success');
            this.refreshAll();
        } catch (e) {
            toast(e.message, 'error');
        } finally {
            setLoading(this.dom.btnBulk, false);
        }
    }
};
