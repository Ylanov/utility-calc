// static/js/modules/summary.js
//
// «Финансовая отчётность» (v2) — только денежная сводка по жильцам.
//
// Раньше этот модуль также рулил «Анализом периодов» (предпросмотр закрытия
// и сравнение двух периодов). Теперь это объединено в «Центр анализа» —
// всё в analyzer.js (таб «Анализ периода»). Сюда остались только KPI,
// фильтры, карточки общежитий, sparkline, выгрузка PDF/Excel/Zip.

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
        expandedDorms: new Set(),          // раскрытые карточки общежитий
        expandedResidents: new Set(),      // раскрытые жильцы (user_id)
        residentDetailCache: new Map(),    // user_id -> detail JSON (фетчится по клику)
        residentDetailLoading: new Set(),  // user_id -> загрузка идёт
        // Текущая загруженная сводка v2
        currentSummary: null,
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
        };
    },

    bindEvents() {
        this.dom.btnRefresh?.addEventListener('click', () => this.loadData());
        this.dom.btnExcel?.addEventListener('click', () => this.downloadExcel());
        this.dom.btnZip?.addEventListener('click', () => this.downloadZip());

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

            // Клик по строке жильца — разворачиваем деталь. PDF-кнопка идёт
            // ниже отдельным ветвлением, чтобы клик по ней не разворачивал строку.
            const pdf = e.target.closest('button[data-pdf-id]');
            if (pdf) {
                this.downloadReceipt(Number(pdf.dataset.pdfId));
                return;
            }

            const resRow = e.target.closest('[data-toggle-resident]');
            if (resRow) {
                const uid = Number(resRow.dataset.toggleResident);
                this.toggleResident(uid);
                return;
            }
        });
    },

    // =====================================================
    // ПЕРИОДЫ (только для селектора финансовой отчётности)
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
            this.loadData();
        } catch (e) {
            this.dom.periodSelector.textContent = 'Ошибка загрузки периодов.';
            console.error(e);
        }
    },

    // =====================================================
    // ФИНАНСОВАЯ ОТЧЁТНОСТЬ v2
    // =====================================================
    async loadData() {
        if (!this.state.selectedPeriodId) return;
        if (this.state.controller) this.state.controller.abort();
        this.state.controller = new AbortController();

        // Сброс развёрнутых жильцов — фильтр/период меняется, строки могут
        // уйти, а кеш деталей зависит от period_id. Проще сбросить чем синкать.
        this.state.expandedResidents.clear();
        this.state.residentDetailCache.clear();
        this.state.residentDetailLoading.clear();

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
        const isExpanded = r.user_id && this.state.expandedResidents.has(r.user_id);
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

        const rowBg = isMissing ? 'background:rgba(254,226,226,0.4);' : (isExpanded ? 'background:rgba(59,130,246,0.05);' : '');
        const expandIcon = r.user_id
            ? `<i class="fa-solid fa-chevron-${isExpanded ? 'down' : 'right'}" style="color:var(--text-tertiary); font-size:10px; margin-right:4px;"></i>`
            : '';
        const clickAttrs = r.user_id ? `data-toggle-resident="${r.user_id}" style="cursor:pointer;"` : '';

        // Основная строка + (если раскрыта) панель деталей под ней
        const mainRow = `
            <tr ${clickAttrs} style="border-bottom:1px solid var(--border-color); ${rowBg}">
                <td style="padding:8px 10px;">
                    <div style="font-weight:600;">${expandIcon}${esc(r.username)}</div>
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

        if (!isExpanded) return mainRow;

        const detail = this.state.residentDetailCache.get(r.user_id);
        const loading = this.state.residentDetailLoading.has(r.user_id);
        const detailHtml = loading || !detail
            ? `<div style="padding:16px; color:var(--text-secondary); text-align:center;">
                 <i class="fa-solid fa-spinner fa-spin"></i> Загрузка деталей…
               </div>`
            : this._renderResidentDetail(detail);

        return mainRow + `
            <tr class="resident-detail-row" style="background:#fafafa;">
                <td colspan="11" style="padding:0; border-bottom:1px solid var(--border-color);">
                    ${detailHtml}
                </td>
            </tr>`;
    },

    // =====================================================
    // РАЗВОРОТ ЖИЛЬЦА: запрос деталей + рендер панели
    // =====================================================
    async toggleResident(userId) {
        if (!userId) return;
        const st = this.state;
        if (st.expandedResidents.has(userId)) {
            st.expandedResidents.delete(userId);
            this.renderSummary();
            return;
        }
        st.expandedResidents.add(userId);
        // Если данные уже в кеше и период не менялся — просто рендерим.
        if (st.residentDetailCache.has(userId)) {
            this.renderSummary();
            return;
        }
        st.residentDetailLoading.add(userId);
        this.renderSummary();
        try {
            const qs = st.selectedPeriodId ? `?period_id=${st.selectedPeriodId}` : '';
            const data = await api.get(`/admin/residents/${userId}/finance-detail${qs}`);
            st.residentDetailCache.set(userId, data);
        } catch (e) {
            st.residentDetailCache.set(userId, { __error: String(e.message || e) });
        } finally {
            st.residentDetailLoading.delete(userId);
            this.renderSummary();
        }
    },

    _renderResidentDetail(data) {
        if (data.__error) {
            return `<div style="padding:16px; color:var(--danger-color);">
                Ошибка загрузки: ${esc(data.__error)}
            </div>`;
        }
        return `
            <div style="padding:16px 20px; display:grid; grid-template-columns: minmax(0,1.2fr) minmax(0,1fr); gap:20px;">
                <div style="min-width:0;">
                    ${this._renderMetersHistory(data)}
                </div>
                <div style="min-width:0;">
                    ${this._renderContractBlock(data.contract)}
                    ${this._renderCurrentCostBreakdown(data.current)}
                    ${this._renderAdjustmentsBlock(data.adjustments)}
                </div>
            </div>`;
    },

    _renderMetersHistory(data) {
        const hist = data.history || [];
        if (!hist.length) {
            return `<div style="color:var(--text-secondary); padding:10px 0;">Нет данных о показаниях.</div>`;
        }
        const SRC_LABEL = {
            gsheets: 'GSheets', app: 'Приложение', baseline: 'Baseline',
            manual: 'Вручную', one_time: 'Разовое', auto: 'Автогенер.',
            initial: 'Начальные', meter_op: 'Счётчик',
        };
        const fmtNum = v => v == null ? '—' : Number(v).toLocaleString('ru-RU', {maximumFractionDigits: 2});
        const fmtDelta = v => {
            if (v == null) return '<span style="color:var(--text-tertiary);">—</span>';
            const color = v > 0 ? '#059669' : v < 0 ? '#dc2626' : 'var(--text-tertiary)';
            const sign = v > 0 ? '+' : '';
            return `<span style="color:${color}; font-size:11px;">${sign}${fmtNum(v)}</span>`;
        };
        const rows = hist.map(h => {
            const srcLbl = h.source ? (SRC_LABEL[h.source] || h.source) : '—';
            const flagsShort = (h.flags || [])
                .filter(f => f && f !== 'PENDING')
                .slice(0, 2).join(', ');
            const statusHtml = h.reading_id
                ? (h.is_approved
                    ? '<span style="color:#059669; font-size:11px;">утв.</span>'
                    : '<span style="color:#f59e0b; font-size:11px;">черн.</span>')
                : '<span style="color:var(--text-tertiary); font-size:11px;">нет</span>';
            return `
                <tr style="border-bottom:1px solid #e5e7eb;">
                    <td style="padding:6px 8px; font-weight:600;">${esc(h.period_name || '—')}</td>
                    <td style="padding:6px 8px; text-align:right; font-family:monospace;">${fmtNum(h.hot_water)}</td>
                    <td style="padding:6px 8px; text-align:right;">${fmtDelta(h.delta_hot)}</td>
                    <td style="padding:6px 8px; text-align:right; font-family:monospace;">${fmtNum(h.cold_water)}</td>
                    <td style="padding:6px 8px; text-align:right;">${fmtDelta(h.delta_cold)}</td>
                    <td style="padding:6px 8px; text-align:right; font-family:monospace;">${fmtNum(h.electricity)}</td>
                    <td style="padding:6px 8px; text-align:right;">${fmtDelta(h.delta_elect)}</td>
                    <td style="padding:6px 8px; font-size:11px;">${esc(srcLbl)}</td>
                    <td style="padding:6px 8px; text-align:center;">${statusHtml}</td>
                    <td style="padding:6px 8px; font-size:10px; color:var(--text-secondary);">${esc(flagsShort)}</td>
                </tr>`;
        }).join('');

        return `
            <div style="font-size:12px; font-weight:600; color:var(--text-secondary); text-transform:uppercase; margin-bottom:6px;">
                <i class="fa-solid fa-gauge-high"></i> Показания за ${hist.length} ${hist.length === 1 ? 'период' : 'периода(ов)'}
            </div>
            <div style="background:var(--bg-card); border:1px solid var(--border-color); border-radius:8px; overflow-x:auto;">
                <table style="width:100%; border-collapse:collapse; font-size:12px; min-width:720px;">
                    <thead style="background:var(--bg-page); color:var(--text-secondary); text-transform:uppercase; font-size:10px;">
                        <tr>
                            <th style="text-align:left; padding:6px 8px;">Период</th>
                            <th style="text-align:right; padding:6px 8px;">ГВС</th>
                            <th style="text-align:right; padding:6px 8px;">Δ</th>
                            <th style="text-align:right; padding:6px 8px;">ХВС</th>
                            <th style="text-align:right; padding:6px 8px;">Δ</th>
                            <th style="text-align:right; padding:6px 8px;">Свет</th>
                            <th style="text-align:right; padding:6px 8px;">Δ</th>
                            <th style="text-align:left; padding:6px 8px;">Источник</th>
                            <th style="text-align:center; padding:6px 8px;">Статус</th>
                            <th style="text-align:left; padding:6px 8px;">Флаги</th>
                        </tr>
                    </thead>
                    <tbody>${rows}</tbody>
                </table>
            </div>`;
    },

    _renderContractBlock(c) {
        if (!c) {
            return `
                <div style="background:#fef2f2; border:1px solid #fecaca; color:#991b1b; border-radius:8px; padding:10px 12px; margin-bottom:12px; font-size:12px;">
                    <i class="fa-solid fa-triangle-exclamation"></i> Активный договор найма не оформлен.
                </div>`;
        }
        const parts = [`№ ${esc(c.number || '—')}`];
        if (c.signed_date) {
            parts.push(`от ${new Date(c.signed_date).toLocaleDateString('ru-RU')}`);
        }
        if (c.valid_until) {
            parts.push(`до ${new Date(c.valid_until).toLocaleDateString('ru-RU')}`);
        }
        return `
            <div style="background:#f0fdf4; border:1px solid #bbf7d0; border-radius:8px; padding:10px 12px; margin-bottom:12px; font-size:12px; display:flex; align-items:center; gap:8px;">
                <i class="fa-solid fa-file-signature" style="color:#10b981;"></i>
                <span style="font-weight:600; color:#065f46;">Договор:</span>
                <span>${parts.join(' · ')}</span>
                ${c.has_file ? '<span style="margin-left:auto; font-size:10px; color:#059669;"><i class="fa-solid fa-paperclip"></i> файл</span>' : ''}
            </div>`;
    },

    _renderCurrentCostBreakdown(current) {
        if (!current) {
            return `
                <div style="font-size:12px; color:var(--text-secondary); padding:10px 0; border-top:1px solid var(--border-color);">
                    Квитанции за этот период нет.
                </div>`;
        }
        const costs = current.costs || {};
        const rows = [
            ['Плата за жильё',     costs.cost_maintenance],
            ['Отопление',          costs.cost_fixed_part],
            ['Подогрев воды',      costs.cost_hot_water],
            ['Холодная вода',      costs.cost_cold_water],
            ['Водоотведение',      costs.cost_sewage],
            ['Электроэнергия',     costs.cost_electricity],
            ['Социальный наём',    costs.cost_social_rent],
            ['ТКО',                costs.cost_waste],
        ];
        const rowsHtml = rows.map(([label, val]) => {
            const v = Number(val || 0);
            if (v === 0) return '';
            return `
                <div style="display:flex; justify-content:space-between; padding:4px 0; font-size:12px; border-bottom:1px dashed #e5e7eb;">
                    <span>${esc(label)}</span>
                    <span style="font-family:monospace;">${v.toLocaleString('ru-RU', {minimumFractionDigits: 2, maximumFractionDigits: 2})}</span>
                </div>`;
        }).filter(Boolean).join('');
        const totalRow = `
            <div style="display:flex; justify-content:space-between; padding:8px 0 4px; font-size:13px; font-weight:700; border-top:2px solid var(--border-color); margin-top:4px;">
                <span>Итого</span>
                <span style="color:#059669; font-family:monospace;">${fmtMoney(current.total_cost)}</span>
            </div>`;

        const debtInfo = [];
        if (current.debt_209 > 0) debtInfo.push(`долг 209: ${fmtMoney(current.debt_209)}`);
        if (current.debt_205 > 0) debtInfo.push(`долг 205: ${fmtMoney(current.debt_205)}`);
        if (current.overpayment_209 > 0) debtInfo.push(`переплата 209: ${fmtMoney(current.overpayment_209)}`);
        if (current.overpayment_205 > 0) debtInfo.push(`переплата 205: ${fmtMoney(current.overpayment_205)}`);
        const debtLine = debtInfo.length
            ? `<div style="font-size:11px; color:var(--text-secondary); margin-top:6px;">${esc(debtInfo.join(' · '))}</div>`
            : '';

        return `
            <div style="font-size:12px; font-weight:600; color:var(--text-secondary); text-transform:uppercase; margin-bottom:6px;">
                <i class="fa-solid fa-file-invoice-dollar"></i> Детализация квитанции
            </div>
            <div style="background:var(--bg-card); border:1px solid var(--border-color); border-radius:8px; padding:12px; margin-bottom:12px;">
                ${rowsHtml || '<div style="font-size:12px; color:var(--text-tertiary);">Все статьи = 0 (baseline)</div>'}
                ${totalRow}
                ${debtLine}
            </div>`;
    },

    _renderAdjustmentsBlock(adjs) {
        if (!adjs || !adjs.length) return '';
        const items = adjs.map(a => {
            const d = a.created_at ? new Date(a.created_at).toLocaleDateString('ru-RU') : '';
            const color = Number(a.amount) > 0 ? '#dc2626' : '#059669';
            const sign = Number(a.amount) > 0 ? '+' : '';
            return `
                <div style="display:flex; gap:10px; padding:6px 0; border-bottom:1px dashed #e5e7eb; font-size:12px;">
                    <div style="flex-shrink:0; width:90px; color:var(--text-secondary);">${esc(a.period_name || '')}</div>
                    <div style="flex:1; min-width:0;">
                        <div>${esc(a.description || '(без пояснения)')}</div>
                        <div style="font-size:10px; color:var(--text-tertiary);">счёт ${esc(a.account_type || '')} · ${esc(d)}</div>
                    </div>
                    <div style="color:${color}; font-weight:700; font-family:monospace; white-space:nowrap;">
                        ${sign}${fmtMoney(a.amount)}
                    </div>
                </div>`;
        }).join('');
        return `
            <div style="font-size:12px; font-weight:600; color:var(--text-secondary); text-transform:uppercase; margin-bottom:6px;">
                <i class="fa-solid fa-sliders"></i> Корректировки (${adjs.length})
            </div>
            <div style="background:var(--bg-card); border:1px solid var(--border-color); border-radius:8px; padding:10px 12px;">
                ${items}
            </div>`;
    },

    // Блоки «Предпросмотр закрытия периода» и «Сравнение периодов» перенесены
    // в analyzer.js (таб «Анализ периода» в Центре анализа). Эндпоинты те же:
    // /admin/periods/close-preview и /admin/periods/compare — просто дёргает
    // их теперь AnalyzerModule.

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
