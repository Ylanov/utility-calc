// static/js/modules/debts.js
//
// Вкладка «Долги 1С» — единая панель для финансиста:
//   * KPI-панель сверху (суммарный долг/переплата, должники, ср. долг, последний импорт)
//   * Импорт Excel с живым прогрессом задачи
//   * История импортов с откатом и просмотром «не найденных» ФИО
//   * Таблица с фильтрами (должники/переплаты/общежитие/мин. долг), сортировкой по колонкам,
//     цветовыми чипами уровней долга
//   * Модалка корректировки (замена prompt-цепочки): счёт, сумма, шаблон причины + комментарий
//   * Экспорт текущей выборки в Excel

import { api } from '../core/api.js';
import { el, toast, setLoading } from '../core/dom.js';

function esc(s) {
    if (s == null) return '';
    return String(s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function fmtMoney(v) {
    const n = Number(v || 0);
    return n.toLocaleString('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fmtDateTime(iso) {
    if (!iso) return '—';
    try { return new Date(iso).toLocaleString('ru-RU'); } catch { return iso; }
}

export const DebtsModule = {
    isInitialized: false,
    state: {
        page: 1, limit: 50, total: 0, search: '',
        filterType: '', dormitory: '', minDebt: '',
        sortBy: 'room', sortDir: 'asc',
        importTaskId: null, pollTimer: null, isUploading: false, lastRequestId: 0,
        currentPollId: null,
    },

    init() {
        this.cacheDOM();
        if (!this.isInitialized) {
            this.bindEvents();
            this.isInitialized = true;
        }
        // Регистрируем глобальный хэндлер — аналитик из «Центр анализа»
        // («Сверка 1С») может позвать модалку «не найдены» по log_id.
        // Живёт на window, чтобы не плодить event-bus.
        window.__openDebtsNotFound = (logId) => {
            const tabBtn = document.querySelector('.tab-btn[data-tab="debts"]');
            tabBtn?.click();
            setTimeout(() => this.openNotFoundModal(logId), 120);
        };
        this.loadStats();
        this.loadDormitories();
        this.loadUsers();
        this.loadImportHistory();
    },

    cacheDOM() {
        this.dom = {
            // Таблица
            tableBody: document.getElementById('debtsTableBody'),
            btnRefresh: document.getElementById('btnRefreshDebts'),
            btnExport: document.getElementById('btnExportDebts'),
            btnPrev: document.getElementById('btnPrevDebts'),
            btnNext: document.getElementById('btnNextDebts'),
            pageInfo: document.getElementById('debtsPageInfo'),
            searchInput: document.getElementById('debtsSearchInput'),
            // Фильтры
            filterType: document.getElementById('debtsFilterType'),
            filterDorm: document.getElementById('debtsFilterDormitory'),
            minDebt: document.getElementById('debtsMinDebt'),
            // Импорт
            btnUpload: document.getElementById('btnUploadDebts'),
            inputUpload: document.getElementById('debtFile1C'),
            uploadResult: document.getElementById('uploadResult'),
            // KPI
            stats: document.getElementById('debtsStats'),
            // История
            importHistoryList: document.getElementById('importHistoryList'),
            btnRefreshImportHistory: document.getElementById('btnRefreshImportHistory'),
            // Модалка корректировки
            adjustModal: document.getElementById('debtAdjustModal'),
            adjustForm: document.getElementById('debtAdjustForm'),
            adjustUserId: document.getElementById('adjustUserId'),
            adjustUserName: document.getElementById('adjustUserName'),
            adjustAccount: document.getElementById('adjustAccount'),
            adjustAmount: document.getElementById('adjustAmount'),
            adjustTemplate: document.getElementById('adjustTemplate'),
            adjustDescription: document.getElementById('adjustDescription'),
            // Модалка «не найдено»
            notFoundModal: document.getElementById('notFoundModal'),
            notFoundList: document.getElementById('notFoundList'),
            notFoundLogMeta: document.getElementById('notFoundLogMeta'),
        };
    },

    bindEvents() {
        this.dom.btnRefresh?.addEventListener('click', () => this.reload());
        this.dom.btnExport?.addEventListener('click', () => this.exportExcel());
        this.dom.btnUpload?.addEventListener('click', () => this.handleUpload());
        this.dom.btnPrev?.addEventListener('click', () => this.changePage(-1));
        this.dom.btnNext?.addEventListener('click', () => this.changePage(1));

        const refilter = () => {
            this.state.filterType = this.dom.filterType?.value || '';
            this.state.dormitory = this.dom.filterDorm?.value || '';
            this.state.minDebt = this.dom.minDebt?.value || '';
            this.state.page = 1;
            this.loadUsers();
        };
        this.dom.filterType?.addEventListener('change', refilter);
        this.dom.filterDorm?.addEventListener('change', refilter);

        let minDebtTimer;
        this.dom.minDebt?.addEventListener('input', () => {
            clearTimeout(minDebtTimer);
            minDebtTimer = setTimeout(refilter, 400);
        });

        let searchTimer;
        this.dom.searchInput?.addEventListener('input', (e) => {
            clearTimeout(searchTimer);
            searchTimer = setTimeout(() => {
                this.state.search = e.target.value || '';
                this.state.page = 1;
                this.loadUsers();
            }, 400);
        });

        // Сортировка по клику на заголовок
        document.querySelectorAll('[data-debt-sort]').forEach(th => {
            th.addEventListener('click', () => {
                const field = th.dataset.debtSort;
                if (this.state.sortBy === field) {
                    this.state.sortDir = this.state.sortDir === 'asc' ? 'desc' : 'asc';
                } else {
                    this.state.sortBy = field;
                    this.state.sortDir = field === 'debt' ? 'desc' : 'asc';
                }
                this.updateSortIcons();
                this.state.page = 1;
                this.loadUsers();
            });
        });

        // Модалка корректировки
        this.dom.adjustModal?.addEventListener('click', (e) => {
            if (e.target.closest('[data-adjust-close]')) this.closeAdjustModal();
        });
        this.dom.adjustForm?.addEventListener('submit', (e) => this.submitAdjust(e));
        this.dom.adjustTemplate?.addEventListener('change', (e) => {
            const val = e.target.value;
            if (val && this.dom.adjustDescription && !this.dom.adjustDescription.value.trim()) {
                this.dom.adjustDescription.value = val;
            }
        });

        // Модалка «не найдено»
        this.dom.notFoundModal?.addEventListener('click', (e) => {
            if (e.target.closest('[data-nf-close]')) this.closeNotFoundModal();
        });

        // История импортов
        this.dom.btnRefreshImportHistory?.addEventListener('click', () => this.loadImportHistory());
        this.dom.importHistoryList?.addEventListener('click', (e) => {
            const btn = e.target.closest('button[data-history-action]');
            if (!btn) return;
            const action = btn.dataset.historyAction;
            const logId = Number(btn.dataset.logId);
            if (action === 'view-not-found') this.openNotFoundModal(logId);
            else if (action === 'undo') this.undoImport(logId);
        });
    },

    reload() { this.state.page = 1; this.loadUsers(); this.loadStats(); },

    changePage(delta) {
        const newPage = this.state.page + delta;
        if (newPage < 1) return;
        this.state.page = newPage;
        this.loadUsers();
    },

    updateSortIcons() {
        document.querySelectorAll('[data-debt-sort]').forEach(th => {
            th.classList.remove('sort-asc', 'sort-desc');
            if (th.dataset.debtSort === this.state.sortBy) {
                th.classList.add(`sort-${this.state.sortDir}`);
            }
        });
    },

    clearPoll() {
        if (this.state.pollTimer) { clearTimeout(this.state.pollTimer); this.state.pollTimer = null; }
        this.state.currentPollId = null;
    },

    // ==========================================================================
    // KPI
    // ==========================================================================
    async loadStats() {
        if (!this.dom.stats) return;
        try {
            const s = await api.get('/financier/debts/stats');
            this.renderStats(s);
        } catch (e) {
            this.dom.stats.innerHTML = `<div style="padding:14px; color:var(--danger-color); grid-column:1/-1;">Ошибка аналитики: ${esc(e.message)}</div>`;
        }
    },

    renderStats(s) {
        const card = (bg, color, icon, value, label, hint) => `
            <div style="background:${bg}; border-radius:10px; padding:14px 12px; border:1px solid ${color}33;">
                <div style="display:flex; align-items:center; gap:8px; color:${color}; font-size:12px; margin-bottom:4px;">
                    <span style="font-size:16px;">${icon}</span>${esc(label)}
                </div>
                <div style="font-size:20px; font-weight:700; color:#111827;">${value}</div>
                ${hint ? `<div style="font-size:11px; color:var(--text-secondary); margin-top:2px;">${esc(hint)}</div>` : ''}
            </div>
        `;

        const lastImp = s.last_import;
        const lastHtml = lastImp
            ? `счёт ${lastImp.account_type}, ${fmtDateTime(lastImp.started_at)} · ${lastImp.started_by || '—'}`
            : 'импортов ещё не было';

        this.dom.stats.innerHTML = [
            card('#f5f3ff', '#7c3aed', '📅', s.period_name || '—', 'Активный период', `всего жильцов: ${s.total_users}`),
            card('#fef2f2', '#dc2626', '🔴', s.debtors_count, 'Должников', `средний долг: ${fmtMoney(s.avg_debt_per_debtor)} ₽`),
            card('#ecfdf5', '#10b981', '🟢', s.overpayers_count, 'С переплатами', `сумма: ${fmtMoney(s.total_overpay)} ₽`),
            card('#fff7ed', '#ea580c', '💰', `${fmtMoney(s.total_debt)} ₽`, 'Суммарный долг', `209: ${fmtMoney(s.total_debt_209)} · 205: ${fmtMoney(s.total_debt_205)}`),
            card('#eff6ff', '#2563eb', '📊', s.readings_count, 'Показаний в периоде', ''),
            card('#f9fafb', '#6b7280', '⏱️', lastImp ? lastImp.status : '—', 'Последний импорт', lastHtml),
        ].join('');
    },

    // ==========================================================================
    // Список общежитий в фильтр
    // ==========================================================================
    async loadDormitories() {
        if (!this.dom.filterDorm) return;
        try {
            const dorms = await api.get('/financier/debts/dormitories');
            const prev = this.dom.filterDorm.value;
            this.dom.filterDorm.innerHTML = '<option value="">Все общежития</option>';
            dorms.forEach(d => {
                const opt = document.createElement('option');
                opt.value = d;
                opt.textContent = d;
                this.dom.filterDorm.appendChild(opt);
            });
            if (prev) this.dom.filterDorm.value = prev;
        } catch { /* молча */ }
    },

    // ==========================================================================
    // ИМПОРТ
    // ==========================================================================
    async handleUpload() {
        if (this.state.isUploading) return toast('Импорт уже выполняется', 'info');
        const file = this.dom.inputUpload.files[0];
        if (!file) return toast('Выберите файл .xlsx', 'error');

        const accountType = document.querySelector('input[name="accountType"]:checked').value;
        if (!confirm(`Загрузить долги для счёта ${accountType}?`)) return;

        this.state.isUploading = true;
        if (this.dom.uploadResult) {
            this.dom.uploadResult.style.display = 'none';
            this.dom.uploadResult.innerHTML = '';
        }
        setLoading(this.dom.btnUpload, true, 'Загрузка...');

        const formData = new FormData();
        formData.append('file', file);
        formData.append('account_type', accountType);

        try {
            const res = await api.post('/financier/import-debts', formData);
            this.dom.inputUpload.value = '';
            toast(`Файл принят (Счёт ${accountType}). Обработка…`, 'info');
            this.pollTask(res.task_id);
        } catch (e) {
            toast(`Ошибка: ${e.message}`, 'error');
            this.state.isUploading = false;
            setLoading(this.dom.btnUpload, false, '⬆ Загрузить');
        }
    },

    async pollTask(taskId) {
        this.clearPoll();
        this.state.currentPollId = taskId;
        let attempts = 0;
        const maxAttempts = 150;

        const check = async () => {
            if (this.state.currentPollId !== taskId) return;
            attempts++;
            if (attempts > maxAttempts) {
                toast('Превышено время ожидания сервера.', 'warning');
                this.state.isUploading = false;
                setLoading(this.dom.btnUpload, false, '⬆ Загрузить');
                return;
            }
            try {
                const res = await api.get(`/admin/tasks/${taskId}`);
                if (this.state.currentPollId !== taskId) return;

                if (res.state === 'PENDING' || res.status === 'processing') {
                    this.state.pollTimer = setTimeout(check, 2000);
                    return;
                }
                if (res.status === 'done' || res.state === 'SUCCESS') {
                    this.renderUploadResult(res.result || res);
                    toast('Импорт завершён!', 'success');
                    this.reload();
                    this.loadImportHistory();
                    this.state.isUploading = false;
                    setLoading(this.dom.btnUpload, false, '⬆ Загрузить');
                    return;
                }
                if (res.state === 'FAILURE') throw new Error(res.error || 'Ошибка воркера');
                throw new Error('Неизвестный статус задачи');
            } catch (e) {
                if (this.state.currentPollId !== taskId) return;
                toast('Ошибка задачи: ' + e.message, 'error');
                if (this.dom.uploadResult) {
                    this.dom.uploadResult.style.display = 'block';
                    this.dom.uploadResult.innerHTML = '';
                    this.dom.uploadResult.appendChild(
                        el('div', { style: { color: 'red' } }, `Сбой: ${e.message}`)
                    );
                }
                this.state.isUploading = false;
                setLoading(this.dom.btnUpload, false, '⬆ Загрузить');
            }
        };
        check();
    },

    renderUploadResult(res) {
        if (!this.dom.uploadResult || !res) return;
        this.dom.uploadResult.innerHTML = '';
        this.dom.uploadResult.style.display = 'block';

        const success = el('div', {
            style: { padding: '15px', background: '#e8f5e9', color: '#2e7d32', borderRadius: '6px', border: '1px solid #c8e6c9' }
        },
            el('h4', { style: { margin: '0 0 10px 0' } }, `✅ Импорт завершён (Счёт ${res.account || '?'})`),
            el('ul', { style: { margin: 0, paddingLeft: '20px' } },
                el('li', {}, 'Обработано: ', el('strong', {}, String(res.processed))),
                el('li', {}, 'Обновлено: ', el('strong', {}, String(res.updated))),
                el('li', {}, 'Создано: ', el('strong', {}, String(res.created))),
                res.log_id ? el('li', {}, 'Запись в истории: ', el('strong', {}, `#${res.log_id}`)) : ''
            )
        );
        this.dom.uploadResult.appendChild(success);

        if (res.not_found_users && res.not_found_users.length) {
            const errorBox = el('div', {
                style: { marginTop: '15px', padding: '15px', background: '#ffebee', color: '#c62828', borderRadius: '6px', border: '1px solid #ffcdd2' }
            },
                el('h4', { style: { margin: '0 0 10px 0' } }, `⚠️ Не найдены (${res.not_found_users.length})`)
            );
            const scrollBox = el('div', {
                style: { maxHeight: '100px', overflow: 'auto', fontSize: '13px', background: 'rgba(255,255,255,.5)', padding: '5px' }
            });
            res.not_found_users.forEach(u => scrollBox.appendChild(el('div', {}, String(u))));
            errorBox.appendChild(scrollBox);
            if (res.log_id) {
                errorBox.appendChild(el('button', {
                    class: 'action-btn secondary-btn',
                    style: { marginTop: '10px', fontSize: '12px' },
                    onclick: () => this.openNotFoundModal(res.log_id)
                }, 'Привязать вручную'));
            }
            this.dom.uploadResult.appendChild(errorBox);
        }
    },

    // ==========================================================================
    // ТАБЛИЦА
    // ==========================================================================
    async loadUsers() {
        if (!this.dom.tableBody) return;
        const requestId = ++this.state.lastRequestId;
        this.dom.tableBody.innerHTML = '<tr><td colspan="10" style="text-align:center;padding:20px">Загрузка…</td></tr>';

        const params = new URLSearchParams({
            page: this.state.page,
            limit: this.state.limit,
            sort_by: this.state.sortBy,
            sort_dir: this.state.sortDir,
        });
        if (this.state.search) params.set('search', this.state.search);
        if (this.state.filterType === 'debtors') params.set('only_debtors', 'true');
        if (this.state.filterType === 'overpaid') params.set('only_overpaid', 'true');
        if (this.state.dormitory) params.set('dormitory', this.state.dormitory);
        if (this.state.minDebt) params.set('min_debt', this.state.minDebt);

        try {
            const data = await api.get(`/financier/users-status?${params}`);
            if (requestId !== this.state.lastRequestId) return;
            this.state.total = data.total;
            this.renderUsers(data.items);
            this.updatePagination();
        } catch (e) {
            if (requestId !== this.state.lastRequestId) return;
            this.dom.tableBody.innerHTML = '';
            this.dom.tableBody.appendChild(
                el('tr', {}, el('td', { colspan: '10', style: { color: 'red', textAlign: 'center', padding: '20px' } }, e.message))
            );
        }
    },

    updatePagination() {
        if (!this.dom.pageInfo) return;
        const totalPages = Math.ceil(this.state.total / this.state.limit) || 1;
        this.dom.pageInfo.textContent = `Стр. ${this.state.page} из ${totalPages} (Всего: ${this.state.total})`;
        this.dom.btnPrev.disabled = this.state.page <= 1;
        this.dom.btnNext.disabled = this.state.page >= totalPages;
    },

    debtChip(amount) {
        const a = Number(amount || 0);
        if (a <= 0) return '';
        if (a >= 10000) return `<span style="background:#fee2e2; color:#991b1b; padding:1px 6px; border-radius:8px; font-size:10px; font-weight:700; margin-left:4px;">КРИТ.</span>`;
        if (a >= 1000) return `<span style="background:#fef3c7; color:#92400e; padding:1px 6px; border-radius:8px; font-size:10px; font-weight:600; margin-left:4px;">средн.</span>`;
        return '';
    },

    renderUsers(users) {
        this.dom.tableBody.innerHTML = '';
        if (!users || !users.length) {
            this.dom.tableBody.innerHTML = '<tr><td colspan="10" style="text-align:center;padding:20px; color:var(--text-secondary);">Нет данных для текущих фильтров</td></tr>';
            return;
        }

        const fragment = document.createDocumentFragment();
        users.forEach(u => {
            const d209 = parseFloat(u.debt_209 || 0), o209 = parseFloat(u.overpayment_209 || 0);
            const d205 = parseFloat(u.debt_205 || 0), o205 = parseFloat(u.overpayment_205 || 0);
            const totalDebt = d209 + d205;
            const total = parseFloat(u.current_total_cost || 0);

            // Цветовой индикатор строки
            let rowBg = '';
            if (totalDebt >= 10000) rowBg = 'background:#fef2f2;';
            else if (totalDebt >= 1000) rowBg = 'background:#fffbeb;';
            else if ((o209 + o205) > 0) rowBg = 'background:#f0fdf4;';

            const room = u.room ? `${u.room.dormitory_name || '—'} / ${u.room.room_number || '—'}` : '—';

            const tr = el('tr', { class: 'table-row', style: { cssText: rowBg } },
                el('td', {}, String(u.id)),
                el('td', { style: { fontWeight: '600' } }, u.username),
                el('td', { style: { fontSize: '12px' } }, room),
                el('td', { style: { color: d209 > 0 ? '#c0392b' : '#ccc', borderLeft: '2px solid #eee' } },
                    d209 > 0 ? fmtMoney(d209) : '—'),
                el('td', { style: { color: o209 > 0 ? '#27ae60' : '#ccc' } },
                    o209 > 0 ? fmtMoney(o209) : '—'),
                el('td', { style: { color: d205 > 0 ? '#d35400' : '#ccc', borderLeft: '2px solid #eee' } },
                    d205 > 0 ? fmtMoney(d205) : '—'),
                el('td', { style: { color: o205 > 0 ? '#27ae60' : '#ccc' } },
                    o205 > 0 ? fmtMoney(o205) : '—'),
            );
            // Суммарный долг + чип
            const sumCell = el('td', { style: { fontWeight: '700', color: totalDebt > 0 ? '#b91c1c' : 'var(--text-secondary)' } });
            sumCell.innerHTML = totalDebt > 0 ? `${fmtMoney(totalDebt)}${this.debtChip(totalDebt)}` : '—';
            tr.appendChild(sumCell);

            tr.appendChild(el('td', { style: { fontWeight: 'bold' } }, total !== 0 ? fmtMoney(total) : '—'));
            tr.appendChild(
                el('td', { style: { textAlign: 'right' } },
                    el('button', {
                        class: 'action-btn', style: { padding: '4px 10px', fontSize: '12px', background: '#6366f1', color: '#fff' },
                        onclick: () => this.openAdjustModal(u.id, u.username)
                    }, 'Корр.')
                )
            );
            fragment.appendChild(tr);
        });
        this.dom.tableBody.appendChild(fragment);
    },

    // ==========================================================================
    // МОДАЛКА КОРРЕКТИРОВКИ (замена prompt-цепочки)
    // ==========================================================================
    openAdjustModal(userId, username) {
        if (!this.dom.adjustModal) return;
        this.dom.adjustForm.reset();
        this.dom.adjustUserId.value = String(userId);
        this.dom.adjustUserName.textContent = username;
        this.dom.adjustAccount.value = '209';
        this.dom.adjustModal.classList.add('open');
        setTimeout(() => this.dom.adjustAmount?.focus(), 50);
    },

    closeAdjustModal() {
        this.dom.adjustModal?.classList.remove('open');
    },

    async submitAdjust(e) {
        e.preventDefault();
        const payload = {
            user_id: Number(this.dom.adjustUserId.value),
            amount: parseFloat(this.dom.adjustAmount.value),
            description: (this.dom.adjustDescription.value || '').trim(),
            account_type: this.dom.adjustAccount.value,
        };
        if (isNaN(payload.amount)) return toast('Введите число', 'error');
        if (!payload.description) return toast('Укажите причину', 'error');

        const btn = this.dom.adjustForm.querySelector('button[type="submit"]');
        setLoading(btn, true, 'Сохранение...');
        try {
            await api.post('/admin/adjustments', payload);
            toast('Корректировка сохранена', 'success');
            this.closeAdjustModal();
            this.reload();
        } catch (err) {
            toast(err.message, 'error');
        } finally {
            setLoading(btn, false, '<i class="fa-solid fa-floppy-disk"></i> Сохранить корректировку');
        }
    },

    // ==========================================================================
    // ЭКСПОРТ
    // ==========================================================================
    async exportExcel() {
        const params = new URLSearchParams();
        if (this.state.search) params.set('search', this.state.search);
        if (this.state.filterType === 'debtors') params.set('only_debtors', 'true');
        if (this.state.filterType === 'overpaid') params.set('only_overpaid', 'true');
        if (this.state.dormitory) params.set('dormitory', this.state.dormitory);
        if (this.state.minDebt) params.set('min_debt', this.state.minDebt);
        setLoading(this.dom.btnExport, true);
        try {
            await api.download(`/financier/debts/export?${params}`, `debts_${Date.now()}.xlsx`);
            toast('Экспорт готов', 'success');
        } catch (e) {
            toast('Ошибка экспорта: ' + e.message, 'error');
        } finally {
            setLoading(this.dom.btnExport, false);
        }
    },

    // ==========================================================================
    // ИСТОРИЯ ИМПОРТОВ
    // ==========================================================================
    async loadImportHistory() {
        if (!this.dom.importHistoryList) return;
        this.dom.importHistoryList.innerHTML = '<div style="padding:20px; text-align:center; color:var(--text-secondary);"><i class="fa-solid fa-spinner fa-spin"></i> Загрузка…</div>';
        try {
            const logs = await api.get('/financier/debts/import-history?limit=20');
            if (!logs.length) {
                this.dom.importHistoryList.innerHTML = '<div style="padding:20px; text-align:center; color:var(--text-secondary); font-size:13px;">Ещё не было импортов.</div>';
                return;
            }
            this.dom.importHistoryList.innerHTML = logs.map(log => this.renderHistoryRow(log)).join('');
        } catch (e) {
            this.dom.importHistoryList.innerHTML = `<div style="padding:16px; color:var(--danger-color);">Ошибка: ${esc(e.message)}</div>`;
        }
    },

    renderHistoryRow(log) {
        const statusColors = {
            pending:   ['#3b82f6', '#dbeafe', 'В процессе'],
            completed: ['#059669', '#d1fae5', 'Готово'],
            failed:    ['#dc2626', '#fee2e2', 'Ошибка'],
            reverted:  ['#6b7280', '#f3f4f6', 'Откачен'],
        };
        const [fg, bg, label] = statusColors[log.status] || ['#6b7280', '#f3f4f6', log.status];
        const canUndo = log.status === 'completed';
        const hasNotFound = log.not_found_count > 0;

        return `
            <div style="padding:10px 12px; border-bottom:1px solid var(--border-color); display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
                <span style="background:${bg}; color:${fg}; padding:2px 8px; border-radius:10px; font-size:11px; font-weight:600; white-space:nowrap;">${esc(label)}</span>
                <span style="font-weight:600; color:#1f2937; font-size:13px; white-space:nowrap;">№${log.id} · ${esc(log.account_type)}</span>
                <span style="color:var(--text-secondary); font-size:12px; flex:1; min-width:140px;">${esc(fmtDateTime(log.started_at))} · ${esc(log.started_by || '—')}</span>
                <span style="font-size:12px; color:var(--text-secondary); white-space:nowrap;" title="Обработано / Обновлено / Создано">
                    📊 ${log.processed} / ✎ ${log.updated} / +${log.created}
                </span>
                ${hasNotFound ? `
                    <button class="action-btn secondary-btn" data-history-action="view-not-found" data-log-id="${log.id}"
                            style="padding:3px 8px; font-size:11px; background:#fef3c7; color:#92400e; border-color:#fde68a; white-space:nowrap;">
                        ⚠ ${log.not_found_count}
                    </button>` : ''}
                ${canUndo ? `
                    <button class="action-btn danger-btn" data-history-action="undo" data-log-id="${log.id}"
                            style="padding:3px 8px; font-size:11px; white-space:nowrap;">
                        <i class="fa-solid fa-rotate-left"></i> Откатить
                    </button>` : ''}
                ${log.error ? `<div style="width:100%; font-size:11px; color:#b91c1c; margin-top:4px;">${esc(log.error)}</div>` : ''}
            </div>
        `;
    },

    async undoImport(logId) {
        if (!confirm(`Откатить импорт №${logId}?\nБудут восстановлены долги/переплаты, которые были ДО этого импорта, и удалены созданные им черновики. Действие необратимо.`)) return;
        try {
            const res = await api.post(`/financier/debts/import-history/${logId}/undo`);
            toast(`Откачено: восстановлено ${res.restored_readings}, удалено ${res.removed_drafts}`, 'success');
            this.reload();
            this.loadImportHistory();
        } catch (e) {
            toast('Ошибка отката: ' + e.message, 'error');
        }
    },

    // ==========================================================================
    // МОДАЛКА «НЕ НАЙДЕННЫЕ»
    // ==========================================================================
    async openNotFoundModal(logId) {
        if (!this.dom.notFoundModal) return;
        this.dom.notFoundModal.classList.add('open');
        this.dom.notFoundLogMeta.textContent = `импорт №${logId}`;
        this.dom.notFoundList.innerHTML = '<div style="padding:20px; text-align:center;"><i class="fa-solid fa-spinner fa-spin"></i> Загрузка…</div>';
        try {
            const data = await api.get(`/financier/debts/import-history/${logId}/not-found`);
            const list = data.not_found_users || [];
            if (!list.length) {
                this.dom.notFoundList.innerHTML = '<div style="padding:20px; text-align:center; color:var(--text-secondary);">Все ФИО из этого импорта привязаны.</div>';
                return;
            }
            this.dom.notFoundList.innerHTML = `
                <p class="hint-text" style="font-size:12px; margin-bottom:12px;">
                    ФИО из Excel, которые fuzzy-матчер не смог привязать к жильцу. Для каждого — введите логин жильца и сумму.
                </p>
                ${list.map(fio => this.renderNotFoundRow(fio, logId, data.account_type)).join('')}
            `;
            this.dom.notFoundList.querySelectorAll('form[data-nf-form]').forEach(f => {
                f.addEventListener('submit', (e) => this.submitReassign(e, logId, data.account_type));
            });
        } catch (e) {
            this.dom.notFoundList.innerHTML = `<div style="padding:16px; color:var(--danger-color);">Ошибка: ${esc(e.message)}</div>`;
        }
    },

    renderNotFoundRow(fio, logId, accountType) {
        return `
            <form data-nf-form data-fio="${esc(fio)}" style="display:flex; gap:8px; align-items:center; padding:10px; border:1px solid var(--border-color); border-radius:8px; margin-bottom:8px; flex-wrap:wrap;">
                <div style="flex:1 1 240px; min-width:0;">
                    <div style="font-weight:600; font-size:13px; color:#1f2937; overflow-wrap:anywhere;">${esc(fio)}</div>
                    <div style="font-size:11px; color:var(--text-secondary);">счёт ${esc(accountType)}</div>
                </div>
                <input type="text" name="user_login" placeholder="Логин жильца" required style="width:160px;" autocomplete="off">
                <input type="number" name="debt" step="0.01" placeholder="Долг ₽" style="width:100px;">
                <input type="number" name="overpayment" step="0.01" placeholder="Перепл. ₽" style="width:100px;">
                <button type="submit" class="action-btn primary-btn" style="padding:5px 12px; font-size:12px;">
                    <i class="fa-solid fa-link"></i> Привязать
                </button>
            </form>
        `;
    },

    async submitReassign(e, logId, accountType) {
        e.preventDefault();
        const form = e.currentTarget;
        const fio = form.dataset.fio;
        const login = form.user_login.value.trim();
        const debt = parseFloat(form.debt.value) || 0;
        const overpayment = parseFloat(form.overpayment.value) || 0;

        if (!login) return toast('Укажите логин жильца', 'error');

        const btn = form.querySelector('button[type="submit"]');
        setLoading(btn, true, '...');
        try {
            // Резолвим логин → user_id (минимальный API — подсветка жильца через /users?search)
            const userSearch = await api.get(`/users?page=1&limit=5&search=${encodeURIComponent(login)}`);
            const exact = (userSearch.items || []).find(u => u.username.toLowerCase() === login.toLowerCase());
            if (!exact) {
                toast(`Жилец «${login}» не найден`, 'error');
                return;
            }

            const fd = new FormData();
            fd.append('fio', fio);
            fd.append('user_id', String(exact.id));
            fd.append('debt', String(debt));
            fd.append('overpayment', String(overpayment));

            await api.post(`/financier/debts/import-history/${logId}/reassign`, fd);
            toast(`Привязано: ${fio} → ${login}`, 'success');
            form.remove();
            this.reload();
        } catch (e2) {
            toast('Ошибка: ' + e2.message, 'error');
        } finally {
            setLoading(btn, false, '<i class="fa-solid fa-link"></i> Привязать');
        }
    },

    closeNotFoundModal() {
        this.dom.notFoundModal?.classList.remove('open');
    },
};
