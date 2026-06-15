// static/js/modules/errors.js — E3-C: «Копилка ошибок» админ-UI.
//
// Источник данных: /api/admin/errors (см. admin_errors.py).
// Главная фишка: кнопка «Скопировать в Claude (markdown)» — генерирует
// готовый markdown-блок со всем контекстом ошибки + auto-investigation,
// админ вставляет в чат с AI-ассистентом и получает разбор.

import { api } from '../core/api.js';
import { toast, showConfirm, showPrompt } from '../core/dom.js';

const STATUS_COLOR = {
    500: '#ef4444', 422: '#f59e0b', 400: '#f59e0b', 409: '#f59e0b',
    404: '#6b7280', 403: '#dc2626', 401: '#dc2626',
};
const SOURCE_LABEL = {
    backend: { label: '🟦 Backend', color: '#3b82f6' },
    celery:  { label: '🟪 Celery',  color: '#8b5cf6' },
    frontend:{ label: '🟧 Frontend',color: '#f97316' },
};

function fmtDateTime(iso) {
    if (!iso) return '—';
    try {
        const d = new Date(iso);
        return d.toLocaleString('ru-RU', {
            day: '2-digit', month: '2-digit', year: 'numeric',
            hour: '2-digit', minute: '2-digit', second: '2-digit',
        });
    } catch { return iso; }
}

