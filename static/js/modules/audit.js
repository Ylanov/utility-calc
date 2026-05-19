// static/js/modules/audit.js
//
// Журнал действий администраторов — multi-admin прозрачность.
// Источник: GET /api/admin/audit-log (paginated) + /actions (для фильтров).
// Lazy-загрузка: модуль грузится при первом клике на вкладку «Журнал».

import { api } from '../core/api.js';
import { toast, escapeHtml } from '../core/dom.js';

export const AuditModule = {
    isInitialized: false,
    state: {
        page: 1,
        limit: 100,
        total: 0,
        action: '',
        entity: '',
        userFilter: '',
    },

    async init() {
        const root = document.getElementById('audit');
        if (!root) return;

        if (!this.isInitialized) {
            this._bindEvents();
            this.isInitialized = true;
        }
        // Грузим фильтры (one shot) + первая страница.
        await this._loadFilters();
        await this.refresh();
    },

    _bindEvents() {
        document.getElementById('btnAuditRefresh')?.addEventListener('click', () => this.refresh());
        document.getElementById('btnAuditPrev')?.addEventListener('click', () => {
            if (this.state.page > 1) {
                this.state.page -= 1;
                this.refresh();
            }
        });
        document.getElementById('btnAuditNext')?.addEventListener('click', () => {
            const maxPage = Math.ceil(this.state.total / this.state.limit) || 1;
            if (this.state.page < maxPage) {
                this.state.page += 1;
                this.refresh();
            }
        });
        // Фильтры — при изменении сбрасываем page и перезагружаем.
        document.getElementById('auditActionFilter')?.addEventListener('change', (e) => {
            this.state.action = e.target.value;
            this.state.page = 1;
            this.refresh();
        });
        document.getElementById('auditEntityFilter')?.addEventListener('change', (e) => {
            this.state.entity = e.target.value;
            this.state.page = 1;
            this.refresh();
        });
        document.getElementById('auditLimitFilter')?.addEventListener('change', (e) => {
            this.state.limit = parseInt(e.target.value) || 100;
            this.state.page = 1;
            this.refresh();
        });
        let userFilterTimer = null;
        document.getElementById('auditUserFilter')?.addEventListener('input', (e) => {
            clearTimeout(userFilterTimer);
            const val = e.target.value;
            userFilterTimer = setTimeout(() => {
                this.state.userFilter = val.trim().toLowerCase();
                this.state.page = 1;
                this._render(this._lastItems || []);
                this._updatePagination();
            }, 250);
        });
    },

    async _loadFilters() {
        try {
            const data = await api.get('/admin/audit-log/actions');
            const actionSel = document.getElementById('auditActionFilter');
            const entitySel = document.getElementById('auditEntityFilter');
            if (actionSel && data?.actions) {
                actionSel.innerHTML = '<option value="">Все действия</option>' +
                    data.actions.map((a) =>
                        `<option value="${escapeHtml(a.name)}">${this._actionLabel(a.name)} (${a.count})</option>`
                    ).join('');
            }
            if (entitySel && data?.entities) {
                entitySel.innerHTML = '<option value="">Все сущности</option>' +
                    data.entities.map((e) =>
                        `<option value="${escapeHtml(e.name)}">${this._entityLabel(e.name)} (${e.count})</option>`
                    ).join('');
            }
        } catch (e) {
            console.warn('[audit] filters load failed:', e);
        }
    },

    async refresh() {
        const tbody = document.getElementById('auditTableBody');
        if (tbody) {
            tbody.innerHTML = `<tr><td colspan="6" class="text-center" style="padding: 40px; color: var(--text-secondary);">
                <i class="fa-solid fa-spinner fa-spin"></i> Загрузка...
            </td></tr>`;
        }
        try {
            const qs = new URLSearchParams({
                page: this.state.page,
                limit: this.state.limit,
            });
            if (this.state.action) qs.set('action', this.state.action);
            if (this.state.entity) qs.set('entity_type', this.state.entity);
            const data = await api.get('/admin/audit-log?' + qs.toString());
            this.state.total = data.total;
            this._lastItems = data.items;
            this._render(data.items);
            this._updatePagination();
        } catch (e) {
            toast('Ошибка загрузки журнала: ' + e.message, 'error');
        }
    },

    _render(items) {
        const tbody = document.getElementById('auditTableBody');
        if (!tbody) return;

        // Client-side фильтр по администратору (поиск по username).
        let filtered = items;
        if (this.state.userFilter) {
            filtered = items.filter((it) =>
                (it.username || '').toLowerCase().includes(this.state.userFilter)
            );
        }

        if (!filtered.length) {
            tbody.innerHTML = `<tr><td colspan="6" class="text-center" style="padding: 40px; color: var(--text-secondary);">
                Записей не найдено
            </td></tr>`;
            return;
        }
        tbody.innerHTML = filtered.map((it) => this._row(it)).join('');
    },

    _row(it) {
        const actionLabel = this._actionLabel(it.action);
        const actionColor = this._actionColor(it.action);
        const entityLabel = this._entityLabel(it.entity_type);
        const details = this._formatDetails(it.details);

        return `
            <tr>
                <td style="font-size: 12px; color: var(--text-secondary); white-space: nowrap;">
                    ${escapeHtml(it.created_at || '—')}
                </td>
                <td>
                    <span style="font-weight: 600; font-size: 13px;">${escapeHtml(it.username || '—')}</span>
                </td>
                <td>
                    <span style="background: ${actionColor.bg}; color: ${actionColor.text};
                                 padding: 3px 8px; border-radius: 6px; font-size: 12px; font-weight: 600;">
                        ${escapeHtml(actionLabel)}
                    </span>
                </td>
                <td style="font-size: 12px; color: var(--text-secondary);">${escapeHtml(entityLabel)}</td>
                <td style="font-family: monospace; font-size: 11px; color: var(--text-secondary);">
                    ${it.entity_id ?? '—'}
                </td>
                <td style="font-size: 12px; max-width: 400px; word-break: break-word;">
                    ${details}
                </td>
            </tr>
        `;
    },

    _updatePagination() {
        const info = document.getElementById('auditPageInfo');
        if (info) {
            const maxPage = Math.ceil(this.state.total / this.state.limit) || 1;
            info.textContent = `Стр. ${this.state.page} из ${maxPage} (всего: ${this.state.total})`;
        }
        const prev = document.getElementById('btnAuditPrev');
        const next = document.getElementById('btnAuditNext');
        if (prev) prev.disabled = this.state.page <= 1;
        if (next) {
            const maxPage = Math.ceil(this.state.total / this.state.limit) || 1;
            next.disabled = this.state.page >= maxPage;
        }
    },

    _formatDetails(details) {
        if (!details) return '<span style="color: var(--text-muted);">—</span>';
        if (typeof details === 'string') return escapeHtml(details);
        // Прячем длинные значения, показываем красиво
        const entries = Object.entries(details).slice(0, 5);
        return entries.map(([k, v]) => {
            let val = typeof v === 'object' ? JSON.stringify(v) : String(v);
            if (val.length > 60) val = val.slice(0, 57) + '…';
            return `<div><b>${escapeHtml(k)}:</b> ${escapeHtml(val)}</div>`;
        }).join('');
    },

    _actionLabel(action) {
        const map = {
            create: 'Создание',
            update: 'Обновление',
            delete: 'Удаление',
            soft_delete: 'Мягкое удаление',
            approve: 'Утверждение',
            reject: 'Отклонение',
            gsheets_approve: 'GSheets утверждение',
            close_period: 'Закрытие периода',
            open_period: 'Открытие периода',
            recalc_start: 'Запуск пересчёта',
            recalc_apply: 'Применение пересчёта',
            pdn_consent_accept: '✓ Согласие на ПД',
            data_deletion_request: '⚠ Запрос удаления данных',
            bulk_approve: 'Массовое утверждение',
        };
        return map[action] || action;
    },

    _actionColor(action) {
        const map = {
            create: { bg: '#d1fae5', text: '#065f46' },
            update: { bg: '#dbeafe', text: '#1e40af' },
            delete: { bg: '#fee2e2', text: '#991b1b' },
            soft_delete: { bg: '#fef3c7', text: '#92400e' },
            approve: { bg: '#d1fae5', text: '#065f46' },
            reject: { bg: '#fee2e2', text: '#991b1b' },
            close_period: { bg: '#fef3c7', text: '#92400e' },
            recalc_start: { bg: '#e0e7ff', text: '#3730a3' },
            recalc_apply: { bg: '#e0e7ff', text: '#3730a3' },
            pdn_consent_accept: { bg: '#d1fae5', text: '#065f46' },
            data_deletion_request: { bg: '#fee2e2', text: '#991b1b' },
        };
        return map[action] || { bg: '#f3f4f6', text: '#374151' };
    },

    _entityLabel(entity) {
        const map = {
            user: 'Жилец',
            room: 'Комната',
            tariff: 'Тариф',
            reading: 'Показания',
            period: 'Период',
            adjustment: 'Корректировка',
        };
        return map[entity] || entity;
    },
};
