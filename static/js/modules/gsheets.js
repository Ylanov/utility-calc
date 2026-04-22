// static/js/modules/gsheets.js
//
// Импорт показаний из Google Sheets с fuzzy-match и утверждением.
//
// Ключевые UX-решения:
// 1. По умолчанию в таблице только АКТИВНЫЕ строки (pending / conflict / unmatched).
//    Утверждённые и отклонённые уходят в "архив" (переключатель сверху).
// 2. После approve/reject строка МОМЕНТАЛЬНО пропадает из списка —
//    не ждём refresh с сервера. Счётчики пересчитываются локально.
// 3. Auto-refresh каждые 30 секунд, но только пока вкладка открыта
//    и не скрыта через Page Visibility API (не тратим трафик впустую).
// 4. Клик на имя жильца открывает модалку с полной историей его подач
//    (GSheets + утверждённые MeterReading за последний год).

import { api } from '../core/api.js';
import { toast } from '../core/dom.js';

const STATUS_META = {
    pending:        { label: 'В ожидании',   color: '#3b82f6', bg: '#dbeafe' },
    conflict:       { label: 'Конфликт',     color: '#f59e0b', bg: '#fef3c7' },
    unmatched:      { label: 'Не найден',    color: '#ef4444', bg: '#fee2e2' },
    auto_approved:  { label: 'Авто-утв.',    color: '#8b5cf6', bg: '#ede9fe' },
    approved:       { label: 'Утверждено',   color: '#10b981', bg: '#d1fae5' },
    rejected:       { label: 'Отклонено',    color: '#6b7280', bg: '#f3f4f6' },
};

const AUTO_REFRESH_INTERVAL_MS = 30_000;

function fmtDateTime(iso) {
    if (!iso) return '—';
    try {
        const d = new Date(iso);
        if (isNaN(d.getTime())) return iso;
        return d.toLocaleString('ru-RU', {
            day: '2-digit', month: '2-digit', year: 'numeric',
            hour: '2-digit', minute: '2-digit',
        });
    } catch { return iso; }
}

function fmtNum(v) {
    if (v === null || v === undefined || v === '') return '—';
    const n = Number(v);
    if (isNaN(n)) return String(v);
    return n.toFixed(3).replace(/\.?0+$/, '');
}

