// static/js/modules/admin-certificates.js
//
// Админская вкладка «Справки» (волна 3 фичи заказа справок).
// Что умеет:
//   * Список заявок с фильтрами (статус, тип, поиск, пагинация).
//   * KPI-плашки: сколько в обработке / готово / выдано / отклонено.
//   * Клик на заявку → модалка с деталями: данные жильца, семья, договор,
//     содержимое заявки (период / куда).
//   * В модалке можно редактировать поля заявки, профиль жильца и семью,
//     перегенерировать PDF, сменить статус («выдано» / «отклонить»),
//     скачать PDF, удалить заявку (только admin).

import { api } from '../core/api.js';
import { toast } from '../core/dom.js';
import { TableController } from '../core/table-controller.js';

function esc(s) {
    if (s == null) return '';
    const d = document.createElement('div');
    d.textContent = String(s);
    return d.innerHTML;
}
function fmtDate(iso) {
    if (!iso) return '—';
    try { return new Date(iso).toLocaleDateString('ru-RU'); } catch { return iso; }
}
function fmtDateTime(iso) {
    if (!iso) return '—';
    try { return new Date(iso).toLocaleString('ru-RU'); } catch { return iso; }
}

const TYPE_LABEL = { flc: 'Выписка из ФЛС' };
const STATUS_META = {
    pending:   { label: 'В обработке', color: '#f59e0b', bg: '#fef3c7' },
    generated: { label: 'Готова',      color: '#10b981', bg: '#d1fae5' },
    delivered: { label: 'Выдана',      color: '#3b82f6', bg: '#dbeafe' },
    rejected:  { label: 'Отклонена',   color: '#ef4444', bg: '#fee2e2' },
};
const ROLE_LABEL = { spouse: 'Супруг(а)', child: 'Ребёнок', parent: 'Родитель', other: 'Член семьи' };

