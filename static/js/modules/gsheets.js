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
        };
    },

    bindEvents() {
        this.dom.btnSync?.addEventListener('click', () => this.triggerSync());
        this.dom.btnRefresh?.addEventListener('click', () => this.refresh());
        this.dom.btnBulkApprove?.addEventListener('click', () => this.bulkApprove());

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
        const params = new URLSearchParams({
            page: this.state.page,
            limit: this.state.limit,
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
                this.dom.tbody.innerHTML =
                    `<tr><td colspan="9" style="padding:20px; text-align:center; color:var(--danger-color);">${escapeHtml(e.message)}</td></tr>`;
            }
        }
    },

    renderRows() {
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
        const max = Math.ceil(this.state.total / this.state.limit) || 1;
        this.dom.pageLabel.textContent = `${this.state.page} / ${max}`;
        this.dom.totalInfo.textContent = `Всего: ${this.state.total}`;
        this.dom.prev.disabled = this.state.page <= 1;
        this.dom.next.disabled = this.state.page >= max;
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

    async approveRow(rowId) {
        const row = this.state.rows.find(r => r.id === rowId);
        if (!row) return;
        if (!confirm(`Утвердить показания жильца «${row.matched_username || row.raw_fio}»?\nБудет создан MeterReading.`)) return;

        // Оптимистичное удаление
        this._removeRowLocally(rowId, 'approved');
        try {
            await api.post(`/admin/gsheets/rows/${rowId}/approve`);
            toast('Показание утверждено', 'success');
            this.loadStats();
        } catch (e) {
            toast('Ошибка: ' + e.message, 'error');
            this.refresh();  // откатываемся на серверное состояние
        }
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
        const answer = prompt(
            'Введите ID жильца, которому принадлежит это показание.\n'
            + 'Найти ID можно на вкладке «Жильцы».'
        );
        if (!answer) return;
        const userId = Number(answer.trim());
        if (!userId || isNaN(userId)) {
            toast('Некорректный ID', 'error');
            return;
        }
        try {
            await api.post(`/admin/gsheets/rows/${rowId}/reassign`, { user_id: userId });
            toast('Жилец переназначен', 'success');
            this.refresh();
        } catch (e) {
            toast('Ошибка: ' + e.message, 'error');
        }
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