function escapeHtml(s) {
    if (s === null || s === undefined) return '';
    return String(s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

export const GSheetsModule = {
    isInitialized: false,

    state: {
        page: 1,
        limit: 50,
        total: 0,
        status: '',       // явно выбранный статус (через select) — приоритет над activeOnly
        activeOnly: true, // по умолчанию скрываем утверждённые/отклонённые
        search: '',
        rows: [],
        stats: null,
        viewMode: 'grouped', // 'grouped' (по жильцам) или 'flat' (плоско)
        // В grouped-режиме поднимаем лимит — в одном жильце может быть много подач,
        // 50 строк = ~10-15 жильцов. 200 строк = почти все активные за раз.
        groupedLimit: 200,
        expandedUserIds: new Set(),  // какие карточки жильцов раскрыты
    },

    _intervalId: null,
    _visibilityHandler: null,
    _isFetching: false,

    init() {
        if (this.isInitialized) {
            this.refresh();
            this._startAutoRefresh();
            return;
        }
        this.cacheDOM();
        if (!this.dom.root) return;
        this.bindEvents();
        this.isInitialized = true;
        this.refresh();
        this._startAutoRefresh();
    },

    cacheDOM() {
        this.dom = {
            root: document.getElementById('gsheetsTableBody'),
            stats: document.getElementById('gsheetsStats'),
            lastImport: document.getElementById('gsheetsLastImport'),
            statusFilter: document.getElementById('gsheetsStatusFilter'),
            search: document.getElementById('gsheetsSearch'),
            selectAll: document.getElementById('gsheetsSelectAll'),
            btnSync: document.getElementById('btnGsheetsSync'),
            btnRefresh: document.getElementById('btnGsheetsRefresh'),
            btnBulkApprove: document.getElementById('btnBulkApprove'),
            tbody: document.getElementById('gsheetsTableBody'),
            prev: document.getElementById('gsheetsPrevPage'),
            next: document.getElementById('gsheetsNextPage'),
            pageLabel: document.getElementById('gsheetsPageLabel'),
            totalInfo: document.getElementById('gsheetsTotalInfo'),
            archiveToggle: document.getElementById('gsheetsArchiveToggle'),
            flatView: document.getElementById('gsheetsFlatView'),
            groupedView: document.getElementById('gsheetsGroupedView'),
            groupedBody: document.getElementById('gsheetsGroupedBody'),
            btnViewGrouped: document.getElementById('gsheetsViewGrouped'),
            btnViewFlat: document.getElementById('gsheetsViewFlat'),
        };
    },

    bindEvents() {
        this.dom.btnSync?.addEventListener('click', () => this.triggerSync());
        this.dom.btnRefresh?.addEventListener('click', () => this.refresh());
        this.dom.btnBulkApprove?.addEventListener('click', () => this.bulkApprove());

        this.dom.btnViewGrouped?.addEventListener('click', () => this._setViewMode('grouped'));
        this.dom.btnViewFlat?.addEventListener('click', () => this._setViewMode('flat'));
        this._applyViewModeUI();  // выставляем начальное состояние кнопок и контейнеров

        this.dom.statusFilter?.addEventListener('change', () => {
            this.state.status = this.dom.statusFilter.value;
            this.state.page = 1;
            this.loadRows();
        });

        this.dom.archiveToggle?.addEventListener('change', (e) => {
            this.state.activeOnly = !e.target.checked;
            this.state.page = 1;
            this.loadRows();
        });

        let searchTimer = null;
        this.dom.search?.addEventListener('input', () => {
            clearTimeout(searchTimer);
            searchTimer = setTimeout(() => {
                this.state.search = this.dom.search.value.trim();
                this.state.page = 1;
                this.loadRows();
            }, 400);
        });

        this.dom.prev?.addEventListener('click', () => {
            if (this.state.page > 1) {
                this.state.page -= 1;
                this.loadRows();
            }
        });
        this.dom.next?.addEventListener('click', () => {
            const max = Math.ceil(this.state.total / this.state.limit) || 1;
            if (this.state.page < max) {
                this.state.page += 1;
                this.loadRows();
            }
        });

        this.dom.selectAll?.addEventListener('change', (e) => {
            this.dom.tbody
                .querySelectorAll('input.row-check')
                .forEach((cb) => { cb.checked = e.target.checked; });
        });

        // Делегирование кликов по кнопкам действий + клик на имя жильца
        this.dom.tbody?.addEventListener('click', (e) => {
            const btn = e.target.closest('button[data-action]');
            if (btn) {
                const rowId = Number(btn.dataset.rowId);
                const action = btn.dataset.action;
                if (action === 'approve') this.approveRow(rowId);
                else if (action === 'reject') this.rejectRow(rowId);
                else if (action === 'reassign') this.reassignPrompt(rowId);
                else if (action === 'delete') this.deleteRow(rowId);
                return;
            }
            const userLink = e.target.closest('a[data-user-id]');
            if (userLink) {
                e.preventDefault();
                this.showUserHistory(Number(userLink.dataset.userId));
            }
        });

        // Те же обработчики для grouped-вида + раскрытие/сворачивание карточки.
        // ВАЖНО: ищем data-action и на button, и на <a> — FIO в заголовке группы
        // это ссылка (<a data-action="group-show-history">), и если её не
        // перехватить, браузер прыгал на href="#" → hashchange → SPA-роутер
        // уходил на defaultTab = "dashboard". Админа резко кидало на дашборд.
        this.dom.groupedBody?.addEventListener('click', (e) => {
            const el = e.target.closest('button[data-action], a[data-action]');
            if (el) {
                e.preventDefault();
                e.stopPropagation();
                const action = el.dataset.action;
                if (action === 'group-approve-all') {
                    this.groupApproveAll(el.dataset.userKey);
                    return;
                }
                if (action === 'group-show-history') {
                    this.showUserHistory(Number(el.dataset.userId));
                    return;
                }
                if (action === 'group-find-relative') {
                    // То же что reassign, но из шапки unmatched-группы.
                    this.reassignPrompt(Number(el.dataset.rowId));
                    return;
                }
                const rowId = Number(el.dataset.rowId);
                if (action === 'approve') this.approveRow(rowId);
                else if (action === 'reject') this.rejectRow(rowId);
                else if (action === 'reassign') this.reassignPrompt(rowId);
                else if (action === 'delete') this.deleteRow(rowId);
                return;
            }
            const header = e.target.closest('[data-toggle-user]');
            if (header) {
                this._toggleUserGroup(header.dataset.toggleUser);
            }
        });

        // Auto-refresh приостанавливается когда вкладка браузера скрыта —
        // экономим запросы и серверные ресурсы на 10к пользователей.
        this._visibilityHandler = () => {
            if (document.hidden) {
                this._stopAutoRefresh();
            } else {
                this.refresh();
                this._startAutoRefresh();
            }
        };
        document.addEventListener('visibilitychange', this._visibilityHandler);
    },

    _startAutoRefresh() {
        this._stopAutoRefresh();
        if (document.hidden) return;
        this._intervalId = setInterval(() => {
            // Обновляем тихо (без лоадера), если пользователь не скроллит таблицу.
            if (!document.hidden && !this._isFetching) {
                this.refresh({ silent: true });
            }
        }, AUTO_REFRESH_INTERVAL_MS);
    },

    _stopAutoRefresh() {
        if (this._intervalId) {
            clearInterval(this._intervalId);
            this._intervalId = null;
        }
    },

    async refresh(options = {}) {
        if (this._isFetching) return;
        this._isFetching = true;
        try {
            await Promise.all([this.loadStats(), this.loadRows(options)]);
        } finally {
            this._isFetching = false;
        }
    },

    async loadStats() {
        try {
            const data = await api.get('/admin/gsheets/stats');
            this.state.stats = data;
            this.renderStats(data);
        } catch (e) {
            this.dom.stats.innerHTML =
                `<div style="color:var(--danger-color); padding:10px;">Не удалось получить статистику: ${escapeHtml(e.message)}</div>`;
        }
    },

    renderStats(data) {
        const by = data.by_status || {};
        const cards = [
            { label: 'Всего',        value: data.total || 0,          color: '#1f2937' },
            { label: 'В ожидании',   value: by.pending || 0,          color: STATUS_META.pending.color,      highlight: by.pending > 0 },
            { label: 'Конфликты',    value: by.conflict || 0,         color: STATUS_META.conflict.color,     highlight: by.conflict > 0 },
            { label: 'Не найдены',   value: by.unmatched || 0,        color: STATUS_META.unmatched.color,    highlight: by.unmatched > 0 },
            { label: 'Авто-утв.',    value: by.auto_approved || 0,    color: STATUS_META.auto_approved.color },
            { label: 'Утверждено',   value: by.approved || 0,         color: STATUS_META.approved.color },
        ];

        this.dom.stats.innerHTML = cards.map(c => `
            <div style="background:var(--bg-card); border:1px solid ${c.highlight ? c.color : 'var(--border-color)'}; border-radius:var(--radius-sm); padding:12px; ${c.highlight ? 'box-shadow:0 0 0 2px ' + c.color + '22;' : ''}">
                <div style="color:var(--text-secondary); font-size:11px; text-transform:uppercase; letter-spacing:.5px;">${escapeHtml(c.label)}</div>
                <div style="font-size:24px; font-weight:700; color:${c.color}; margin-top:4px;">${c.value}</div>
            </div>
        `).join('');

        const last = data.last_import_at ? fmtDateTime(data.last_import_at) : '—';
        const lastSheet = data.last_sheet_timestamp ? fmtDateTime(data.last_sheet_timestamp) : '—';
        const cfg = data.sheet_id_configured
            ? `<span style="color:#10b981;"><i class="fa-solid fa-circle-check"></i> таблица настроена</span>`
            : `<span style="color:#ef4444;"><i class="fa-solid fa-triangle-exclamation"></i> GSHEETS_SHEET_ID не задан в .env</span>`;
        const autoRefresh = `<span style="color:#10b981;"><i class="fa-solid fa-bolt"></i> авто-обновление каждые ${Math.round(AUTO_REFRESH_INTERVAL_MS/1000)}с</span>`;

        this.dom.lastImport.innerHTML = `
            <div><b>Последний импорт:</b> ${last} &nbsp;·&nbsp;
            <b>Самое свежее показание в таблице:</b> ${lastSheet}</div>
            <div style="margin-top:4px; font-size:12px;">${cfg}
            &nbsp;·&nbsp; Авто-синхронизация с Google: каждые ${data.auto_sync_interval_min || 0} мин
            &nbsp;·&nbsp; ${autoRefresh}</div>
        `;
    },

    async loadRows(options = {}) {
        // В grouped-режиме поднимаем лимит, чтобы все подачи одного жильца
        // оказались на одной странице (иначе "цепочка" разорвётся между страницами).
        const limit = this.state.viewMode === 'grouped'
            ? this.state.groupedLimit
            : this.state.limit;
        const params = new URLSearchParams({
            page: this.state.page,
            limit: limit,
            active_only: this.state.activeOnly ? 'true' : 'false',
        });
        if (this.state.status) params.set('status', this.state.status);
        if (this.state.search) params.set('search', this.state.search);

        try {
            const data = await api.get(`/admin/gsheets/rows?${params.toString()}`);
            this.state.total = data.total;
            this.state.rows = data.items || [];
            this.renderRows();
            this.renderPagination();
        } catch (e) {
            if (!options.silent) {
                const errHtml = `<div style="padding:20px; text-align:center; color:var(--danger-color);">${escapeHtml(e.message)}</div>`;
                this.dom.tbody.innerHTML =
                    `<tr><td colspan="9">${errHtml}</td></tr>`;
                if (this.dom.groupedBody) this.dom.groupedBody.innerHTML = errHtml;
            }
        }
    },

    renderRows() {
        if (this.state.viewMode === 'grouped') {
            this._renderGrouped();
        } else {
            this._renderFlat();
        }
    },

    _renderFlat() {
        if (!this.state.rows.length) {
            const emptyText = this.state.activeOnly
                ? '🎉 Все актуальные строки обработаны! Переключите «показать архив» чтобы увидеть утверждённые/отклонённые.'
                : 'Нет строк' + (this.state.status ? ' с этим статусом' : '');
            this.dom.tbody.innerHTML =
                `<tr><td colspan="9" style="padding:30px; text-align:center; color:var(--text-secondary);">${escapeHtml(emptyText)}</td></tr>`;
            return;
        }
        this.dom.tbody.innerHTML = this.state.rows.map(row => this._renderRow(row)).join('');
    },

    _renderRow(row) {
        const meta = STATUS_META[row.status] || { label: row.status, color: '#6b7280', bg: '#f3f4f6' };
        const statusCell = `
            <span style="display:inline-block; padding:2px 8px; border-radius:12px; background:${meta.bg}; color:${meta.color}; font-size:11px; font-weight:600;">
                ${escapeHtml(meta.label)}
            </span>
            ${row.match_score ? `<div style="color:var(--text-secondary); font-size:10px; margin-top:2px;">${row.match_score}%</div>` : ''}
        `;

        const matchedCell = row.matched_user_id
            ? `<a href="#" data-user-id="${row.matched_user_id}" style="font-weight:600; color:var(--primary-color); text-decoration:none;" title="Показать историю">${escapeHtml(row.matched_username || '')}</a>
               <div style="color:var(--text-secondary); font-size:11px;">${escapeHtml(row.matched_room || 'без комнаты')}</div>
               ${row.conflict_reason ? `<div style="color:${STATUS_META.conflict.color}; font-size:11px; margin-top:2px;"><i class="fa-solid fa-triangle-exclamation"></i> ${escapeHtml(row.conflict_reason)}</div>` : ''}`
            : `<span style="color:var(--text-secondary); font-style:italic;">—</span>`;

        const actions = this._renderActions(row);

        // Цветовой индикатор низкого score (85%)
        const scoreWarning = row.match_score && row.match_score < 85 && row.match_score > 0
            ? 'background: linear-gradient(to right, rgba(245,158,11,0.05) 0%, transparent 100%);'
            : '';

        return `
            <tr data-row-id="${row.id}" style="${scoreWarning}">
                <td><input type="checkbox" class="row-check" data-row-id="${row.id}" ${row.matched_user_id ? '' : 'disabled'}></td>
                <td style="font-size:12px;">${escapeHtml(fmtDateTime(row.sheet_timestamp))}</td>
                <td>
                    <div style="font-weight:500;">${escapeHtml(row.raw_fio)}</div>
                    <div style="color:var(--text-secondary); font-size:11px;">${escapeHtml(row.raw_dormitory || '')}</div>
                </td>
                <td>${escapeHtml(row.raw_room_number || '—')}</td>
                <td style="text-align:right; font-family:monospace;">${fmtNum(row.hot_water)}</td>
                <td style="text-align:right; font-family:monospace;">${fmtNum(row.cold_water)}</td>
                <td>${matchedCell}</td>
                <td>${statusCell}</td>
                <td style="text-align:right;">${actions}</td>
            </tr>
        `;
    },

    _renderActions(row) {
        const buttons = [];
        const disabledApproved = (row.status === 'approved' || row.status === 'auto_approved');

        if (!disabledApproved && row.status !== 'rejected') {
            if (row.matched_user_id) {
                buttons.push(`<button class="action-btn success-btn" data-action="approve" data-row-id="${row.id}" style="padding:3px 8px; font-size:11px;" title="Утвердить"><i class="fa-solid fa-check"></i></button>`);
            }
            buttons.push(`<button class="action-btn secondary-btn" data-action="reassign" data-row-id="${row.id}" style="padding:3px 8px; font-size:11px;" title="Переназначить жильца"><i class="fa-solid fa-user-pen"></i></button>`);
            buttons.push(`<button class="action-btn danger-btn" data-action="reject" data-row-id="${row.id}" style="padding:3px 8px; font-size:11px;" title="Отклонить"><i class="fa-solid fa-xmark"></i></button>`);
        } else {
            buttons.push(`<span style="color:var(--text-secondary); font-size:11px;">${disabledApproved ? '✓ в системе' : 'отклонено'}</span>`);
            if (row.status === 'rejected') {
                buttons.push(`<button class="icon-btn" data-action="delete" data-row-id="${row.id}" style="padding:3px 6px;" title="Удалить из буфера"><i class="fa-solid fa-trash"></i></button>`);
            }
        }
        return buttons.join(' ');
    },

    renderPagination() {
        const limit = this.state.viewMode === 'grouped'
            ? this.state.groupedLimit
            : this.state.limit;
        const max = Math.ceil(this.state.total / limit) || 1;
        this.dom.pageLabel.textContent = `${this.state.page} / ${max}`;
        this.dom.totalInfo.textContent = `Всего: ${this.state.total}`;
        this.dom.prev.disabled = this.state.page <= 1;
        this.dom.next.disabled = this.state.page >= max;
    },

    // ========================================================
    // VIEW MODE — переключение «по жильцам» ↔ «плоско»
    // ========================================================
    _setViewMode(mode) {
        if (this.state.viewMode === mode) return;
        this.state.viewMode = mode;
        this.state.page = 1;
        this._applyViewModeUI();
        this.loadRows();
    },

    _applyViewModeUI() {
        const grouped = this.state.viewMode === 'grouped';
        if (this.dom.flatView)    this.dom.flatView.style.display    = grouped ? 'none' : '';
        if (this.dom.groupedView) this.dom.groupedView.style.display = grouped ? '' : 'none';

        // Подсветка активной кнопки переключателя.
        const setActive = (btn, active) => {
            if (!btn) return;
            btn.classList.toggle('primary-btn', active);
            btn.classList.toggle('secondary-btn', !active);
        };
        setActive(this.dom.btnViewGrouped, grouped);
        setActive(this.dom.btnViewFlat, !grouped);
    },

    // ========================================================
    // GROUPED VIEW — карточки по жильцам с цепочкой подач
    // ========================================================

    /**
     * Группирует строки по жильцу. Ключ:
     *  - matched_user_id если есть (надёжный матч),
     *  - "unmatched:<нормализованное ФИО>" — собирает в одну группу строки
     *    с одинаковым ФИО, которые fuzzy не сопоставил (часто это один и тот же
     *    человек, у которого ФИО написано иначе чем в БД).
     */
    _groupRowsByUser() {
        const groups = new Map();
        for (const row of this.state.rows) {
            const key = row.matched_user_id
                ? `u:${row.matched_user_id}`
                : `n:${(row.raw_fio || '').trim().toLowerCase().replace(/\s+/g, ' ')}`;
            if (!groups.has(key)) {
                groups.set(key, {
                    key,
                    userId: row.matched_user_id || null,
                    username: row.matched_username || null,
                    matchedRoom: row.matched_room || null,
                    fio: row.raw_fio,
                    dormitory: row.raw_dormitory || '',
                    roomNumber: row.raw_room_number || '',
                    rows: [],
                });
            }
            groups.get(key).rows.push(row);
        }

        // Сортируем подачи внутри каждой группы по дате (старые → новые),
        // чтобы было видно как растут счётчики во времени.
        for (const g of groups.values()) {
            g.rows.sort((a, b) => {
                const ta = a.sheet_timestamp ? Date.parse(a.sheet_timestamp) : 0;
                const tb = b.sheet_timestamp ? Date.parse(b.sheet_timestamp) : 0;
                return ta - tb;
            });
            g.pendingCount = g.rows.filter(
                r => r.status === 'pending' || r.status === 'conflict'
            ).length;
            g.lastSubmission = g.rows.length
                ? g.rows[g.rows.length - 1].sheet_timestamp
                : null;
        }

        // Сортируем группы: сверху те, у кого больше необработанных подач.
        return Array.from(groups.values()).sort((a, b) => {
            if (b.pendingCount !== a.pendingCount) return b.pendingCount - a.pendingCount;
            const ta = a.lastSubmission ? Date.parse(a.lastSubmission) : 0;
            const tb = b.lastSubmission ? Date.parse(b.lastSubmission) : 0;
            return tb - ta;
        });
    },

    _renderGrouped() {
        if (!this.state.rows.length) {
            const emptyText = this.state.activeOnly
                ? '🎉 Все актуальные подачи обработаны!'
                : 'Нет подач' + (this.state.status ? ' с этим статусом' : '');
            this.dom.groupedBody.innerHTML =
                `<div style="padding:30px; text-align:center; color:var(--text-secondary);">${escapeHtml(emptyText)}</div>`;
            return;
        }
        const groups = this._groupRowsByUser();
        this.dom.groupedBody.innerHTML = groups.map(g => this._renderUserGroup(g)).join('');
    },

    _renderUserGroup(g) {
        // Все группы по умолчанию свёрнуты — при 300+ жильцах с 29 подачами
        // каждый раскрытый шеврон даёт мегапортянку в браузере.
        // Раскрыть — клик по заголовку карточки (запоминается в expandedUserIds).
        const isOpen = this.state.expandedUserIds.has(g.key);

        const headerColor = g.userId
            ? 'var(--primary-color)'
            : 'var(--danger-color)';
        const userBadge = g.userId
            ? `<a href="#" data-user-id="${g.userId}" data-action="group-show-history"
                  style="color:var(--primary-color); text-decoration:none; font-weight:600;"
                  title="Подробная история жильца">
                  ${escapeHtml(g.username || g.fio)}
              </a>`
            : `<span style="color:var(--danger-color); font-weight:600;">
                  <i class="fa-solid fa-user-xmark"></i> ${escapeHtml(g.fio)} (не сопоставлен)
              </span>`;

        const roomLabel = g.matchedRoom || (g.roomNumber ? `комн. ${g.roomNumber}` : '—');
        const pendingBadge = g.pendingCount > 0
            ? `<span style="background:#fef3c7; color:#92400e; padding:2px 8px; border-radius:10px; font-size:11px; font-weight:600;">${g.pendingCount} ждут</span>`
            : `<span style="background:#d1fae5; color:#065f46; padding:2px 8px; border-radius:10px; font-size:11px; font-weight:600;">все обработаны</span>`;

        // Для unmatched-группы (нет привязки к жильцу) показываем заметную
        // кнопку «Кто это?» — открывает reassign-модалку с подсказками
        // родственников. Для matched-группы — обычная «Утв. все».
        let bulkBtn = '';
        if (!g.userId && g.rows.length) {
            // Берём id первой строки группы — модалка покажет кандидатов
            // именно для этой подачи. После подтверждения alias автоматически
            // подцепится для всех остальных подач этого ФИО.
            const firstRowId = g.rows[0].id;
            bulkBtn = `<button class="action-btn warning-btn" data-action="group-find-relative" data-row-id="${firstRowId}"
                               style="padding:4px 10px; font-size:12px;" title="Анализатор покажет возможных родственников">
                          <i class="fa-solid fa-user-magnifying-glass"></i> Кто это?
                      </button>`;
        } else if (g.pendingCount > 0 && g.userId) {
            bulkBtn = `<button class="action-btn success-btn" data-action="group-approve-all" data-user-key="${escapeHtml(g.key)}"
                               style="padding:4px 10px; font-size:12px;" title="Утвердить все pending этого жильца">
                          <i class="fa-solid fa-check-double"></i> Утв. все (${g.pendingCount})
                      </button>`;
        }

        const chain = isOpen ? this._renderUserChain(g) : '';

        return `
        <div class="gsheets-group-card" style="border:1px solid ${g.pendingCount > 0 ? '#fde68a' : 'var(--border-color)'}; border-radius:10px; background:var(--bg-card); overflow:hidden;">
            <div data-toggle-user="${escapeHtml(g.key)}"
                 style="display:flex; align-items:center; gap:12px; padding:12px 16px; cursor:pointer; background:${g.pendingCount > 0 ? 'rgba(254,243,199,0.3)' : 'transparent'};">
                <i class="fa-solid fa-chevron-${isOpen ? 'down' : 'right'}" style="color:var(--text-secondary); width:14px;"></i>
                <div style="width:36px; height:36px; border-radius:50%; background:${headerColor}22; color:${headerColor}; display:flex; align-items:center; justify-content:center; font-weight:600; font-size:14px; flex-shrink:0;">
                    ${escapeHtml((g.username || g.fio || '?').slice(0, 1).toUpperCase())}
                </div>
                <div style="flex:1; min-width:0;">
                    <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
                        ${userBadge}
                        ${pendingBadge}
                    </div>
                    <div style="color:var(--text-secondary); font-size:12px; margin-top:2px;">
                        ${escapeHtml(roomLabel)} · ${g.rows.length} ${g.rows.length === 1 ? 'подача' : 'подач'} · последняя: ${escapeHtml(fmtDateTime(g.lastSubmission))}
                    </div>
                </div>
                <div style="flex-shrink:0;">${bulkBtn}</div>
            </div>
            ${chain}
        </div>`;
    },

    /**
     * Цепочка подач: таблица с дельтами относительно предыдущей подачи
     * (видно как растёт счётчик во времени).
     *
     * ВАЖНО: рендерим в хронологическом порядке (старые → новые), чтобы
     * корректно считать дельты «от предыдущей подачи». После этого
     * переворачиваем массив строк на показ — админ видит свежие сверху,
     * старые снизу. Дельты остаются валидными (прирост от прошлой подачи).
     */
    _renderUserChain(g) {
        // Дельты — насколько счётчик вырос относительно предыдущей подачи.
        const fmtDelta = (cur, prv) => {
            if (cur == null || prv == null) return '';
            const d = Number(cur) - Number(prv);
            if (isNaN(d)) return '';
            if (d === 0) return `<span style="color:#9ca3af; font-size:10px;"> · 0</span>`;
            if (d < 0)  return `<span style="color:#dc2626; font-size:10px; font-weight:600;" title="Счётчик уменьшился — возможна ошибка"> · ↓${fmtNum(Math.abs(d))}</span>`;
            return `<span style="color:#16a34a; font-size:10px;"> · +${fmtNum(d)}</span>`;
        };

        // g.rows уже отсортирован хронологически (старые → новые) в _groupRowsByUser.
        // Строим HTML в таком порядке (для правильных дельт), но отображаем
        // в обратном — чтобы свежие подачи были сверху.
        const htmlByChronoIdx = g.rows.map((r, idx) => {
            const prev = idx > 0 ? g.rows[idx - 1] : null;
            const meta = STATUS_META[r.status] || { label: r.status, color: '#6b7280', bg: '#f3f4f6' };
            const actions = this._renderActions(r);
            const conflictNote = r.conflict_reason
                ? `<div style="color:${STATUS_META.conflict.color}; font-size:11px; margin-top:2px;"><i class="fa-solid fa-triangle-exclamation"></i> ${escapeHtml(r.conflict_reason)}</div>`
                : '';
            const roomCell = r.raw_room_number
                ? `<span style="font-size:11px; color:var(--text-secondary);">комн. ${escapeHtml(r.raw_room_number)}</span>`
                : '';

            return `
            <tr data-row-id="${r.id}">
                <td style="padding:8px 10px; font-size:12px; white-space:nowrap;">
                    <div>${escapeHtml(fmtDateTime(r.sheet_timestamp))}</div>
                    ${roomCell}
                </td>
                <td style="padding:8px 10px; text-align:right; font-family:monospace;">
                    <span style="color:#dc2626;">🔥</span> ${fmtNum(r.hot_water)}${fmtDelta(r.hot_water, prev?.hot_water)}
                </td>
                <td style="padding:8px 10px; text-align:right; font-family:monospace;">
                    <span style="color:#2563eb;">💧</span> ${fmtNum(r.cold_water)}${fmtDelta(r.cold_water, prev?.cold_water)}
                </td>
                <td style="padding:8px 10px;">
                    <span style="display:inline-block; padding:2px 8px; border-radius:10px; background:${meta.bg}; color:${meta.color}; font-size:11px; font-weight:600;">${escapeHtml(meta.label)}</span>
                    ${conflictNote}
                </td>
                <td style="padding:8px 10px; text-align:right; white-space:nowrap;">${actions}</td>
            </tr>`;
        });

        // Переворачиваем для отображения — свежие сверху.
        const rowsHtml = htmlByChronoIdx.slice().reverse().join('');

        return `
        <div style="border-top:1px solid var(--border-color); background:var(--bg-page);">
            <table style="width:100%; border-collapse:collapse;">
                <thead>
                    <tr style="background:var(--bg-page); color:var(--text-secondary); font-size:11px; text-transform:uppercase;">
                        <th style="padding:6px 10px; text-align:left;">Дата</th>
                        <th style="padding:6px 10px; text-align:right;">Гор. вода</th>
                        <th style="padding:6px 10px; text-align:right;">Хол. вода</th>
                        <th style="padding:6px 10px; text-align:left;">Статус</th>
                        <th style="padding:6px 10px;"></th>
                    </tr>
                </thead>
                <tbody>${rowsHtml}</tbody>
            </table>
        </div>`;
    },

    _toggleUserGroup(key) {
        if (this.state.expandedUserIds.has(key)) {
            this.state.expandedUserIds.delete(key);
        } else {
            this.state.expandedUserIds.add(key);
        }
        this._renderGrouped();
    },

    /**
     * Утвердить все pending/conflict подачи одного жильца. Используем
     * существующий /bulk-approve. После выполнения карточка пропадёт
     * (если активный фильтр) либо обновится статус.
     */
    async groupApproveAll(userKey) {
        const group = this._groupRowsByUser().find(g => g.key === userKey);
        if (!group || !group.userId) return;

        const ids = group.rows
            .filter(r => r.status === 'pending' || r.status === 'conflict')
            .map(r => r.id);
        if (!ids.length) return;

        const lastRow = group.rows[group.rows.length - 1];
        const onlyLast = ids.length > 1 && confirm(
            `У жильца «${group.username || group.fio}» ${ids.length} необработанных подач.\n\n` +
            `OK — утвердить ТОЛЬКО последнюю (${fmtDateTime(lastRow.sheet_timestamp)}, ` +
            `ГВС=${fmtNum(lastRow.hot_water)}, ХВС=${fmtNum(lastRow.cold_water)})\n` +
            `Отмена — утвердить ВСЕ ${ids.length} по очереди`
        );

        const targetIds = onlyLast
            ? [lastRow.id]
            : ids;

        // Оптимистичное удаление
        const setIds = new Set(targetIds);
        this.state.rows = this.state.rows.filter(r => !setIds.has(r.id));
        this.renderRows();

        try {
            const data = await api.post('/admin/gsheets/rows/bulk-approve', { row_ids: targetIds });
            const msg = `Утверждено: ${data.approved}${data.failed?.length ? `, ошибок: ${data.failed.length}` : ''}`;
            toast(msg, data.failed?.length ? 'warning' : 'success');
            if (data.failed?.length) {
                data.failed.slice(0, 3).forEach(f => toast(`#${f.row_id}: ${f.reason}`, 'error'));
            }
            this.refresh();
        } catch (e) {
            toast('Ошибка: ' + e.message, 'error');
            this.refresh();
        }
    },

    // ========================================================
    // OPTIMISTIC UI — моментальное удаление строки после действия
    // ========================================================
    _removeRowLocally(rowId, newStatus) {
        this.state.rows = this.state.rows.filter(r => r.id !== rowId);
        this.state.total = Math.max(0, this.state.total - (this.state.activeOnly ? 1 : 0));
        // Обновляем счётчики статистики на лету
        if (this.state.stats && this.state.stats.by_status) {
            // Предыдущий статус мы не знаем точно, но можем вычесть 1 из pending/conflict/unmatched
            // и добавить 1 к newStatus. Простая «ленивая» версия — просто уменьшить total.
            if (newStatus && this.state.stats.by_status[newStatus] !== undefined) {
                this.state.stats.by_status[newStatus] += 1;
            } else if (newStatus) {
                this.state.stats.by_status[newStatus] = 1;
            }
        }
        this.renderRows();
        this.renderPagination();
    },

    async triggerSync() {
        const btn = this.dom.btnSync;
        btn.disabled = true;
        const originalHTML = btn.innerHTML;
        btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Запуск…';
        try {
            await api.post('/admin/gsheets/sync', {});
            toast('Синхронизация запущена. Новые строки появятся через 5-10 секунд.', 'success');
            // Ждём 5 секунд → обновляем (через auto-refresh само обновится, но пользователь ждёт).
            setTimeout(() => this.refresh(), 5000);
            setTimeout(() => this.refresh(), 15000);
        } catch (e) {
            toast('Не удалось запустить: ' + e.message, 'error');
        } finally {
            btn.disabled = false;
            btn.innerHTML = originalHTML;
        }
    },

    async approveRow(rowId, { skipConfirm = false } = {}) {
        const row = this.state.rows.find(r => r.id === rowId);
        if (!row) return;
        if (!skipConfirm && !confirm(`Утвердить показания жильца «${row.matched_username || row.raw_fio}»?\nБудет создан MeterReading.`)) return;

        // Оптимистичное удаление
        this._removeRowLocally(rowId, 'approved');
        try {
            await api.post(`/admin/gsheets/rows/${rowId}/approve`);
            toast('Показание утверждено', 'success');
            this.loadStats();
        } catch (e) {
            // 409 с conflict-структурой — открываем сравнительную модалку.
            // Старое показание уже утверждено, новое пришло из gsheets: даём
            // админу увидеть разницу и решить — заменить или оставить старое.
            if (e.status === 409 && e.data && e.data.conflict) {
                this.refresh();  // возвращаем строку обратно в список
                this._showConflictModal(rowId, e.data.conflict);
                return;
            }
            toast('Ошибка: ' + e.message, 'error');
            this.refresh();  // откатываемся на серверное состояние
        }
    },

    _showConflictModal(rowId, conflict) {
        const ex = conflict.existing || {};
        const inc = conflict.incoming || {};
        const createdAt = ex.created_at ? new Date(ex.created_at).toLocaleString('ru-RU') : '—';

        const dim = (v) => v === null || v === undefined ? '—' : Number(v).toFixed(3);
        const diff = (a, b) => {
            const d = Number(b) - Number(a);
            if (!isFinite(d) || d === 0) return '<span style="color:#6b7280;">= совпадает</span>';
            const sign = d > 0 ? '+' : '';
            const color = d > 0 ? '#059669' : '#dc2626';
            return `<span style="color:${color}; font-weight:600;">${sign}${d.toFixed(3)}</span>`;
        };

        // Удаляем прошлую конфликт-модалку, если вдруг осталась
        document.getElementById('gsheetsConflictModal')?.remove();

        const modal = document.createElement('div');
        modal.id = 'gsheetsConflictModal';
        modal.className = 'modal-overlay open';
        modal.innerHTML = `
            <div class="modal-window" style="width: 640px;">
                <div class="modal-header" style="background:#fef3c7; border-bottom:1px solid #fde68a;">
                    <h3 style="color:#b45309;">
                        <i class="fa-solid fa-triangle-exclamation"></i>
                        Конфликт показаний — жилец «${escapeHtml(conflict.user_username || '')}»
                    </h3>
                    <button class="close-btn close-icon" data-act="cancel">&times;</button>
                </div>
                <div class="modal-form" style="padding:18px 22px; background:var(--bg-page);">
                    <p style="margin:0 0 14px; font-size:13px; color:var(--text-secondary);">
                        За период <b>«${escapeHtml(conflict.period_name || '')}»</b> уже есть утверждённое показание.
                        Сравните старое и новое — и выберите, заменять ли.
                    </p>
                    <table style="width:100%; border-collapse:collapse; font-size:13px; background:white; border:1px solid var(--border-color); border-radius:8px; overflow:hidden;">
                        <thead>
                            <tr style="background:#f3f4f6;">
                                <th style="text-align:left; padding:10px 14px; border-bottom:1px solid var(--border-color);">Показатель</th>
                                <th style="text-align:right; padding:10px 14px; border-bottom:1px solid var(--border-color); color:#6b7280;">Старое (id=${ex.id})</th>
                                <th style="text-align:right; padding:10px 14px; border-bottom:1px solid var(--border-color); color:#1d4ed8;">Новое (из gsheets)</th>
                                <th style="text-align:right; padding:10px 14px; border-bottom:1px solid var(--border-color);">Δ</th>
                            </tr>
                        </thead>
                        <tbody>
                            <tr>
                                <td style="padding:10px 14px; color:#dc2626; border-bottom:1px solid #f3f4f6;">🔥 ГВС, м³</td>
                                <td style="padding:10px 14px; text-align:right; border-bottom:1px solid #f3f4f6; font-family:monospace;">${dim(ex.hot_water)}</td>
                                <td style="padding:10px 14px; text-align:right; border-bottom:1px solid #f3f4f6; font-family:monospace; font-weight:600;">${dim(inc.hot_water)}</td>
                                <td style="padding:10px 14px; text-align:right; border-bottom:1px solid #f3f4f6;">${diff(ex.hot_water, inc.hot_water)}</td>
                            </tr>
                            <tr>
                                <td style="padding:10px 14px; color:#2563eb; border-bottom:1px solid #f3f4f6;">💧 ХВС, м³</td>
                                <td style="padding:10px 14px; text-align:right; border-bottom:1px solid #f3f4f6; font-family:monospace;">${dim(ex.cold_water)}</td>
                                <td style="padding:10px 14px; text-align:right; border-bottom:1px solid #f3f4f6; font-family:monospace; font-weight:600;">${dim(inc.cold_water)}</td>
                                <td style="padding:10px 14px; text-align:right; border-bottom:1px solid #f3f4f6;">${diff(ex.cold_water, inc.cold_water)}</td>
                            </tr>
                            <tr>
                                <td style="padding:10px 14px; color:#d97706;">⚡ Электр., кВт·ч</td>
                                <td style="padding:10px 14px; text-align:right; font-family:monospace;">${dim(ex.electricity)}</td>
                                <td style="padding:10px 14px; text-align:right; font-family:monospace; color:#9ca3af;">не подаётся в таблице</td>
                                <td style="padding:10px 14px; text-align:right; color:#6b7280;">—</td>
                            </tr>
                        </tbody>
                    </table>
                    <p style="margin:14px 0 0; font-size:12px; color:var(--text-secondary);">
                        Дата создания старого показания: <b>${createdAt}</b>.
                        При замене старое показание (id=${ex.id}) будет удалено, а из этой строки gsheets создастся новое.
                        <br><b style="color:#b45309;">Действие необратимо.</b>
                    </p>
                </div>
                <div class="modal-footer" style="display:flex; gap:10px; justify-content:flex-end;">
                    <button class="action-btn secondary-btn" data-act="cancel">Отмена</button>
                    <button class="action-btn danger-btn" data-act="reject">Отклонить эту строку</button>
                    <button class="action-btn success-btn" data-act="replace">
                        <i class="fa-solid fa-arrows-rotate"></i> Заменить старое
                    </button>
                </div>
            </div>
        `;
        document.body.appendChild(modal);

        const close = () => modal.remove();
        modal.addEventListener('click', async (e) => {
            const btn = e.target.closest('[data-act]');
            if (!btn) return;
            const act = btn.dataset.act;
            if (act === 'cancel') { close(); return; }
            if (act === 'reject') {
                close();
                await this.rejectRow(rowId);
                return;
            }
            if (act === 'replace') {
                if (!confirm(`Удалить старое показание (id=${ex.id}) и создать новое из этой строки? Действие необратимо.`)) return;
                btn.disabled = true;
                btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Заменяем…';
                try {
                    await api.delete(`/admin/readings/${ex.id}`);
                    close();
                    await this.approveRow(rowId, { skipConfirm: true });
                    toast('Старое показание удалено, новое утверждено', 'success');
                } catch (err) {
                    toast('Ошибка замены: ' + err.message, 'error');
                    btn.disabled = false;
                    btn.innerHTML = '<i class="fa-solid fa-arrows-rotate"></i> Заменить старое';
                }
            }
        });
    },

    async rejectRow(rowId) {
        const row = this.state.rows.find(r => r.id === rowId);
        if (!row) return;
        if (!confirm(`Отклонить строку жильца «${row.matched_username || row.raw_fio}»?`)) return;

        this._removeRowLocally(rowId, 'rejected');
        try {
            await api.post(`/admin/gsheets/rows/${rowId}/reject`);
            this.loadStats();
        } catch (e) {
            toast('Ошибка: ' + e.message, 'error');
            this.refresh();
        }
    },

    async deleteRow(rowId) {
        if (!confirm('Удалить отклонённую строку безвозвратно?')) return;
        this._removeRowLocally(rowId, null);
        try {
            await api.delete(`/admin/gsheets/rows/${rowId}`);
            this.loadStats();
        } catch (e) {
            toast('Ошибка: ' + e.message, 'error');
            this.refresh();
        }
    },

    async reassignPrompt(rowId) {
        const row = this.state.rows.find(r => r.id === rowId);
        const initialQuery = row?.raw_fio || '';
        const candidatesPromise = row && row.matched_user_id == null
            ? api.get(`/admin/gsheets/rows/${rowId}/relative-candidates`)
                .catch(() => ({ candidates: [] }))
            : Promise.resolve({ candidates: [] });

        const result = await this._showReassignModal({
            rowId,
            rawFio: row?.raw_fio || '',
            rawRoom: row?.raw_room_number || '',
            initialQuery,
            candidatesPromise,
        });
        if (!result) return;

        try {
            const endpoint = result.asRelative
                ? `/admin/gsheets/rows/${rowId}/confirm-relative`
                : `/admin/gsheets/rows/${rowId}/reassign`;
            const payload = result.asRelative
                ? { user_id: result.userId, note: result.note || 'Родственник' }
                : { user_id: result.userId, remember: result.remember, note: result.note || null };
            const resp = await api.post(endpoint, payload);

            // Backend теперь подхватывает все остальные unmatched/conflict
            // строки того же ФИО — сообщаем сколько.
            const siblings = resp?.siblings_updated || 0;
            const siblingsPart = siblings > 0
                ? ` + привязано ещё ${siblings} ${siblings === 1 ? 'подача' : (siblings < 5 ? 'подачи' : 'подач')} того же ФИО`
                : '';

            const msg = result.asRelative
                ? `Подтверждено: подача от родственника ${result.username}.${siblingsPart}`
                : (resp?.alias_created
                    ? `Жилец переназначен на ${result.username}, ФИО «${row?.raw_fio || ''}» запомнено.${siblingsPart}`
                    : `Жилец переназначен на ${result.username}.${siblingsPart}`);
            toast(msg, 'success');
            this.refresh();
        } catch (e) {
            toast('Ошибка: ' + e.message, 'error');
        }
    },

    /**
     * Показывает модалку с поиском жильца по ФИО и блоком «возможные
     * родственники». Возвращает {userId, username, asRelative, remember, note}
     * или null если пользователь закрыл окно.
     */
    _showReassignModal({ rowId, rawFio, rawRoom, initialQuery, candidatesPromise }) {
        return new Promise((resolve) => {
            // Удаляем старую модалку если осталась.
            document.getElementById('gsheetsReassignModal')?.remove();

            const modal = document.createElement('div');
            modal.id = 'gsheetsReassignModal';
            modal.style.cssText = 'position:fixed; inset:0; background:rgba(0,0,0,0.5); display:flex; align-items:center; justify-content:center; z-index:9999;';
            modal.innerHTML = `
                <div style="background:var(--bg-card); border-radius:14px; max-width:680px; width:92%; max-height:88vh; overflow:hidden; display:flex; flex-direction:column; box-shadow:0 24px 64px rgba(0,0,0,0.35);">
                    <div style="padding:18px 22px; border-bottom:1px solid var(--border-color); display:flex; justify-content:space-between; align-items:flex-start; gap:12px;">
                        <div>
                            <h3 style="margin:0 0 4px 0; font-size:16px;">Переназначить жильца</h3>
                            <div style="font-size:12px; color:var(--text-secondary);">
                                Подача: <b>${escapeHtml(rawFio)}</b>${rawRoom ? ` · комн. ${escapeHtml(rawRoom)}` : ''}
                            </div>
                        </div>
                        <button class="icon-btn" data-close="1" title="Закрыть" style="font-size:18px;">
                            <i class="fa-solid fa-xmark"></i>
                        </button>
                    </div>

                    <div id="reassignSuggestions" style="padding:14px 22px 0;"></div>

                    <div style="padding:14px 22px 4px;">
                        <label style="font-size:12px; color:var(--text-secondary); display:block; margin-bottom:6px;">
                            Поиск жильца по ФИО или номеру комнаты
                        </label>
                        <input type="text" id="reassignSearch" autocomplete="off"
                               placeholder="Начните вводить фамилию или номер комнаты…"
                               style="width:100%; padding:10px 12px; font-size:14px; border:1px solid var(--border-color); border-radius:8px;">
                    </div>

                    <div id="reassignResults" style="flex:1; overflow:auto; padding:6px 12px 12px;">
                        <div style="padding:30px; text-align:center; color:var(--text-secondary); font-size:13px;">
                            Введите 2+ символа для поиска
                        </div>
                    </div>

                    <div style="padding:12px 22px; border-top:1px solid var(--border-color); display:flex; justify-content:space-between; align-items:center; gap:12px; flex-wrap:wrap;">
                        <label style="display:inline-flex; align-items:center; gap:8px; font-size:12px; color:var(--text-secondary); cursor:pointer; user-select:none;">
                            <input type="checkbox" id="reassignRemember" checked>
                            Запомнить «${escapeHtml(rawFio)}» за этим жильцом (auto-match впредь)
                        </label>
                        <button class="action-btn secondary-btn" data-close="1" style="padding:8px 16px;">Отмена</button>
                    </div>
                </div>`;
            document.body.appendChild(modal);

            const close = (value) => {
                modal.remove();
                resolve(value);
            };
            modal.addEventListener('click', (e) => {
                if (e.target === modal || e.target.closest('[data-close]')) close(null);
            });

            const input = modal.querySelector('#reassignSearch');
            const results = modal.querySelector('#reassignResults');
            const suggestionsBlock = modal.querySelector('#reassignSuggestions');
            const remember = modal.querySelector('#reassignRemember');

            // Карточка кандидата. Режимы:
            //   * opts.suggestion=true  — две кнопки: «Это он» (прямая привязка,
            //     когда в таблице указана только фамилия и это тот самый жилец)
            //     + «Это родственник» (привязка с note-ролью типа «жена»).
            //   * opts.suggestion=false — одна кнопка «Выбрать» (из поиска).
            const renderCandidate = (c, opts = {}) => {
                const reasonHtml = opts.reason
                    ? `<div style="font-size:11px; color:#92400e; margin-top:4px;">
                          <i class="fa-solid fa-circle-info"></i> ${escapeHtml(opts.reason)}
                          ${opts.score != null ? `<span style="margin-left:6px; opacity:0.7;">${opts.score}%</span>` : ''}
                       </div>`
                    : '';
                const buttons = opts.suggestion
                    ? `
                        <button class="action-btn primary-btn" data-pick="1" data-kind="self"
                                style="padding:5px 10px; font-size:12px; white-space:nowrap;"
                                title="Это тот самый жилец (в таблице была только фамилия)">
                            <i class="fa-solid fa-check"></i> Это он
                        </button>
                        <button class="action-btn success-btn" data-pick="1" data-kind="relative"
                                style="padding:5px 10px; font-size:12px; white-space:nowrap;"
                                title="Родственник этого жильца (жена, муж, ребёнок…)">
                            <i class="fa-solid fa-people-roof"></i> Родственник
                        </button>`
                    : `
                        <button class="action-btn primary-btn" data-pick="1" data-kind="self"
                                style="padding:5px 10px; font-size:12px; white-space:nowrap;">
                            <i class="fa-solid fa-check"></i> Выбрать
                        </button>`;

                return `
                    <div class="reassign-candidate" data-user-id="${c.id}" data-username="${escapeHtml(c.username)}"
                         style="display:flex; justify-content:space-between; align-items:center; gap:10px; padding:10px 12px; border:1px solid var(--border-color); border-radius:8px; margin-bottom:6px; background:var(--bg-card);">
                        <div style="flex:1; min-width:0;">
                            <div style="font-weight:600; font-size:13px;">${escapeHtml(c.username)}</div>
                            <div style="color:var(--text-secondary); font-size:11px;">
                                ${escapeHtml(c.room || 'без комнаты')} · ${c.residents_count || 1} чел.
                            </div>
                            ${reasonHtml}
                        </div>
                        <div style="display:flex; gap:6px; flex-shrink:0;">${buttons}</div>
                    </div>`;
            };

            // Делегирование клика. kind='self' — прямая привязка,
            // 'relative' — спросить роль и проставить asRelative.
            modal.addEventListener('click', (e) => {
                const btn = e.target.closest('[data-pick="1"]');
                if (!btn) return;
                const card = btn.closest('.reassign-candidate');
                if (!card) return;
                const kind = btn.dataset.kind || 'self';
                const asRelative = kind === 'relative';
                let note = null;
                if (asRelative) {
                    note = prompt(
                        'Кем является «' + rawFio + '» жильцу «' + card.dataset.username + '»?\n'
                        + 'Например: жена, муж, дочь, сын, мать, отец',
                        'Жена',
                    );
                    if (note === null) return;  // отмена
                }
                close({
                    userId: Number(card.dataset.userId),
                    username: card.dataset.username,
                    asRelative,
                    remember: asRelative ? true : remember.checked,
                    note,
                });
            });

            // Подгружаем подсказки родственников (если есть rowId).
            // Блок ограничен по высоте — при 10+ кандидатах появляется внутренний
            // скролл, а не распирает модалку (раньше поиск и результаты уезжали
            // за нижний край окна).
            candidatesPromise.then(data => {
                const cands = data?.candidates || [];
                if (!cands.length) {
                    suggestionsBlock.innerHTML = '';
                    return;
                }
                suggestionsBlock.innerHTML = `
                    <div style="background:#fef9c3; border:1px solid #fde68a; border-radius:10px; padding:12px 14px; margin-bottom:8px;">
                        <div style="font-size:13px; font-weight:600; color:#92400e; margin-bottom:8px; display:flex; justify-content:space-between; align-items:center;">
                            <span><i class="fa-solid fa-lightbulb"></i> Возможные кандидаты (${cands.length})</span>
                            <span style="font-size:11px; font-weight:normal; color:#92400e; opacity:0.8;">Прокрутите список ↓</span>
                        </div>
                        <div style="max-height:38vh; overflow-y:auto; padding-right:4px;">
                            ${cands.map(c => renderCandidate(c, {
                                reason: c.reason,
                                score: c.score,
                                suggestion: true,
                            })).join('')}
                        </div>
                    </div>`;
            }).catch(() => { suggestionsBlock.innerHTML = ''; });

            // Живой поиск с debounce
            let searchTimer = null;
            const doSearch = async (query) => {
                if (query.length < 2) {
                    results.innerHTML = `<div style="padding:30px; text-align:center; color:var(--text-secondary); font-size:13px;">Введите 2+ символа для поиска</div>`;
                    return;
                }
                results.innerHTML = `<div style="padding:20px; text-align:center; color:var(--text-secondary); font-size:13px;"><i class="fa-solid fa-spinner fa-spin"></i> Поиск…</div>`;
                try {
                    const data = await api.get(`/admin/gsheets/search-users?q=${encodeURIComponent(query)}&limit=20`);
                    const items = data?.items || [];
                    if (!items.length) {
                        results.innerHTML = `<div style="padding:30px; text-align:center; color:var(--text-secondary); font-size:13px;">Ничего не найдено по запросу «${escapeHtml(query)}»</div>`;
                        return;
                    }
                    results.innerHTML = items.map(c => renderCandidate(c)).join('');
                } catch (e) {
                    results.innerHTML = `<div style="padding:20px; color:var(--danger-color); font-size:12px;">Ошибка поиска: ${escapeHtml(e.message)}</div>`;
                }
            };

            input.addEventListener('input', () => {
                clearTimeout(searchTimer);
                searchTimer = setTimeout(() => doSearch(input.value.trim()), 300);
            });

            // Автостарт: подставляем raw_fio в инпут и сразу ищем — часто
            // совпадение почти точное, и админу остаётся только нажать «Выбрать».
            input.value = initialQuery;
            doSearch(initialQuery);
            input.focus();
            input.select();
        });
    },

    async bulkApprove() {
        const checked = Array.from(
            this.dom.tbody.querySelectorAll('input.row-check:checked')
        ).map(cb => Number(cb.dataset.rowId));

        if (!checked.length) {
            toast('Сначала отметьте строки галочками', 'info');
            return;
        }
        if (!confirm(`Утвердить ${checked.length} строк? Для каждой будет создан MeterReading.`)) return;

        // Оптимистичное удаление
        const toRemove = new Set(checked);
        this.state.rows = this.state.rows.filter(r => !toRemove.has(r.id));
        this.renderRows();
        this.renderPagination();

        try {
            const data = await api.post('/admin/gsheets/rows/bulk-approve', { row_ids: checked });
            const msg = `Утверждено: ${data.approved}${data.failed?.length ? `, ошибок: ${data.failed.length}` : ''}`;
            toast(msg, data.failed?.length ? 'warning' : 'success');
            if (data.failed?.length) {
                console.warn('Ошибки при массовом утверждении:', data.failed);
                // Показываем первые 3 ошибки в toast
                data.failed.slice(0, 3).forEach(f => {
                    toast(`Строка #${f.row_id}: ${f.reason}`, 'error');
                });
            }
            this.refresh();
        } catch (e) {
            toast('Ошибка: ' + e.message, 'error');
            this.refresh();
        }
    },

    // ========================================================
    // МОДАЛКА ИСТОРИИ ПОДАЧ ЖИЛЬЦА
    // ========================================================
    async showUserHistory(userId) {
        let modal = document.getElementById('gsheetsUserModal');
        if (!modal) {
            modal = document.createElement('div');
            modal.id = 'gsheetsUserModal';
            modal.style.cssText = 'position:fixed; top:0; left:0; right:0; bottom:0; background:rgba(0,0,0,0.5); display:flex; align-items:center; justify-content:center; z-index:9999;';
            modal.innerHTML = `
                <div style="background:#fff; border-radius:12px; max-width:780px; width:90%; max-height:85vh; overflow:auto; box-shadow:0 20px 60px rgba(0,0,0,0.3);">
                    <div style="padding:20px 24px; border-bottom:1px solid var(--border-color); display:flex; justify-content:space-between; align-items:center; position:sticky; top:0; background:#fff; z-index:1;">
                        <h3 id="gsheetsModalTitle" style="margin:0;">История подач</h3>
                        <button id="gsheetsModalClose" class="icon-btn" style="font-size:18px;">
                            <i class="fa-solid fa-xmark"></i>
                        </button>
                    </div>
                    <div id="gsheetsModalBody" style="padding:20px 24px;">Загрузка...</div>
                </div>
            `;
            document.body.appendChild(modal);

            modal.addEventListener('click', (e) => {
                if (e.target === modal || e.target.closest('#gsheetsModalClose')) {
                    modal.style.display = 'none';
                }
            });
        }

        modal.style.display = 'flex';
        const body = document.getElementById('gsheetsModalBody');
        body.innerHTML = '<div style="text-align:center; padding:40px; color:var(--text-secondary);"><i class="fa-solid fa-spinner fa-spin"></i> Загрузка истории…</div>';

        try {
            const data = await api.get(`/admin/gsheets/users/${userId}/history`);
            this._renderUserHistory(data);
        } catch (e) {
            body.innerHTML = `<div style="color:var(--danger-color); padding:20px;">Ошибка: ${escapeHtml(e.message)}</div>`;
        }
    },

    _renderUserHistory(data) {
        document.getElementById('gsheetsModalTitle').textContent =
            `${data.user.username} — история подач`;

        const body = document.getElementById('gsheetsModalBody');

        const lastGs = fmtDateTime(data.last_gsheet_submission);
        const lastAp = fmtDateTime(data.last_approved_reading);
        const deltaHot = data.delta_hot != null ? fmtNum(data.delta_hot) : '—';
        const deltaCold = data.delta_cold != null ? fmtNum(data.delta_cold) : '—';

        // Верхние карточки
        const header = `
            <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(140px, 1fr)); gap:10px; margin-bottom:20px;">
                <div style="background:#f9fafb; padding:12px; border-radius:8px;">
                    <div style="font-size:11px; color:var(--text-secondary); text-transform:uppercase;">Помещение</div>
                    <div style="font-weight:600; margin-top:4px;">${escapeHtml(data.user.room || '—')}</div>
                </div>
                <div style="background:#f9fafb; padding:12px; border-radius:8px;">
                    <div style="font-size:11px; color:var(--text-secondary); text-transform:uppercase;">Жильцов в комнате</div>
                    <div style="font-weight:600; margin-top:4px;">${data.user.residents_count}</div>
                </div>
                <div style="background:#dbeafe; padding:12px; border-radius:8px;">
                    <div style="font-size:11px; color:#1e40af; text-transform:uppercase;">Послед. подача в Sheets</div>
                    <div style="font-weight:600; margin-top:4px; font-size:13px;">${lastGs}</div>
                </div>
                <div style="background:#d1fae5; padding:12px; border-radius:8px;">
                    <div style="font-size:11px; color:#065f46; text-transform:uppercase;">Послед. в системе</div>
                    <div style="font-weight:600; margin-top:4px; font-size:13px;">${lastAp}</div>
                </div>
                <div style="background:#fef3c7; padding:12px; border-radius:8px;">
                    <div style="font-size:11px; color:#92400e; text-transform:uppercase;">Расход за послед. период</div>
                    <div style="font-weight:600; margin-top:4px;">🔥 ${deltaHot} &nbsp;·&nbsp; 💧 ${deltaCold}</div>
                </div>
            </div>
        `;

        // Таблица GSheets-строк
        const gsheetsTable = data.gsheets_rows.length ? `
            <h4 style="margin:20px 0 10px;"><i class="fa-solid fa-table" style="color:#16a34a; margin-right:6px;"></i>Подачи через Google Sheets (${data.gsheets_rows.length})</h4>
            <table style="width:100%; font-size:13px; border-collapse:collapse;">
                <thead style="background:#f9fafb;">
                    <tr><th style="padding:6px 8px; text-align:left;">Дата</th><th style="padding:6px 8px;">Комн.</th><th style="padding:6px 8px; text-align:right;">ГВС</th><th style="padding:6px 8px; text-align:right;">ХВС</th><th style="padding:6px 8px;">Статус</th></tr>
                </thead>
                <tbody>
                ${data.gsheets_rows.map(r => {
                    const m = STATUS_META[r.status] || { label: r.status, color: '#6b7280', bg: '#f3f4f6' };
                    return `<tr style="border-bottom:1px solid #f3f4f6;">
                        <td style="padding:6px 8px; font-size:12px;">${escapeHtml(fmtDateTime(r.sheet_timestamp))}</td>
                        <td style="padding:6px 8px;">${escapeHtml(r.raw_room_number || '—')}</td>
                        <td style="padding:6px 8px; text-align:right; font-family:monospace;">${fmtNum(r.hot_water)}</td>
                        <td style="padding:6px 8px; text-align:right; font-family:monospace;">${fmtNum(r.cold_water)}</td>
                        <td style="padding:6px 8px;">
                            <span style="padding:1px 6px; border-radius:10px; background:${m.bg}; color:${m.color}; font-size:10px; font-weight:600;">${escapeHtml(m.label)}</span>
                        </td>
                    </tr>`;
                }).join('')}
                </tbody>
            </table>
        ` : '<p style="color:var(--text-secondary); margin:20px 0;">В Google Sheets подач не найдено.</p>';

        // Таблица утверждённых MeterReading
        const readingsTable = data.approved_readings.length ? `
            <h4 style="margin:20px 0 10px;"><i class="fa-solid fa-check-double" style="color:#10b981; margin-right:6px;"></i>Утверждённые показания в системе (${data.approved_readings.length})</h4>
            <table style="width:100%; font-size:13px; border-collapse:collapse;">
                <thead style="background:#f9fafb;">
                    <tr><th style="padding:6px 8px; text-align:left;">Создано</th><th style="padding:6px 8px;">Период</th><th style="padding:6px 8px; text-align:right;">ГВС</th><th style="padding:6px 8px; text-align:right;">ХВС</th><th style="padding:6px 8px; text-align:right;">Электр.</th><th style="padding:6px 8px;">Флаги</th></tr>
                </thead>
                <tbody>
                ${data.approved_readings.map(r => `
                    <tr style="border-bottom:1px solid #f3f4f6;">
                        <td style="padding:6px 8px; font-size:12px;">${escapeHtml(fmtDateTime(r.created_at))}</td>
                        <td style="padding:6px 8px;">${escapeHtml(r.period || '—')}</td>
                        <td style="padding:6px 8px; text-align:right; font-family:monospace;">${fmtNum(r.hot_water)}</td>
                        <td style="padding:6px 8px; text-align:right; font-family:monospace;">${fmtNum(r.cold_water)}</td>
                        <td style="padding:6px 8px; text-align:right; font-family:monospace;">${fmtNum(r.electricity)}</td>
                        <td style="padding:6px 8px; font-size:11px; color:var(--text-secondary);">${escapeHtml(r.anomaly_flags || '')}</td>
                    </tr>
                `).join('')}
                </tbody>
            </table>
        ` : '<p style="color:var(--text-secondary); margin:20px 0;">Утверждённых показаний в системе нет.</p>';

        body.innerHTML = header + gsheetsTable + readingsTable;
    },
};