export const AdminCertificatesModule = {
    isInitialized: false,
    currentCert: null,   // открытая деталь
    _statsReqId: 0,      // race guard для KPI

    init() {
        this.cacheDOM();
        if (!this.dom.tableBody) return;  // компонент не смонтирован

        if (!this.isInitialized) {
            this.bindEvents();
            this.isInitialized = true;
        }
        this.loadStats();
        this.initTable();
    },

    cacheDOM() {
        this.dom = {
            stats: document.getElementById('certsStats'),
            tableBody: document.getElementById('certsTableBody'),
            search: document.getElementById('certsSearch'),
            filterStatus: document.getElementById('certsFilterStatus'),
            filterType: document.getElementById('certsFilterType'),
            btnRefresh: document.getElementById('btnRefreshCerts'),
            btnPrev: document.getElementById('btnCertsPrev'),
            btnNext: document.getElementById('btnCertsNext'),
            pageInfo: document.getElementById('certsPageInfo'),
            modal: document.getElementById('certDetailModal'),
            modalTitle: document.getElementById('certModalTitle'),
            modalBody: document.getElementById('certModalBody'),
            modalFooter: document.getElementById('certModalFooter'),
        };
    },

    bindEvents() {
        this.dom.btnRefresh?.addEventListener('click', () => this.refreshAll());

        const refilter = () => {
            if (!this.table) return;
            this.table.state.page = 1;
            this.table.load();
            this.loadStats();
        };
        this.dom.filterStatus?.addEventListener('change', refilter);
        this.dom.filterType?.addEventListener('change', refilter);

        // Поиск — собственный debounce, т.к. поле с id "certsSearch",
        // а TableController по умолчанию привязывается к своему searchInput.
        let t;
        this.dom.search?.addEventListener('input', e => {
            clearTimeout(t);
            t = setTimeout(() => {
                if (!this.table) return;
                this.table.state.search = e.target.value || '';
                this.table.state.page = 1;
                this.table.load();
            }, 400);
        });

        // Клик по строке → открыть деталь
        this.dom.tableBody?.addEventListener('click', (e) => {
            const row = e.target.closest('tr[data-cert-id]');
            const actionBtn = e.target.closest('button[data-cert-quick-action]');
            if (actionBtn) {
                e.stopPropagation();
                const id = Number(actionBtn.dataset.certId);
                const action = actionBtn.dataset.certQuickAction;
                if (action === 'download') this.downloadCert(id);
                return;
            }
            if (row) this.openDetail(Number(row.dataset.certId));
        });

        // Модалка — закрытие
        this.dom.modal?.addEventListener('click', (e) => {
            if (e.target.closest('[data-cert-close]') || e.target === this.dom.modal) {
                this.dom.modal.classList.remove('open');
                this.currentCert = null;
            }
        });
    },

    refreshAll() {
        this.table?.refresh();
        this.loadStats();
    },

    // =====================================================
    // KPI
    // =====================================================
    async loadStats() {
        if (!this.dom.stats) return;
        const myId = ++this._statsReqId;
        try {
            const s = await api.get('/admin/certificates/stats');
            if (myId !== this._statsReqId) return;
            this.renderStats(s);
        } catch (e) {
            if (myId !== this._statsReqId) return;
            this.dom.stats.innerHTML =
                `<div style="padding:10px; color:var(--danger-color); grid-column:1/-1;">Ошибка: ${esc(e.message)}</div>`;
        }
    },

    renderStats(s) {
        const card = (bg, color, icon, value, label) => `
            <div style="background:${bg}; border:1px solid ${color}33; border-radius:10px; padding:12px;">
                <div style="display:flex; align-items:center; gap:6px; color:${color}; font-size:11px; text-transform:uppercase; letter-spacing:.3px; margin-bottom:3px;">
                    <span>${icon}</span>${label}
                </div>
                <div style="font-size:20px; font-weight:700; color:#111827;">${value}</div>
            </div>
        `;
        this.dom.stats.innerHTML = [
            card('#eff6ff', '#2563eb', '📋', s.total, 'Всего'),
            card('#fef3c7', '#f59e0b', '⏳', s.pending, 'В обработке'),
            card('#ecfdf5', '#10b981', '✅', s.generated, 'Готовых'),
            card('#dbeafe', '#3b82f6', '📦', s.delivered, 'Выданы'),
            card('#fef2f2', '#ef4444', '🚫', s.rejected, 'Отклонены'),
        ].join('');
    },

    // =====================================================
    // TABLE
    // =====================================================
    initTable() {
        this.table = new TableController({
            endpoint: '/admin/certificates',
            dom: {
                tableBody: 'certsTableBody',
                prevBtn: 'btnCertsPrev',
                nextBtn: 'btnCertsNext',
                pageInfo: 'certsPageInfo',
            },
            getExtraParams: () => {
                const p = {};
                if (this.dom.filterStatus?.value) p.status = this.dom.filterStatus.value;
                if (this.dom.filterType?.value) p.type = this.dom.filterType.value;
                return p;
            },
            renderRow: (c) => this.renderRow(c),
        });
        this.table.init();
    },

    renderRow(c) {
        const meta = STATUS_META[c.status] || { label: c.status, color: '#6b7280', bg: '#f3f4f6' };
        const tr = document.createElement('tr');
        tr.dataset.certId = String(c.id);
        tr.style.cursor = 'pointer';
        tr.innerHTML = `
            <td style="font-family:monospace; color:var(--text-secondary);">#${c.id}</td>
            <td>
                <div style="font-weight:600;">${esc(TYPE_LABEL[c.type] || c.type)}</div>
                <div style="font-size:11px; color:var(--text-secondary);">${esc(fmtDateTime(c.created_at))}</div>
            </td>
            <td>
                <div style="font-weight:500;">${esc(c.full_name || c.username)}</div>
                ${c.full_name ? `<div style="font-size:11px; color:var(--text-secondary);">${esc(c.username)}</div>` : ''}
            </td>
            <td style="font-size:12px; color:var(--text-secondary);">
                ${esc(c.dormitory || '—')}${c.room_number ? ' / ' + esc(c.room_number) : ''}
            </td>
            <td style="font-size:13px; max-width:220px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${esc(c.data?.purpose || '')}">
                ${esc(c.data?.purpose || '—')}
            </td>
            <td>
                <span style="background:${meta.bg}; color:${meta.color}; padding:3px 10px; border-radius:12px; font-size:11px; font-weight:600; white-space:nowrap;">
                    ${esc(meta.label)}
                </span>
            </td>
            <td class="text-right" style="white-space:nowrap;">
                ${c.has_pdf ? `
                    <button class="icon-btn" data-cert-quick-action="download" data-cert-id="${c.id}" title="Скачать PDF">
                        <i class="fa-solid fa-download"></i>
                    </button>
                ` : ''}
            </td>
        `;
        return tr;
    },

    // =====================================================
    // DETAIL MODAL
    // =====================================================
    async openDetail(certId) {
        this.dom.modal.classList.add('open');
        this.dom.modalTitle.textContent = `Заявка №${certId}`;
        this.dom.modalBody.innerHTML = `
            <div style="padding:40px; text-align:center; color:var(--text-secondary);">
                <i class="fa-solid fa-spinner fa-spin"></i> Загрузка…
            </div>`;
        this.dom.modalFooter.innerHTML = `<button class="action-btn secondary-btn" data-cert-close>Закрыть</button>`;

        try {
            this.currentCert = await api.get(`/admin/certificates/${certId}`);
            this.renderDetail();
        } catch (e) {
            this.dom.modalBody.innerHTML =
                `<div style="padding:24px; color:var(--danger-color);">Ошибка: ${esc(e.message)}</div>`;
        }
    },

    renderDetail() {
        const c = this.currentCert;
        const meta = STATUS_META[c.status] || { label: c.status, color: '#6b7280', bg: '#f3f4f6' };
        const u = c.user;
        const d = c.data || {};

        const familyHtml = c.family.length ? `
            <ul style="margin:0; padding:0; list-style:none;">
                ${c.family.map(m => `
                    <li style="padding:6px 10px; background:white; border:1px solid var(--border-color); border-radius:6px; margin-bottom:4px; display:flex; justify-content:space-between; align-items:center; gap:10px;">
                        <span>
                            <b>${esc(m.full_name)}</b>
                            <span style="color:var(--text-secondary); font-size:12px; margin-left:6px;">
                                ${esc(ROLE_LABEL[m.role] || m.role)}${m.birth_date ? ' · ' + esc(fmtDate(m.birth_date)) + ' г.р.' : ''}
                            </span>
                        </span>
                        <span style="display:flex; gap:4px;">
                            <button class="icon-btn" data-family-edit="${m.id}" title="Редактировать"><i class="fa-solid fa-pen"></i></button>
                            <button class="icon-btn" data-family-delete="${m.id}" title="Удалить" style="color:var(--danger-color);"><i class="fa-solid fa-trash"></i></button>
                        </span>
                    </li>
                `).join('')}
            </ul>
        ` : `<div style="color:var(--text-secondary); font-style:italic;">Членов семьи нет</div>`;

        const contractBlock = c.contract ? `
            <div style="padding:10px 12px; background:var(--bg-page); border-radius:6px; font-size:13px;">
                <b>№ ${esc(c.contract.number || '—')}</b>
                · от ${esc(fmtDate(c.contract.signed_date))}
                ${c.contract.valid_until ? ' · до ' + esc(fmtDate(c.contract.valid_until)) : ''}
            </div>
        ` : `<div style="color:var(--warning-color); font-style:italic; font-size:13px;">⚠ Договор не привязан</div>`;

        this.dom.modalBody.innerHTML = `
            <!-- Статус + тип -->
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:14px; flex-wrap:wrap; gap:10px;">
                <div>
                    <span style="background:${meta.bg}; color:${meta.color}; padding:4px 12px; border-radius:14px; font-size:12px; font-weight:600;">${esc(meta.label)}</span>
                    <span style="margin-left:10px; font-size:13px; color:var(--text-secondary);">
                        ${esc(TYPE_LABEL[c.type] || c.type)} · создана ${esc(fmtDateTime(c.created_at))}
                        ${c.processed_by_username ? ' · обработал: ' + esc(c.processed_by_username) : ''}
                    </span>
                </div>
            </div>

            <!-- Данные жильца -->
            <h4 style="margin:0 0 10px;"><i class="fa-solid fa-user" style="color:#2563eb;"></i> Жилец</h4>
            <form id="adminUserProfileForm" style="display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:16px;">
                <div class="form-group" style="margin:0;">
                    <label style="font-size:11px;">ФИО</label>
                    <input type="text" name="full_name" value="${esc(u.full_name || '')}" placeholder="${esc(u.username)}">
                </div>
                <div class="form-group" style="margin:0;">
                    <label style="font-size:11px;">Должность</label>
                    <input type="text" name="position" value="${esc(u.position || '')}">
                </div>
                <div class="form-group" style="margin:0;">
                    <label style="font-size:11px;">Паспорт серия</label>
                    <input type="text" name="passport_series" value="${esc(u.passport_series || '')}">
                </div>
                <div class="form-group" style="margin:0;">
                    <label style="font-size:11px;">Паспорт номер</label>
                    <input type="text" name="passport_number" value="${esc(u.passport_number || '')}">
                </div>
                <div class="form-group" style="margin:0; grid-column:1/-1;">
                    <label style="font-size:11px;">Кем выдан</label>
                    <input type="text" name="passport_issued_by" value="${esc(u.passport_issued_by || '')}">
                </div>
                <div class="form-group" style="margin:0;">
                    <label style="font-size:11px;">Дата выдачи</label>
                    <input type="date" name="passport_issued_at" value="${esc(u.passport_issued_at || '')}">
                </div>
                <div class="form-group" style="margin:0;">
                    <label style="font-size:11px;">Дата регистрации</label>
                    <input type="date" name="registration_date" value="${esc(u.registration_date || '')}">
                </div>
                <div style="grid-column:1/-1; display:flex; justify-content:flex-end;">
                    <button type="submit" class="action-btn secondary-btn" style="padding:4px 12px; font-size:12px;">
                        <i class="fa-solid fa-save"></i> Сохранить данные жильца
                    </button>
                </div>
            </form>

            <!-- Семья -->
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
                <h4 style="margin:0;"><i class="fa-solid fa-people-group" style="color:#10b981;"></i> Семья</h4>
                <button id="adminAddFamilyBtn" class="action-btn secondary-btn" style="padding:4px 10px; font-size:12px;">
                    <i class="fa-solid fa-plus"></i> Добавить
                </button>
            </div>
            <div id="adminFamilyList" style="margin-bottom:16px;">${familyHtml}</div>

            <!-- Договор -->
            <h4 style="margin:0 0 10px;"><i class="fa-solid fa-file-signature" style="color:#7c3aed;"></i> Договор найма</h4>
            <div style="margin-bottom:16px;">${contractBlock}</div>

            <!-- Данные заявки -->
            <h4 style="margin:0 0 10px;"><i class="fa-solid fa-clipboard-list" style="color:#ea580c;"></i> Поля заявки</h4>
            <form id="adminCertDataForm" style="display:grid; grid-template-columns:1fr 1fr; gap:10px;">
                <div class="form-group" style="margin:0;">
                    <label style="font-size:11px;">Период с</label>
                    <input type="date" name="period_from" value="${esc(d.period_from || '')}">
                </div>
                <div class="form-group" style="margin:0;">
                    <label style="font-size:11px;">по</label>
                    <input type="date" name="period_to" value="${esc(d.period_to || '')}">
                </div>
                <div class="form-group" style="margin:0; grid-column:1/-1;">
                    <label style="font-size:11px;">Куда предоставить</label>
                    <input type="text" name="purpose" value="${esc(d.purpose || '')}" required>
                </div>
                <div class="form-group" style="margin:0; grid-column:1/-1;">
                    <label style="font-size:11px;">Комментарий (внутренний)</label>
                    <textarea name="note" rows="2">${esc(c.note || '')}</textarea>
                </div>
            </form>
        `;

        // Footer — кнопки действий
        this.dom.modalFooter.innerHTML = `
            <button class="action-btn danger-btn" id="btnCertReject" style="padding:6px 14px; font-size:13px;">
                <i class="fa-solid fa-ban"></i> Отклонить
            </button>
            <button class="action-btn secondary-btn" id="btnCertSave" style="padding:6px 14px; font-size:13px;">
                <i class="fa-solid fa-save"></i> Сохранить поля
            </button>
            <button class="action-btn warning-btn" id="btnCertRegenerate" style="padding:6px 14px; font-size:13px;">
                <i class="fa-solid fa-rotate"></i> Перегенерировать PDF
            </button>
            ${c.has_pdf ? `
                <button class="action-btn secondary-btn" id="btnCertDownload" style="padding:6px 14px; font-size:13px;">
                    <i class="fa-solid fa-download"></i> Скачать
                </button>
            ` : ''}
            <button class="action-btn success-btn" id="btnCertDeliver" style="padding:6px 14px; font-size:13px;"
                    ${c.status === 'delivered' ? 'disabled style="opacity:0.5;"' : ''}>
                <i class="fa-solid fa-check-double"></i> Отметить выданной
            </button>
            <button class="action-btn secondary-btn" data-cert-close>Закрыть</button>
        `;

        // Привязка событий
        this._bindDetailEvents();
    },

    _bindDetailEvents() {
        const cert = this.currentCert;

        // Форма профиля жильца
        document.getElementById('adminUserProfileForm')?.addEventListener('submit', async (e) => {
            e.preventDefault();
            const fd = new FormData(e.target);
            const payload = {};
            for (const [k, v] of fd.entries()) payload[k] = v || null;
            try {
                await api.patch(`/admin/users/${cert.user.id}/profile`, payload);
                toast('Профиль жильца обновлён', 'success');
            } catch (err) {
                toast('Ошибка: ' + err.message, 'error');
            }
        });

        // Добавить члена семьи
        document.getElementById('adminAddFamilyBtn')?.addEventListener('click', () => this._promptAddFamily());

        // Правка/удаление семьи
        document.getElementById('adminFamilyList')?.addEventListener('click', (e) => {
            const edit = e.target.closest('[data-family-edit]');
            const del = e.target.closest('[data-family-delete]');
            if (edit) {
                const m = cert.family.find(x => x.id === Number(edit.dataset.familyEdit));
                if (m) this._promptEditFamily(m);
            }
            if (del) this._deleteFamilyMember(Number(del.dataset.familyDelete));
        });

        // Сохранить поля заявки
        document.getElementById('btnCertSave')?.addEventListener('click', async () => {
            const form = document.getElementById('adminCertDataForm');
            const fd = new FormData(form);
            const payload = {};
            for (const [k, v] of fd.entries()) payload[k] = v || null;
            try {
                await api.patch(`/admin/certificates/${cert.id}`, payload);
                toast('Поля заявки сохранены', 'success');
                await this.openDetail(cert.id);
            } catch (err) {
                toast('Ошибка: ' + err.message, 'error');
            }
        });

        // Перегенерировать PDF
        document.getElementById('btnCertRegenerate')?.addEventListener('click', async () => {
            if (!confirm('Перегенерировать PDF с актуальными данными?')) return;
            const btn = document.getElementById('btnCertRegenerate');
            const orig = btn.innerHTML;
            btn.disabled = true;
            btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Генерация…';
            try {
                await api.post(`/admin/certificates/${cert.id}/regenerate`);
                toast('PDF готов', 'success');
                await this.openDetail(cert.id);
                this.refreshAll();
            } catch (err) {
                toast('Ошибка: ' + err.message, 'error');
                btn.disabled = false;
                btn.innerHTML = orig;
            }
        });

        // Скачать
        document.getElementById('btnCertDownload')?.addEventListener('click', () => this.downloadCert(cert.id));

        // Отметить выданной
        document.getElementById('btnCertDeliver')?.addEventListener('click', async () => {
            try {
                await api.patch(`/admin/certificates/${cert.id}`, { status: 'delivered' });
                toast('Отмечено: выдано', 'success');
                await this.openDetail(cert.id);
                this.refreshAll();
            } catch (err) {
                toast('Ошибка: ' + err.message, 'error');
            }
        });

        // Отклонить
        document.getElementById('btnCertReject')?.addEventListener('click', async () => {
            const reason = prompt('Причина отклонения (попадёт в комментарий заявки):');
            if (!reason) return;
            try {
                await api.patch(`/admin/certificates/${cert.id}`, {
                    status: 'rejected',
                    note: reason,
                });
                toast('Заявка отклонена', 'info');
                await this.openDetail(cert.id);
                this.refreshAll();
            } catch (err) {
                toast('Ошибка: ' + err.message, 'error');
            }
        });
    },

    async _promptAddFamily() {
        const cert = this.currentCert;
        const name = prompt('ФИО члена семьи:');
        if (!name || name.trim().length < 2) return;
        const role = prompt('Отношение (spouse/child/parent/other):', 'child');
        if (!role) return;
        const birth = prompt('Дата рождения (ГГГГ-ММ-ДД) — можно пусто:', '');
        try {
            await api.post(`/admin/users/${cert.user.id}/family`, {
                role: role.trim(),
                full_name: name.trim(),
                birth_date: birth && birth.match(/^\d{4}-\d{2}-\d{2}$/) ? birth : null,
            });
            toast('Добавлено', 'success');
            await this.openDetail(cert.id);
        } catch (err) {
            toast('Ошибка: ' + err.message, 'error');
        }
    },

    async _promptEditFamily(member) {
        const cert = this.currentCert;
        const name = prompt('ФИО:', member.full_name);
        if (!name) return;
        const birth = prompt('Дата рождения (ГГГГ-ММ-ДД) — можно пусто:', member.birth_date || '');
        try {
            await api.put(`/admin/users/${cert.user.id}/family/${member.id}`, {
                role: member.role,
                full_name: name.trim(),
                birth_date: birth && birth.match(/^\d{4}-\d{2}-\d{2}$/) ? birth : null,
            });
            toast('Обновлено', 'success');
            await this.openDetail(cert.id);
        } catch (err) {
            toast('Ошибка: ' + err.message, 'error');
        }
    },

    async _deleteFamilyMember(memberId) {
        const cert = this.currentCert;
        if (!confirm('Удалить члена семьи?')) return;
        try {
            await api.delete(`/admin/users/${cert.user.id}/family/${memberId}`);
            toast('Удалено', 'success');
            await this.openDetail(cert.id);
        } catch (err) {
            toast('Ошибка: ' + err.message, 'error');
        }
    },

    async downloadCert(certId) {
        try {
            await api.download(`/admin/certificates/${certId}/download`, `Zayavlenie_FLS_${certId}.pdf`);
        } catch (err) {
            toast('Ошибка скачивания: ' + err.message, 'error');
        }
    },
};
