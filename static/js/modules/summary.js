// static/js/modules/summary.js
//
// «Финансовая отчётность» (v2) + «Анализ периодов» (предпросмотр + сравнение).
//
// Что нового vs v1:
//  * новый endpoint /admin/summary/v2 с финансовыми анализаторами
//    (DEBT_GROWING, BILL_SPIKE, BILL_DROP, ZERO_BILL, OVERPAY_SUSPECT,
//    HIGH_BILL_PER_PERSON, MISSING_RECEIPT);
//  * KPI-карточки сверху, топ-должники / топ-переплатчики;
//  * фильтры (все / должники / переплаты / аномалии / нет квитанции),
//    поиск по ФИО или номеру комнаты;
//  * группировка по общежитиям с раскрывающимися карточками;
//  * sparkline за 6 месяцев, Δ vs прошлый период с цветом;
//  * квитанция PDF одной кнопкой прямо в строке.
//
// «Анализ периодов» — два таба в одной accordion-секции
// (раньше были две: «Сравнение» и «Предпросмотр закрытия»).

import { api } from '../core/api.js';
import { setLoading, toast } from '../core/dom.js';

function esc(s) {
    if (s === null || s === undefined) return '';
    return String(s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function fmtMoney(v) {
    const n = Number(v || 0);
    return n.toLocaleString('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + ' ₽';
}

const FLAG_META = {
    DEBT_GROWING:         { color: '#dc2626', bg: '#fee2e2', icon: 'fa-arrow-trend-up',    title: 'Долг растёт' },
    BILL_SPIKE:           { color: '#f59e0b', bg: '#fef3c7', icon: 'fa-arrow-up',          title: 'Резкий рост счёта' },
    BILL_DROP:            { color: '#3b82f6', bg: '#dbeafe', icon: 'fa-arrow-down',        title: 'Резкое падение счёта' },
    ZERO_BILL:            { color: '#dc2626', bg: '#fee2e2', icon: 'fa-circle-exclamation', title: 'Нулевая квитанция' },
    OVERPAY_SUSPECT:      { color: '#7c3aed', bg: '#ede9fe', icon: 'fa-coins',             title: 'Подозрительная переплата' },
    HIGH_BILL_PER_PERSON: { color: '#f59e0b', bg: '#fef3c7', icon: 'fa-user-large',        title: 'Высокий счёт на 1 чел.' },
    MISSING_RECEIPT:      { color: '#dc2626', bg: '#fee2e2', icon: 'fa-receipt',           title: 'Нет квитанции' },
    WRONG_BILLING_MODE:   { color: '#f59e0b', bg: '#fef3c7', icon: 'fa-circle-question',   title: 'Несоответствие типа жильца' },
};

/** Простой SVG-sparkline. Берёт массив чисел, рисует ломаную. */
function sparkSvg(values, width = 80, height = 24) {
    if (!values || !values.length) return '';
    const min = Math.min(...values);
    const max = Math.max(...values);
    const range = max - min || 1;
    const step = values.length > 1 ? width / (values.length - 1) : width;
    const points = values.map((v, i) => {
        const x = i * step;
        const y = height - ((v - min) / range) * (height - 4) - 2;
        return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(' ');
    const last = values[values.length - 1];
    const prev = values.length >= 2 ? values[values.length - 2] : last;
    const color = last > prev ? '#dc2626' : last < prev ? '#10b981' : '#6b7280';
    return `
      <svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" style="display:inline-block; vertical-align:middle;">
        <polyline fill="none" stroke="${color}" stroke-width="1.5" points="${points}"/>
        <circle cx="${(values.length - 1) * step}" cy="${height - ((last - min) / range) * (height - 4) - 2}" r="2" fill="${color}"/>
      </svg>`;
}

export const SummaryModule = {
    isInitialized: false,
    periodsCache: [],
    state: {
        selectedPeriodId: null,
        controller: null,
        pollTimer: null,
        // Фильтры финансовой отчётности
        filter: 'all',     // 'all'|'debtors'|'overpaid'|'anomaly'|'missing'
        search: '',
        expandedDorms: new Set(),  // раскрытые карточки общежитий
        // Текущая загруженная сводка v2
        currentSummary: null,
        // Активный таб «Анализ периодов»: 'preview'|'compare'
        periodsTab: 'preview',
    },
    dom: {},

    init() {
        if (!this.dom.container) {
            this.cacheDOM();
            if (!this.isInitialized) {
                this.bindEvents();
                this.isInitialized = true;
            }
        }
        this.loadPeriods();
    },

    cacheDOM() {
        this.dom = {
            // Финансовая отчётность
            container:      document.getElementById('summaryContainer'),
            kpis:           document.getElementById('summaryKPIs'),
            topRow:         document.getElementById('summaryTopRow'),
            search:         document.getElementById('summarySearch'),
            periodSelector: document.getElementById('summaryPeriodSelector'),
            btnRefresh:     document.getElementById('btnRefreshSummary'),
            btnExcel:       document.getElementById('btnDownloadExcel'),
            btnZip:         document.getElementById('btnDownloadZip'),
            // Анализ периодов: предпросмотр
            closePreviewContainer: document.getElementById('closePreviewContainer'),
            btnLoadPreview:        document.getElementById('btnLoadPreview'),
            // Анализ периодов: сравнение
            comparePeriodA:    document.getElementById('comparePeriodA'),
            comparePeriodB:    document.getElementById('comparePeriodB'),
            btnCompare:        document.getElementById('btnCompare'),
            compareContainer:  document.getElementById('compareContainer'),
            // Табы
            tabPreview: document.getElementById('tabPreview'),
            tabCompare: document.getElementById('tabCompare'),
            paneClosePreview: document.getElementById('paneClosePreview'),
            paneCompare: document.getElementById('paneCompare'),
        };
    },

    bindEvents() {
        this.dom.btnRefresh?.addEventListener('click', () => this.loadData());
        this.dom.btnExcel?.addEventListener('click', () => this.downloadExcel());
        this.dom.btnZip?.addEventListener('click', () => this.downloadZip());
        this.dom.btnLoadPreview?.addEventListener('click', () => this.loadClosePreview());
        this.dom.btnCompare?.addEventListener('click', () => this.runComparison());

        // Фильтры финансовой отчётности
        document.querySelectorAll('[data-summary-filter]').forEach(btn => {
            btn.addEventListener('click', () => {
                this.state.filter = btn.dataset.summaryFilter;
                document.querySelectorAll('[data-summary-filter]').forEach(b => {
                    b.classList.toggle('primary-btn',   b === btn);
                    b.classList.toggle('secondary-btn', b !== btn);
                });
                this.loadData();
            });
        });

        // Дебаунс поиска
        let t = null;
        this.dom.search?.addEventListener('input', () => {
            clearTimeout(t);
            t = setTimeout(() => {
                this.state.search = this.dom.search.value.trim();
                this.loadData();
            }, 350);
        });

        // Делегирование клика для раскрытия карточек общежитий и PDF
        this.dom.container?.addEventListener('click', (e) => {
            const head = e.target.closest('[data-toggle-dorm]');
            if (head) {
                const name = head.dataset.toggleDorm;
                if (this.state.expandedDorms.has(name)) {
                    this.state.expandedDorms.delete(name);
                } else {
                    this.state.expandedDorms.add(name);
                }
                this.renderSummary();
                return;
            }
            const pdf = e.target.closest('button[data-pdf-id]');
            if (pdf) this.downloadReceipt(Number(pdf.dataset.pdfId));
        });

        // Табы «Анализ периодов»
        this.dom.tabPreview?.addEventListener('click', () => this._setPeriodsTab('preview'));
        this.dom.tabCompare?.addEventListener('click', () => this._setPeriodsTab('compare'));
    },

    _setPeriodsTab(tab) {
        if (this.state.periodsTab === tab) return;
        this.state.periodsTab = tab;
        const isPreview = tab === 'preview';
        this.dom.paneClosePreview.style.display = isPreview ? '' : 'none';
        this.dom.paneCompare.style.display      = isPreview ? 'none' : '';
        const set = (btn, active) => {
            if (!btn) return;
            btn.classList.toggle('primary-btn', active);
            btn.classList.toggle('secondary-btn', !active);
        };
        set(this.dom.tabPreview, isPreview);
        set(this.dom.tabCompare, !isPreview);
    },

    // =====================================================
    // ПЕРИОДЫ (общая загрузка для обоих модулей)
    // =====================================================
    async loadPeriods() {
        if (!this.dom.periodSelector) return;
        this.dom.periodSelector.innerHTML = '<span style="color:var(--text-secondary); font-size:13px;">Загрузка…</span>';
        try {
            const periods = await api.get('/admin/periods/history');
            this.periodsCache = periods || [];
            if (!this.periodsCache.length) {
                this.dom.container.innerHTML = '<div style="text-align:center; padding:40px; color:var(--text-secondary);">Нет доступных периодов.</div>';
                this.dom.periodSelector.innerHTML = '<span style="color:var(--text-secondary); font-size:13px;">Нет периодов</span>';
                return;
            }
            // Селектор для финансовой отчётности
            this.dom.periodSelector.innerHTML = '';
            const sel = document.createElement('select');
            sel.style.cssText = 'padding:7px 10px; font-size:13px; min-width:240px;';
            this.periodsCache.forEach(p => {
                const opt = document.createElement('option');
                opt.value = p.id;
                opt.textContent = p.name + (p.is_active ? ' (Активный)' : '');
                sel.appendChild(opt);
            });
            sel.addEventListener('change', () => {
                this.state.selectedPeriodId = sel.value;
                this.loadData();
            });
            this.dom.periodSelector.appendChild(sel);
            this.state.selectedPeriodId = this.periodsCache[0].id;

            this.populateCompareSelectors(this.periodsCache);
            this.loadData();
        } catch (e) {
            this.dom.periodSelector.textContent = 'Ошибка загрузки периодов.';
            console.error(e);
        }
    },

    populateCompareSelectors(periods) {
        if (!this.dom.comparePeriodA || !this.dom.comparePeriodB) return;
        const fill = (selectEl) => {
            selectEl.innerHTML = '<option value="">Выберите период…</option>';
            periods.forEach(p => {
                const o = document.createElement('option');
                o.value = p.id;
                o.textContent = p.name + (p.is_active ? ' (Акт.)' : '');
                selectEl.appendChild(o);
            });
        };
        fill(this.dom.comparePeriodA);
        fill(this.dom.comparePeriodB);
        if (periods.length >= 2) {
            this.dom.comparePeriodB.value = periods[0].id;
            this.dom.comparePeriodA.value = periods[1].id;
        }
    },

    // =====================================================
    // ФИНАНСОВАЯ ОТЧЁТНОСТЬ v2
    // =====================================================
    async loadData() {
        if (!this.state.selectedPeriodId) return;
        if (this.state.controller) this.state.controller.abort();
        this.state.controller = new AbortController();

        this.dom.kpis.innerHTML = '<div style="grid-column: 1/-1; padding:14px; text-align:center; color:var(--text-secondary);">Загрузка…</div>';
        this.dom.topRow.innerHTML = '';
        this.dom.container.innerHTML = '<div style="padding:30px; text-align:center; color:var(--text-secondary);">Загрузка данных…</div>';

        const params = new URLSearchParams({ period_id: this.state.selectedPeriodId });
        if (this.state.filter === 'debtors')  params.set('only_debtors', 'true');
        if (this.state.filter === 'overpaid') params.set('only_overpaid', 'true');
        if (this.state.filter === 'anomaly')  params.set('only_anomaly', 'true');
        if (this.state.filter === 'missing')  params.set('only_missing', 'true');
        if (this.state.search) params.set('search', this.state.search);

        try {
            const data = await api.get(`/admin/summary/v2?${params}`, { signal: this.state.controller.signal });
            this.state.currentSummary = data;
            this.renderKPI(data);
            this.renderTopRow(data);
            this.renderSummary();
        } catch (e) {
            if (e.name === 'AbortError') return;
            this.dom.container.innerHTML = `<div style="padding:20px; color:var(--danger-color);">Ошибка: ${esc(e.message)}</div>`;
        }
    },

    renderKPI(d) {
        const k = d.kpi || {};
        const card = (label, value, color, hint) => `
            <div style="background:var(--bg-card); border:1px solid var(--border-color); border-radius:10px; padding:14px;">
                <div style="font-size:11px; color:var(--text-secondary); text-transform:uppercase; letter-spacing:.5px;">${esc(label)}</div>
                <div style="font-size:20px; font-weight:700; color:${color}; margin:4px 0 2px;">${value}</div>
                ${hint ? `<div style="font-size:11px; color:var(--text-tertiary);">${esc(hint)}</div>` : ''}
            </div>`;
        this.dom.kpis.innerHTML = [
            card('Всего начислено', fmtMoney(k.total_billed),     '#059669', `${k.residents_count} жильцов`),
            card('Долгов',          fmtMoney(k.total_debt),       k.total_debt > 0 ? '#dc2626' : '#10b981', 'к возврату'),
            card('Переплат',        fmtMoney(k.total_overpay),    k.total_overpay > 0 ? '#7c3aed' : '#9ca3af', 'аванс'),
            card('Аномалий',        String(k.flagged_count || 0), k.flagged_count > 0 ? '#f59e0b' : '#10b981', 'требуют внимания'),
            card('Без квитанции',   String(k.missing_count || 0), k.missing_count > 0 ? '#dc2626' : '#10b981', 'жильцы не подали'),
        ].join('');
    },

    renderTopRow(d) {
        const debtorsList = (d.top_debtors || []).slice(0, 5);
        const overList = (d.top_overpayers || []).slice(0, 5);
        const renderList = (items, color, fld) => {
            if (!items.length) {
                return '<div style="padding:14px; color:var(--text-secondary); font-size:12px;">— нет —</div>';
            }
            return items.map(r => `
                <div style="display:flex; justify-content:space-between; align-items:center; padding:6px 12px; border-bottom:1px solid var(--border-color);">
                    <div style="flex:1; min-width:0;">
                        <div style="font-weight:600; font-size:13px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${esc(r.username)}</div>
                        <div style="color:var(--text-secondary); font-size:11px;">комн. ${esc(r.room_number || '—')}</div>
                    </div>
                    <div style="font-weight:700; color:${color}; white-space:nowrap;">${fmtMoney(r[fld])}</div>
                </div>`).join('');
        };
        this.dom.topRow.innerHTML = `
            <div style="background:var(--bg-card); border:1px solid var(--border-color); border-radius:10px; overflow:hidden;">
                <div style="padding:10px 14px; background:#fee2e2; color:#991b1b; font-weight:600; font-size:13px; border-bottom:1px solid var(--border-color);">
                    <i class="fa-solid fa-arrow-trend-down"></i> Топ должников
                </div>
                ${renderList(debtorsList, '#dc2626', 'debt')}
            </div>
            <div style="background:var(--bg-card); border:1px solid var(--border-color); border-radius:10px; overflow:hidden;">
                <div style="padding:10px 14px; background:#ede9fe; color:#5b21b6; font-weight:600; font-size:13px; border-bottom:1px solid var(--border-color);">
                    <i class="fa-solid fa-coins"></i> Топ переплат
                </div>
                ${renderList(overList, '#7c3aed', 'overpayment')}
            </div>`;
    },

    renderSummary() {
        const data = this.state.currentSummary;
        if (!data) return;
        if (!data.dormitories?.length) {
            this.dom.container.innerHTML = `<div style="padding:30px; text-align:center; color:var(--text-secondary);">
                Нет данных для выбранных фильтров.
            </div>`;
            return;
        }
        this.dom.container.innerHTML = data.dormitories.map(d => this._renderDorm(d)).join('');
    },

    _renderDorm(d) {
        const expanded = this.state.expandedDorms.has(d.name);
        const color = d.flagged_count > 0 ? '#f59e0b' : '#059669';
        const debtBadge = d.total_debt > 0
            ? `<span style="background:#fee2e2; color:#991b1b; padding:2px 8px; border-radius:10px; font-size:11px; font-weight:600;">долг ${fmtMoney(d.total_debt)}</span>`
            : '';
        const flagBadge = d.flagged_count > 0
            ? `<span style="background:#fef3c7; color:#92400e; padding:2px 8px; border-radius:10px; font-size:11px; font-weight:600;">${d.flagged_count} аномалий</span>`
            : '';

        const body = expanded ? this._renderDormBody(d) : '';
        return `
        <div style="border:1px solid ${d.flagged_count > 0 ? '#fde68a' : 'var(--border-color)'}; border-radius:10px; margin-bottom:10px; overflow:hidden; background:var(--bg-card);">
            <div data-toggle-dorm="${esc(d.name)}" style="display:flex; align-items:center; gap:12px; padding:12px 16px; cursor:pointer; background:${d.flagged_count > 0 ? 'rgba(254,243,199,0.3)' : 'transparent'};">
                <i class="fa-solid fa-chevron-${expanded ? 'down' : 'right'}" style="color:var(--text-secondary); width:14px;"></i>
                <i class="fa-solid fa-building" style="color:${color}; font-size:20px;"></i>
                <div style="flex:1; min-width:0;">
                    <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
                        <strong style="font-size:15px;">${esc(d.name)}</strong>
                        ${debtBadge}
                        ${flagBadge}
                    </div>
                    <div style="color:var(--text-secondary); font-size:12px; margin-top:2px;">
                        ${d.residents_count} жильцов · начислено ${fmtMoney(d.total_billed)}
                        ${d.total_overpay > 0 ? ` · переплат ${fmtMoney(d.total_overpay)}` : ''}
                    </div>
                </div>
            </div>
            ${body}
        </div>`;
    },

    _renderDormBody(d) {
        const rows = d.residents.map(r => this._renderResidentRow(r)).join('');
        return `
            <div style="border-top:1px solid var(--border-color); overflow-x:auto;">
                <table style="width:100%; border-collapse:collapse; min-width:1100px; font-size:13px;">
                    <thead style="background:var(--bg-page); color:var(--text-secondary); font-size:11px; text-transform:uppercase;">
                        <tr>
                            <th style="text-align:left; padding:8px 10px;">Жилец</th>
                            <th style="text-align:left; padding:8px 10px;">Комната</th>
                            <th style="text-align:right; padding:8px 10px;">209 (Комм.)</th>
                            <th style="text-align:right; padding:8px 10px;">205 (Найм)</th>
                            <th style="text-align:right; padding:8px 10px;">Итого</th>
                            <th style="text-align:right; padding:8px 10px;">Δ vs прошлый</th>
                            <th style="text-align:center; padding:8px 10px;">Динамика</th>
                            <th style="text-align:right; padding:8px 10px;">Долг</th>
                            <th style="text-align:right; padding:8px 10px;">Переплата</th>
                            <th style="text-align:left; padding:8px 10px;">Флаги</th>
                            <th style="text-align:right; padding:8px 10px;"></th>
                        </tr>
                    </thead>
                    <tbody>${rows}</tbody>
                </table>
            </div>`;
    },

    _renderResidentRow(r) {
        const isMissing = !r.reading_id;
        const deltaCell = (() => {
            if (r.delta_amount === null || r.delta_amount === undefined) return '<span style="color:var(--text-tertiary);">—</span>';
            const sign = r.delta_amount > 0 ? '+' : '';
            const color = r.delta_amount > 0 ? '#dc2626' : r.delta_amount < 0 ? '#10b981' : '#6b7280';
            const arrow = r.delta_amount > 0 ? '▲' : r.delta_amount < 0 ? '▼' : '—';
            const pctText = r.delta_percent != null ? ` (${sign}${r.delta_percent.toFixed(1)}%)` : '';
            return `<span style="color:${color}; font-weight:600;">${arrow} ${sign}${r.delta_amount.toFixed(2)}</span><span style="color:${color}; font-size:11px;">${pctText}</span>`;
        })();

        const allFlags = [...(r.finance_flags || []), ...(r.meter_flags || []).slice(0, 2)];
        const flagsHtml = allFlags.map(f => {
            const m = FLAG_META[f] || { color: '#6b7280', bg: '#f3f4f6', icon: 'fa-tag', title: f };
            return `<span title="${esc(m.title)} (${esc(f)})" style="display:inline-block; padding:2px 6px; border-radius:8px; background:${m.bg}; color:${m.color}; font-size:10px; font-weight:600; margin-right:3px; margin-bottom:2px; white-space:nowrap;">
                <i class="fa-solid ${m.icon}"></i> ${esc(m.title)}
            </span>`;
        }).join('');

        const debtCell = r.debt > 0
            ? `<span style="color:#dc2626; font-weight:700;">${fmtMoney(r.debt)}</span>`
            : '<span style="color:var(--text-tertiary);">—</span>';
        const overCell = r.overpayment > 0
            ? `<span style="color:#7c3aed; font-weight:700;">${fmtMoney(r.overpayment)}</span>`
            : '<span style="color:var(--text-tertiary);">—</span>';

        const pdfBtn = r.reading_id
            ? `<button class="action-btn primary-btn" data-pdf-id="${r.reading_id}" style="padding:4px 10px; font-size:11px;" title="Скачать квитанцию PDF"><i class="fa-solid fa-file-pdf"></i></button>`
            : `<span style="color:var(--text-tertiary); font-size:11px;">—</span>`;

        const rowBg = isMissing ? 'background:rgba(254,226,226,0.4);' : '';
        return `
            <tr style="border-bottom:1px solid var(--border-color); ${rowBg}">
                <td style="padding:8px 10px;">
                    <div style="font-weight:600;">${esc(r.username)}</div>
                    <div style="color:var(--text-secondary); font-size:11px;">${esc(r.area || 0)}м² · ${r.residents_count} чел.</div>
                </td>
                <td style="padding:8px 10px; font-family:monospace; font-size:12px;">${esc(r.room_number || '—')}</td>
                <td style="padding:8px 10px; text-align:right; font-family:monospace;">${isMissing ? '—' : Number(r.total_209 || 0).toFixed(2)}</td>
                <td style="padding:8px 10px; text-align:right; font-family:monospace;">${isMissing ? '—' : Number(r.total_205 || 0).toFixed(2)}</td>
                <td style="padding:8px 10px; text-align:right; font-family:monospace; font-weight:700; color:#059669;">${isMissing ? '—' : fmtMoney(r.total_cost)}</td>
                <td style="padding:8px 10px; text-align:right;">${deltaCell}</td>
                <td style="padding:8px 10px; text-align:center;">${sparkSvg(r.sparkline)}</td>
                <td style="padding:8px 10px; text-align:right;">${debtCell}</td>
                <td style="padding:8px 10px; text-align:right;">${overCell}</td>
                <td style="padding:8px 10px;">${flagsHtml || '<span style="color:var(--text-tertiary); font-size:11px;">—</span>'}</td>
                <td style="padding:8px 10px; text-align:right;">${pdfBtn}</td>
            </tr>`;
    },

    // =====================================================
    // ПРЕДПРОСМОТР ЗАКРЫТИЯ ПЕРИОДА
    // =====================================================
    async loadClosePreview() {
        setLoading(this.dom.btnLoadPreview, true, 'Анализ…');
        this.dom.closePreviewContainer.innerHTML = '<div style="padding:20px; text-align:center; color:var(--text-secondary);">Сканируем данные…</div>';
        try {
            const data = await api.get('/admin/periods/close-preview');
            this.renderClosePreview(data);
        } catch (e) {
            this.dom.closePreviewContainer.innerHTML = `<div style="padding:16px; color:var(--danger-color);">Ошибка: ${esc(e.message)}</div>`;
        } finally {
            setLoading(this.dom.btnLoadPreview, false, 'Загрузить отчёт');
        }
    },

    renderClosePreview(data) {
        const pct = data.total_occupied_rooms > 0
            ? Math.round(data.rooms_with_readings / data.total_occupied_rooms * 100) : 0;
        const progressColor = pct >= 80 ? '#10b981' : pct >= 50 ? '#f59e0b' : '#ef4444';

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
                        <td style="padding:8px; font-weight:500;">${esc(d.name)}</td>
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
                ${card('Предв. итого', fmtMoney(data.estimated_total), '#1f2937', '#f9fafb')}
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

    // =====================================================
    // СРАВНЕНИЕ ПЕРИОДОВ
    // =====================================================
    async runComparison() {
        const idA = this.dom.comparePeriodA?.value;
        const idB = this.dom.comparePeriodB?.value;
        if (!idA || !idB) return toast('Выберите оба периода', 'warning');
        if (idA === idB) return toast('Периоды должны быть разными', 'warning');

        setLoading(this.dom.btnCompare, true, 'Анализ…');
        this.dom.compareContainer.innerHTML = '<div style="padding:30px; text-align:center; color:var(--text-secondary);">Сравниваем данные…</div>';
        try {
            const data = await api.get(`/admin/periods/compare?period_a=${idA}&period_b=${idB}`);
            this.renderComparison(data);
        } catch (e) {
            this.dom.compareContainer.innerHTML = `<div style="padding:16px; color:var(--danger-color);">Ошибка: ${esc(e.message)}</div>`;
        } finally {
            setLoading(this.dom.btnCompare, false, 'Сравнить');
        }
    },

    renderComparison(data) {
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
                <span><strong>A:</strong> ${esc(data.period_a.name)}</span>
                <span style="color:var(--text-secondary);">→</span>
                <span><strong>B:</strong> ${esc(data.period_b.name)}</span>
                <span style="color:var(--text-secondary); margin-left:auto; font-size:12px;">Красный = рост, зелёный = экономия</span>
            </div>`;

        data.dormitories.forEach(dorm => {
            const tc = dorm.details.total_cost;
            const dColor = tc.delta > 0 ? '#ef4444' : tc.delta < 0 ? '#10b981' : '#6b7280';
            html += `
                <div style="margin-bottom:14px; border:1px solid var(--border-color); border-radius:8px; overflow:hidden;">
                    <div style="display:flex; justify-content:space-between; align-items:center; padding:10px 14px; background:var(--bg-page); border-bottom:1px solid var(--border-color);">
                        <strong style="font-size:14px;">🏢 ${esc(dorm.dormitory)}</strong>
                        <span style="color:${dColor}; font-weight:700; font-size:14px;">
                            ${tc.delta > 0 ? '+' : ''}${tc.delta.toFixed(2)} ₽ (${tc.delta > 0 ? '+' : ''}${tc.percent}%)
                        </span>
                    </div>
                    <table style="width:100%; border-collapse:collapse; font-size:13px;">
                        <thead>
                            <tr style="background:var(--bg-page); color:var(--text-secondary); font-size:11px; text-transform:uppercase;">
                                <th style="text-align:left; padding:6px 10px;">Ресурс</th>
                                <th style="text-align:right; padding:6px 10px;">${esc(data.period_a.name)}</th>
                                <th style="text-align:right; padding:6px 10px;">${esc(data.period_b.name)}</th>
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
                        ${esc(data.period_a.name)}: <strong>${gt.period_a.toFixed(2)} ₽</strong>
                        &nbsp;→&nbsp;
                        ${esc(data.period_b.name)}: <strong>${gt.period_b.toFixed(2)} ₽</strong>
                    </div>
                </div>
                <div style="text-align:right;">
                    <div style="font-size:24px; font-weight:700; color:${gtColor};">${gt.delta > 0 ? '+' : ''}${gt.delta.toFixed(2)} ₽</div>
                    <div style="font-size:14px; color:${gtColor};">${gt.delta > 0 ? '+' : ''}${gt.percent}%</div>
                </div>
            </div>`;
        this.dom.compareContainer.innerHTML = html;
    },

    // =====================================================
    // КВИТАНЦИИ И ЭКСПОРТ
    // =====================================================
    async downloadReceipt(id) {
        if (!id) return;
        toast('Подготовка PDF…', 'info');
        try {
            await api.download(`/admin/receipts/${id}/download`, `Kvitanciya_${id}.pdf`);
        } catch (e) {
            toast('Ошибка скачивания: ' + e.message, 'error');
        }
    },

    async downloadExcel() {
        if (!this.state.selectedPeriodId) return toast('Сначала выберите период', 'warning');
        setLoading(this.dom.btnExcel, true, 'Формирование…');
        try {
            const url = `/admin/export_report?period_id=${this.state.selectedPeriodId}`;
            await api.download(url, `Svodnaya_vedomost_${this.state.selectedPeriodId}.xlsx`);
        } catch (e) {
            toast('Ошибка Excel: ' + e.message, 'error');
        } finally {
            setLoading(this.dom.btnExcel, false);
        }
    },

    async downloadZip() {
        if (!this.state.selectedPeriodId) return toast('Сначала выберите период', 'warning');
        setLoading(this.dom.btnZip, true, 'Запуск задачи…');
        try {
            toast('Архив формируется на сервере. Это может занять до минуты…', 'info');
            const res = await api.post(`/admin/reports/bulk-zip?period_id=${this.state.selectedPeriodId}`);
            await this.pollTask(res.task_id, this.dom.btnZip);
        } catch (e) {
            toast('Ошибка запуска: ' + e.message, 'error');
            setLoading(this.dom.btnZip, false);
        }
    },

    async pollTask(taskId, button) {
        if (this.state.pollTimer) clearInterval(this.state.pollTimer);
        const originalText = button.textContent;
        setLoading(button, true, 'Обработка…');
        return new Promise((resolve, reject) => {
            let attempts = 0;
            const max = 150;
            this.state.pollTimer = setInterval(async () => {
                attempts++;
                if (attempts > max) {
                    clearInterval(this.state.pollTimer);
                    setLoading(button, false, originalText);
                    return reject(new Error('Время ожидания истекло.'));
                }
                try {
                    const data = await api.get(`/admin/tasks/${taskId}`);
                    if (data.status === 'done' || data.state === 'SUCCESS') {
                        clearInterval(this.state.pollTimer);
                        setLoading(button, false, originalText);
                        if (data.download_url) {
                            window.open(data.download_url, '_blank');
                            toast('Архив готов и скачивается!', 'success');
                            resolve(data);
                        } else if (data.result && data.result.status === 'error') {
                            reject(new Error(data.result.message || 'Ошибка сборки архива.'));
                        } else {
                            reject(new Error('Неожиданный ответ от сервера.'));
                        }
                    } else if (data.state === 'FAILURE') {
                        clearInterval(this.state.pollTimer);
                        setLoading(button, false, originalText);
                        reject(new Error(data.error || 'Ошибка задачи.'));
                    }
                } catch (e) {
                    if (e.name === 'AbortError') return;
                }
            }, 2000);
        });
    },
};