function escapeHtml(s) {
    if (s === null || s === undefined) return '';
    return String(s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

export const ErrorsModule = {
    isInitialized: false,

    state: {
        page: 1, limit: 50, total: 0, items: [],
        filters: {
            source: '', http_status: '', resolved: 'false',
            since_hours: '24', path_contains: '', exc_type: '',
        },
        selectedId: null,
        currentDetail: null,  // полный объект последней открытой ошибки
    },

    dom: {},

    _autoRefreshId: null,

    init() {
        if (!this.isInitialized) {
            this._cacheDom();
            this._bindEvents();
            this.isInitialized = true;
        }
        this.refreshAll();
        this._startAutoRefresh();
    },

    _cacheDom() {
        this.dom = {
            kpi:         document.getElementById('errorsKpi'),
            tbody:       document.getElementById('errorsTableBody'),
            pagePrev:    document.getElementById('errorsPagePrev'),
            pageNext:    document.getElementById('errorsPageNext'),
            pageLabel:   document.getElementById('errorsPageLabel'),
            pageTotal:   document.getElementById('errorsPageTotal'),
            btnRefresh:  document.getElementById('errorsRefresh'),
            fSource:     document.getElementById('errorsFilterSource'),
            fStatus:     document.getElementById('errorsFilterStatus'),
            fResolved:   document.getElementById('errorsFilterResolved'),
            fSince:      document.getElementById('errorsFilterSince'),
            fPath:       document.getElementById('errorsFilterPath'),
            fExcType:    document.getElementById('errorsFilterExcType'),

            modal:           document.getElementById('errorDetailModal'),
            modalTitle:      document.getElementById('errorDetailTitle'),
            modalMeta:       document.getElementById('errorDetailMeta'),
            modalExc:        document.getElementById('errorDetailExc'),
            modalTraceback:  document.getElementById('errorDetailTraceback'),
            modalInvestigation: document.getElementById('errorDetailInvestigation'),
            modalRequestBody:document.getElementById('errorDetailRequestBody'),
            modalExtra:      document.getElementById('errorDetailExtra'),
            btnCopy:         document.getElementById('errorCopyClaude'),
            btnResolve:      document.getElementById('errorResolve'),
            btnReopen:       document.getElementById('errorReopen'),
            btnDelete:       document.getElementById('errorDelete'),

            badge: document.getElementById('errorsBadge'),
        };
    },

    _bindEvents() {
        this.dom.btnRefresh?.addEventListener('click', () => this.refreshAll());
        this.dom.pagePrev?.addEventListener('click', () => this._gotoPage(this.state.page - 1));
        this.dom.pageNext?.addEventListener('click', () => this._gotoPage(this.state.page + 1));

        // фильтры
        [this.dom.fSource, this.dom.fStatus, this.dom.fResolved, this.dom.fSince].forEach(el => {
            el?.addEventListener('change', () => {
                this._readFilters();
                this.state.page = 1;
                this.loadList();
            });
        });
        let typingTimer = null;
        [this.dom.fPath, this.dom.fExcType].forEach(el => {
            el?.addEventListener('input', () => {
                clearTimeout(typingTimer);
                typingTimer = setTimeout(() => {
                    this._readFilters();
                    this.state.page = 1;
                    this.loadList();
                }, 350);
            });
        });

        // клик по строке
        this.dom.tbody?.addEventListener('click', (e) => {
            const tr = e.target.closest('tr[data-error-id]');
            if (!tr) return;
            this.openDetails(Number(tr.dataset.errorId));
        });

        // действия в модалке
        this.dom.modal?.addEventListener('click', (e) => {
            const close = e.target.closest('[data-action="error-close"]') || e.target === this.dom.modal;
            if (close) {
                this.dom.modal.classList.remove('open');
                this.state.currentDetail = null;
            }
        });
        this.dom.btnCopy?.addEventListener('click', () => this.copyToClaude());
        this.dom.btnResolve?.addEventListener('click', () => this.markResolved());
        this.dom.btnReopen?.addEventListener('click', () => this.reopen());
        this.dom.btnDelete?.addEventListener('click', () => this.deleteCurrent());

        // Esc закрывает модалку (удобство).
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && this.dom.modal?.classList.contains('open')) {
                this.dom.modal.classList.remove('open');
                this.state.currentDetail = null;
            }
        });
    },

    _startAutoRefresh() {
        this._stopAutoRefresh();
        this._autoRefreshId = setInterval(() => {
            if (!document.hidden) this.loadStats();  // тихо обновляем счётчики
        }, 60000);
    },
    _stopAutoRefresh() {
        if (this._autoRefreshId) { clearInterval(this._autoRefreshId); this._autoRefreshId = null; }
    },

    _readFilters() {
        const f = this.state.filters;
        f.source       = this.dom.fSource?.value || '';
        f.http_status  = this.dom.fStatus?.value || '';
        f.resolved     = this.dom.fResolved?.value;  // '' / 'true' / 'false'
        f.since_hours  = this.dom.fSince?.value || '';
        f.path_contains = (this.dom.fPath?.value || '').trim();
        f.exc_type     = (this.dom.fExcType?.value || '').trim();
    },

    async refreshAll() {
        this._readFilters();
        await Promise.all([this.loadStats(), this.loadList()]);
    },

    async loadStats() {
        try {
            const s = await api.get('/admin/errors/stats');
            this._renderKpi(s);
            this._updateBadge(s.total_unresolved || 0);
        } catch (e) {
            console.warn('[errors] stats failed:', e?.message);
        }
    },

    _renderKpi(s) {
        const cards = [
            { label: 'Активных (не решено)', value: s.total_unresolved || 0,
              color: (s.total_unresolved || 0) > 0 ? '#ef4444' : '#10b981' },
            { label: 'За 24 часа', value: s.last_24h || 0, color: '#3b82f6' },
            { label: '🟦 Backend', value: s.by_source_unresolved?.backend || 0, color: '#3b82f6' },
            { label: '🟪 Celery',  value: s.by_source_unresolved?.celery  || 0, color: '#8b5cf6' },
            { label: '🟧 Frontend',value: s.by_source_unresolved?.frontend|| 0, color: '#f97316' },
        ];
        this.dom.kpi.innerHTML = cards.map(c => `
            <div style="background:var(--bg-card); border:1px solid var(--border-color); border-radius:8px; padding:12px;">
                <div style="color:var(--text-secondary); font-size:11px; text-transform:uppercase;">${escapeHtml(c.label)}</div>
                <div style="font-size:22px; font-weight:700; color:${c.color}; margin-top:4px;">${c.value}</div>
            </div>`).join('');
    },

    _updateBadge(n) {
        if (!this.dom.badge) return;
        if (n > 0) {
            this.dom.badge.textContent = n > 99 ? '99+' : String(n);
            this.dom.badge.style.display = 'inline-block';
        } else {
            this.dom.badge.style.display = 'none';
        }
    },

    async loadList() {
        const params = new URLSearchParams({
            page: this.state.page,
            limit: this.state.limit,
        });
        const f = this.state.filters;
        if (f.source)       params.set('source', f.source);
        if (f.http_status)  params.set('http_status', f.http_status);
        if (f.resolved !== '') params.set('resolved', f.resolved);
        if (f.since_hours)  params.set('since_hours', f.since_hours);
        if (f.path_contains)params.set('path_contains', f.path_contains);
        if (f.exc_type)     params.set('exc_type', f.exc_type);
        try {
            const data = await api.get(`/admin/errors?${params.toString()}`);
            this.state.items = data.items || [];
            this.state.total = data.total || 0;
            this._renderTable();
            this._renderPagination();
        } catch (e) {
            this.dom.tbody.innerHTML = `<tr><td colspan="7" style="padding:20px; text-align:center; color:var(--danger-color);">Не удалось загрузить: ${escapeHtml(e.message)}</td></tr>`;
        }
    },

    _renderTable() {
        if (!this.state.items.length) {
            this.dom.tbody.innerHTML = `<tr><td colspan="7" style="padding:40px; text-align:center; color:var(--text-secondary);">
                🎉 Нет ошибок по выбранным фильтрам
            </td></tr>`;
            return;
        }
        this.dom.tbody.innerHTML = this.state.items.map(r => {
            const src = SOURCE_LABEL[r.source] || { label: r.source, color: '#6b7280' };
            const statusColor = STATUS_COLOR[r.http_status] || '#6b7280';
            const resolvedTag = r.resolved
                ? '<span style="background:#d1fae5; color:#065f46; padding:1px 6px; border-radius:8px; font-size:10px;">решено</span>'
                : '';
            return `
                <tr data-error-id="${r.id}" style="border-top:1px solid var(--border-color); cursor:pointer;"
                    onmouseover="this.style.background='var(--bg-page)'"
                    onmouseout="this.style.background='transparent'">
                    <td style="padding:8px 10px; white-space:nowrap; font-size:12px;">
                        ${escapeHtml(fmtDateTime(r.occurred_at))} ${resolvedTag}
                    </td>
                    <td style="padding:8px 10px; font-size:12px; color:${src.color};">${escapeHtml(src.label)}</td>
                    <td style="padding:8px 10px;">
                        ${r.http_status ? `<span style="background:${statusColor}22; color:${statusColor}; padding:2px 8px; border-radius:10px; font-size:11px; font-weight:600;">${r.http_status}</span>` : '—'}
                    </td>
                    <td style="padding:8px 10px; font-weight:500; font-size:12px;">
                        ${escapeHtml(r.exc_type || '—')}
                        <div style="color:var(--text-secondary); font-size:11px; max-width:400px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">
                            ${escapeHtml(r.exc_message_short || '')}
                        </div>
                    </td>
                    <td style="padding:8px 10px; font-family:monospace; font-size:11px; max-width:280px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">
                        ${r.http_method ? `<span style="color:var(--text-secondary);">${escapeHtml(r.http_method)}</span> ` : ''}
                        ${escapeHtml(r.http_path || '—')}
                    </td>
                    <td style="padding:8px 10px; font-size:12px;">
                        ${escapeHtml(r.user_username || '—')}
                    </td>
                    <td style="padding:8px 10px; text-align:right; font-size:11px; color:var(--text-tertiary);">
                        ${r.copied_count > 0 ? `📋 ${r.copied_count}` : ''}
                    </td>
                </tr>`;
        }).join('');
    },

    _renderPagination() {
        const max = Math.ceil(this.state.total / this.state.limit) || 1;
        this.dom.pageLabel.textContent = `${this.state.page} / ${max}`;
        this.dom.pageTotal.textContent = `Всего: ${this.state.total}`;
        this.dom.pagePrev.disabled = this.state.page <= 1;
        this.dom.pageNext.disabled = this.state.page >= max;
    },

    _gotoPage(p) {
        const max = Math.ceil(this.state.total / this.state.limit) || 1;
        if (p < 1 || p > max) return;
        this.state.page = p;
        this.loadList();
    },

    // =============== DETAILS MODAL ===============
    async openDetails(id) {
        this.state.selectedId = id;
        try {
            const r = await api.get(`/admin/errors/${id}`);
            this.state.currentDetail = r;
            this._renderDetail(r);
            this.dom.modal.classList.add('open');
        } catch (e) {
            toast('Не удалось загрузить детали: ' + e.message, 'error');
        }
    },

    _renderDetail(r) {
        const src = SOURCE_LABEL[r.source] || { label: r.source };
        this.dom.modalTitle.textContent = `Ошибка #${r.id} — ${src.label}`;

        this.dom.modalMeta.innerHTML = [
            ['Произошла', fmtDateTime(r.occurred_at)],
            ['Источник', src.label],
            ['Уровень', r.level],
            ['HTTP', `${r.http_method || '—'} ${r.http_status || ''}`],
            ['Путь', r.http_path || '—'],
            ['Пользователь', r.user_username || '—'],
            ['Request-ID', r.request_id || '—'],
            ['Решено', r.resolved ? '✅ да' : '⏳ нет'],
            ['Копировали', String(r.copied_count || 0)],
        ].map(([k, v]) => `
            <div>
                <div style="color:var(--text-secondary); font-size:10px; text-transform:uppercase;">${escapeHtml(k)}</div>
                <div style="font-weight:500; font-size:12px; word-break:break-all;">${escapeHtml(v)}</div>
            </div>`).join('');

        this.dom.modalExc.textContent = `${r.exc_type || 'Unknown'}: ${r.exc_message || ''}`;
        this.dom.modalTraceback.textContent = r.traceback || '(нет traceback)';
        this.dom.modalInvestigation.textContent = r.investigation
            ? JSON.stringify(r.investigation, null, 2)
            : '(auto-investigation не выполнено)';

        this.dom.modalRequestBody.textContent = r.request_body
            ? JSON.stringify(r.request_body, null, 2)
            : '(тело запроса пустое)';
        this.dom.modalExtra.textContent = r.extra
            ? JSON.stringify(r.extra, null, 2)
            : '(нет доп. метаданных)';

        // Кнопки resolve/reopen — показываем актуальную.
        this.dom.btnResolve.style.display = r.resolved ? 'none' : '';
        this.dom.btnReopen.style.display  = r.resolved ? '' : 'none';
    },

    async copyToClaude() {
        if (!this.state.selectedId) return;
        try {
            const res = await api.get(`/admin/errors/${this.state.selectedId}/copy`);
            await navigator.clipboard.writeText(res.markdown);
            toast(`Скопировано! (вы скопировали эту ошибку ${res.copied_count} раз).
Теперь вставь в чат с Claude.`, 'success');
        } catch (e) {
            toast('Не удалось скопировать: ' + e.message, 'error');
        }
    },

    async markResolved() {
        if (!this.state.selectedId) return;
        const notes = await showPrompt('Заметка', 'Заметка (необязательно — что было сделано):', '');
        try {
            await api.post(`/admin/errors/${this.state.selectedId}/resolve`,
                notes ? { notes } : {});
            toast('Помечено как решённое', 'success');
            this.dom.modal.classList.remove('open');
            this.refreshAll();
        } catch (e) {
            toast('Не удалось: ' + e.message, 'error');
        }
    },

    async reopen() {
        if (!this.state.selectedId) return;
        try {
            await api.post(`/admin/errors/${this.state.selectedId}/reopen`, {});
            toast('Открыто заново', 'info');
            this.refreshAll();
            this.openDetails(this.state.selectedId);
        } catch (e) {
            toast('Не удалось: ' + e.message, 'error');
        }
    },

    async deleteCurrent() {
        if (!this.state.selectedId) return;
        if (!await showConfirm('Удалить эту запись об ошибке? Действие необратимо.', { danger: true, confirmText: 'Удалить' })) return;
        try {
            await api.del(`/admin/errors/${this.state.selectedId}`);
            toast('Удалено', 'success');
            this.dom.modal.classList.remove('open');
            this.refreshAll();
        } catch (e) {
            // Fallback если api.del нет — DELETE через fetch.
            try {
                const resp = await fetch(`/api/admin/errors/${this.state.selectedId}`, {
                    method: 'DELETE',
                    credentials: 'include',
                });
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                toast('Удалено', 'success');
                this.dom.modal.classList.remove('open');
                this.refreshAll();
            } catch (e2) {
                toast('Не удалось удалить: ' + e2.message, 'error');
            }
        }
    },
};
