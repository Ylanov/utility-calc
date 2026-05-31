// static/js/modules/tickets.js
//
// Админская вкладка «Обращения». Список тикетов с фильтром по статусу,
// раскрытие для прочтения + форма ответа.

import { api } from '../core/api.js';
import { toast, escapeHtml, showConfirm, showPrompt } from '../core/dom.js';

export const TicketsModule = {
    isInitialized: false,
    state: { status: 'open' },

    async init() {
        const root = document.getElementById('tickets');
        if (!root) return;
        if (!this.isInitialized) {
            this._bind();
            this.isInitialized = true;
        }
        await this.refresh();
    },

    _bind() {
        document.getElementById('btnTicketsRefresh')?.addEventListener('click', () => this.refresh());
        document.getElementById('ticketsStatusFilter')?.addEventListener('change', (e) => {
            this.state.status = e.target.value;
            this.refresh();
        });
    },

    async refresh() {
        const list = document.getElementById('ticketsList');
        if (!list) return;
        list.innerHTML = `<div class="text-center" style="padding: 40px; color: var(--text-secondary);">
            <i class="fa-solid fa-spinner fa-spin"></i> Загрузка...
        </div>`;
        try {
            const qs = new URLSearchParams({ limit: 100 });
            if (this.state.status) qs.set('status', this.state.status);
            const data = await api.get('/admin/tickets?' + qs.toString());
            this._render(data.items);
        } catch (e) {
            toast('Ошибка загрузки обращений: ' + e.message, 'error');
        }
    },

    _render(items) {
        const list = document.getElementById('ticketsList');
        if (!list) return;
        if (!items?.length) {
            list.innerHTML = `<div class="text-center" style="padding: 40px; color: var(--text-secondary);">
                В этой категории обращений нет
            </div>`;
            return;
        }
        list.innerHTML = items.map((t) => this._row(t)).join('');
        // Bind respond-buttons
        list.querySelectorAll('[data-respond]').forEach((btn) => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const id = parseInt(btn.dataset.respond);
                this._respond(id);
            });
        });
        list.querySelectorAll('[data-close-ticket]').forEach((btn) => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const id = parseInt(btn.dataset.closeTicket);
                this._setStatus(id, 'closed');
            });
        });
    },

    _row(t) {
        const status = this._statusBadge(t.status);
        const created = t.created_at ? new Date(t.created_at).toLocaleString('ru-RU') : '—';
        const responded = t.responded_at ? new Date(t.responded_at).toLocaleString('ru-RU') : '';
        return `
            <details class="card" style="margin-bottom: 10px; padding: 0;" ${t.status === 'open' ? 'open' : ''}>
                <summary style="
                    padding: 14px 18px;
                    cursor: pointer;
                    display: flex;
                    justify-content: space-between;
                    align-items: flex-start;
                    gap: 14px;
                    list-style: none;
                ">
                    <div style="flex:1; min-width: 0;">
                        <div style="font-weight: 600; font-size: 14px; margin-bottom: 4px;">
                            ${escapeHtml(t.subject)}
                        </div>
                        <div style="font-size: 12px; color: var(--text-secondary);">
                            от <b>${escapeHtml(t.username || '—')}</b> · ${escapeHtml(created)}
                        </div>
                    </div>
                    ${status}
                </summary>
                <div style="padding: 0 18px 16px;">
                    <div style="background: var(--bg-page); border-radius: 8px; padding: 12px 14px; font-size: 13px; white-space: pre-wrap; margin-bottom: 14px;">
                        ${escapeHtml(t.message)}
                    </div>

                    ${t.admin_response ? `
                        <div style="background: var(--success-bg); border-left: 3px solid var(--success-color); border-radius: 6px; padding: 12px 14px; font-size: 13px; white-space: pre-wrap; margin-bottom: 10px;">
                            <div style="color: var(--success-color); font-size: 11px; text-transform: uppercase; letter-spacing: .4px; margin-bottom: 6px; font-weight: 600;">
                                Ответ (${escapeHtml(t.responded_by_username || '—')} · ${escapeHtml(responded)})
                            </div>
                            ${escapeHtml(t.admin_response)}
                        </div>
                    ` : ''}

                    <div style="display: flex; gap: 8px; flex-wrap: wrap;">
                        <button class="action-btn primary-btn" data-respond="${t.id}" style="padding: 8px 16px; font-size: 13px;">
                            <i class="fa-solid fa-paper-plane"></i>
                            ${t.admin_response ? 'Изменить ответ' : 'Ответить'}
                        </button>
                        ${t.status !== 'closed' ? `
                            <button class="action-btn secondary-btn" data-close-ticket="${t.id}" style="padding: 8px 16px; font-size: 13px;">
                                Закрыть без ответа
                            </button>
                        ` : ''}
                    </div>
                </div>
            </details>
        `;
    },

    _statusBadge(status) {
        const map = {
            open:        { label: 'Открыто',    bg: '#fef3c7', text: '#92400e' },
            in_progress: { label: 'В работе',   bg: '#dbeafe', text: '#1e40af' },
            answered:    { label: 'Отвечено',   bg: '#d1fae5', text: '#065f46' },
            closed:      { label: 'Закрыто',    bg: '#f3f4f6', text: '#6b7280' },
        };
        const m = map[status] || map.open;
        return `<span style="background: ${m.bg}; color: ${m.text}; padding: 3px 10px; border-radius: 12px; font-size: 11px; font-weight: 600; white-space: nowrap;">${m.label}</span>`;
    },

    async _respond(id) {
        const text = await showPrompt('Ответ жильцу', 'Введите ответ жильцу:');
        if (text === null) return;
        const trimmed = (text || '').trim();
        if (trimmed.length < 1) {
            toast('Ответ не может быть пустым', 'error');
            return;
        }
        try {
            await api.patch(`/admin/tickets/${id}`, { admin_response: trimmed });
            toast('Ответ отправлен', 'success');
            await this.refresh();
        } catch (e) {
            toast('Ошибка: ' + e.message, 'error');
        }
    },

    async _setStatus(id, status) {
        if (!await showConfirm(`Изменить статус на «${status}»?`)) return;
        try {
            await api.patch(`/admin/tickets/${id}`, { status });
            toast('Статус обновлён', 'success');
            await this.refresh();
        } catch (e) {
            toast('Ошибка: ' + e.message, 'error');
        }
    },
};
