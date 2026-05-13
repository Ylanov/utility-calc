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
            // Парный импорт v2 — два отдельных file-input. Старый
            // #debtFile1C оставлен fallback'ом если HTML где-то ещё
            // содержит legacy-шаблон, но в актуальном tab_debts.html
            // его нет.
            inputUpload209: document.getElementById('debtFile209'),
            inputUpload205: document.getElementById('debtFile205'),
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
            else if (action === 'diff') this.openDiffModal(logId);
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

        const file209 = this.dom.inputUpload209?.files[0] || null;
        const file205 = this.dom.inputUpload205?.files[0] || null;
        // Legacy: если только старая разметка (#debtFile1C + radio) — старая логика.
        if (!file209 && !file205) {
            const legacyFile = this.dom.inputUpload?.files[0];
            if (legacyFile) {
                return this._handleLegacyUpload(legacyFile);
            }
            return toast('Выберите хотя бы один файл .xlsx', 'error');
        }

        const summary = [
            file209 ? `209: ${file209.name}` : null,
            file205 ? `205: ${file205.name}` : null,
        ].filter(Boolean).join('\n');
        if (!confirm(`Загрузить файлы?\n${summary}`)) return;

        this.state.isUploading = true;
        if (this.dom.uploadResult) {
            this.dom.uploadResult.style.display = 'none';
            this.dom.uploadResult.innerHTML = '';
        }
        setLoading(this.dom.btnUpload, true, 'Загрузка...');

        const formData = new FormData();
        if (file209) formData.append('file_209', file209);
        if (file205) formData.append('file_205', file205);

        try {
            const res = await api.post('/financier/import-debts-pair', formData);
            // Очищаем inputs чтобы случайно не нажать «загрузить» ещё раз.
            if (this.dom.inputUpload209) this.dom.inputUpload209.value = '';
            if (this.dom.inputUpload205) this.dom.inputUpload205.value = '';

            toast(`Файлы приняты (${res.tasks?.length || 0}). Обработка…`, 'info');
            // Polling по последнему таску — обычно у нас 1-2 и они идут
            // параллельно, общая длительность определяется самым медленным.
            // Для простоты UI ждём один из тасков; loadImportHistory всё равно
            // покажет обе записи в любом случае.
            const lastTask = res.tasks?.[res.tasks.length - 1];
            if (lastTask?.task_id) {
                this.pollTask(lastTask.task_id);
            } else {
                this.state.isUploading = false;
                setLoading(this.dom.btnUpload, false, '⬆ Загрузить выбранные');
                this.loadImportHistory();
            }
        } catch (e) {
            toast(`Ошибка: ${e.message}`, 'error');
            this.state.isUploading = false;
            setLoading(this.dom.btnUpload, false, '⬆ Загрузить выбранные');
        }
    },

    async _handleLegacyUpload(file) {
        // Старая разметка (только #debtFile1C + radio) — отдельный код-пас
        // для обратной совместимости. После полного удаления tab_debts.html
        // v1 этот метод можно убрать.
        const radio = document.querySelector('input[name="accountType"]:checked');
        const accountType = radio?.value || '209';
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
            setLoading(this.dom.btnUpload, false, '⬆ Загрузить выбранные');
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
                setLoading(this.dom.btnUpload, false, '⬆ Загрузить выбранные');
                return;
            }
            try {
                const res = await api.get(`/admin/tasks/${taskId}`);
                if (this.state.currentPollId !== taskId) return;

                // Celery task проходит состояния:
                //   PENDING (в очереди) → STARTED (worker взял) → RETRY (autoretry)
                //   → SUCCESS | FAILURE
                // Раньше STARTED/RETRY валились в «Неизвестный статус» —
                // считаем их как «продолжаем polling».
                const inProgress = ['PENDING', 'STARTED', 'RETRY', 'RECEIVED'];
                if (inProgress.includes(res.state) || res.status === 'processing') {
                    this.state.pollTimer = setTimeout(check, 2000);
                    return;
                }
                if (res.status === 'done' || res.state === 'SUCCESS') {
                    this.renderUploadResult(res.result || res);
                    toast('Импорт завершён!', 'success');
                    this.reload();
                    this.loadImportHistory();
                    this.state.isUploading = false;
                    setLoading(this.dom.btnUpload, false, '⬆ Загрузить выбранные');
                    return;
                }
                if (res.state === 'FAILURE' || res.state === 'REVOKED') {
                    throw new Error(res.error || 'Ошибка воркера');
                }
                // На всякий случай: неизвестное состояние — повторяем polling,
                // а не сразу падаем с ошибкой. maxAttempts ограничит сверху.
                this.state.pollTimer = setTimeout(check, 3000);
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
                setLoading(this.dom.btnUpload, false, '⬆ Загрузить выбранные');
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
            // Группа кнопок: «История» — модалка sparkline через все импорты;
            // «Корр.» — ручная корректировка сальдо. Раньше была только Корр.
            const actionsCell = el('td', { style: { textAlign: 'right', whiteSpace: 'nowrap' } });
            actionsCell.appendChild(el('button', {
                class: 'action-btn', style: { padding: '4px 8px', fontSize: '12px', background: '#fff', color: '#4338ca', border: '1px solid #c7d2fe', marginRight: '4px' },
                title: 'История долгов через все импорты 1С',
                onclick: () => this.openUserDebtHistory(u.id, u.username),
            }, '📊'));
            actionsCell.appendChild(el('button', {
                class: 'action-btn', style: { padding: '4px 10px', fontSize: '12px', background: '#6366f1', color: '#fff' },
                onclick: () => this.openAdjustModal(u.id, u.username),
            }, 'Корр.'));
            tr.appendChild(actionsCell);
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
                ${log.has_archive ? `
                    <a href="/api/financier/debts/import-history/${log.id}/download"
                       class="action-btn secondary-btn" download
                       title="Скачать оригинальный xlsx из 1С"
                       style="padding:3px 8px; font-size:11px; white-space:nowrap; text-decoration:none;">
                        <i class="fa-solid fa-download"></i>
                    </a>` : ''}
                ${log.status === 'completed' ? `
                    <button class="action-btn secondary-btn" data-history-action="diff" data-log-id="${log.id}"
                            title="Сравнить с предыдущим импортом того же счёта"
                            style="padding:3px 8px; font-size:11px; white-space:nowrap; background:#eef2ff; color:#4338ca; border-color:#c7d2fe;">
                        <i class="fa-solid fa-code-compare"></i> Diff
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
                    ФИО из Excel, которых fuzzy-матчер не смог привязать к жильцу.
                    <b>Суммы долга/переплаты подгружены автоматически</b> — нажмите
                    «Найти похожих» (если жилец есть в системе) или «Создать жильца»
                    (если нового нет).
                </p>
                ${list.map(item => {
                    // Backend нормализует к dict {fio, debt, overpayment}.
                    // Старые импорты (до фикса) — debt/overpayment = "0".
                    const fio = (typeof item === 'object') ? item.fio : item;
                    const debt = (typeof item === 'object') ? Number(item.debt) || 0 : 0;
                    const overpay = (typeof item === 'object') ? Number(item.overpayment) || 0 : 0;
                    return this.renderNotFoundRow(fio, logId, data.account_type, debt, overpay);
                }).join('')}
            `;
            // Контекст для click-handler. Меняется при каждом openNotFoundModal,
            // handler читает из state — нет накопления listeners.
            this._nfCtx = { logId, accountType: data.account_type };
            if (!this._nfClickHandlerAttached) {
                this.dom.notFoundList.addEventListener('click', (e) => {
                    const btn = e.target.closest('button[data-nf-action]');
                    if (!btn || !this._nfCtx) return;
                    const { logId, accountType } = this._nfCtx;
                    const action = btn.dataset.nfAction;
                    const row = btn.closest('.nf-row');
                    if (!row) return;
                    const fio = row.dataset.fio;
                    if (action === 'find') {
                        this._nfFindCandidates(row, fio, logId, accountType);
                    } else if (action === 'create') {
                        this._nfShowCreateForm(row, fio, logId, accountType);
                    } else if (action === 'legacy') {
                        this._nfShowLegacyForm(row, fio, logId, accountType);
                    } else if (action === 'pick-candidate') {
                        this._nfPickCandidate(row, fio, logId, accountType, Number(btn.dataset.userId), btn.dataset.username);
                    } else if (action === 'edit-fio') {
                        this._nfEditFio(btn, Number(btn.dataset.userId), btn.dataset.username);
                    } else if (action === 'submit-create') {
                        this._nfSubmitCreate(row, fio, logId, accountType);
                    } else if (action === 'submit-legacy') {
                        this._nfSubmitLegacy(row, fio, logId, accountType);
                    }
                });
                this._nfClickHandlerAttached = true;
            }
            // Инжектим CSS для иконки-карандаша «Исправить ФИО» один раз —
            // нужен hover-стейт и opacity, которые inline в HTML не работают.
            if (!document.getElementById('nf-fio-edit-styles')) {
                const styleEl = document.createElement('style');
                styleEl.id = 'nf-fio-edit-styles';
                styleEl.textContent = `
                    .nf-edit-fio-btn {
                        background: transparent;
                        border: none;
                        padding: 2px 5px;
                        border-radius: 4px;
                        color: var(--text-tertiary);
                        opacity: 0.35;
                        cursor: pointer;
                        font-size: 11px;
                        transition: opacity 0.15s, background 0.15s, color 0.15s;
                    }
                    .nf-candidate:hover .nf-edit-fio-btn { opacity: 0.7; }
                    .nf-edit-fio-btn:hover {
                        opacity: 1 !important;
                        background: #eef2ff;
                        color: #4338ca;
                    }
                `;
                document.head.appendChild(styleEl);
            }
        } catch (e) {
            this.dom.notFoundList.innerHTML = `<div style="padding:16px; color:var(--danger-color);">Ошибка: ${esc(e.message)}</div>`;
        }
    },

    renderNotFoundRow(fio, logId, accountType, debt = 0, overpay = 0) {
        // Каждая строка содержит:
        //  - ФИО + поля для суммы (префилл из импорта, можно править)
        //  - Кнопка «Найти похожих» — раскрывает inline-блок с кандидатами
        //  - Кнопка «Создать жильца» — раскрывает форму создания
        //  - (старое) Inline-форма с логином — fallback если знаешь точный логин
        const safeId = btoa(unescape(encodeURIComponent(fio))).replace(/[^a-zA-Z0-9]/g, '').slice(0, 16);
        const sumHint = (debt > 0 || overpay > 0)
            ? `<span style="font-size:11px; color:#92400e; margin-left:6px;">
                 ${debt > 0 ? `долг ${debt.toLocaleString('ru-RU')} ₽` : ''}
                 ${overpay > 0 ? `${debt > 0 ? ' · ' : ''}переплата ${overpay.toLocaleString('ru-RU')} ₽` : ''}
                 (из файла)
               </span>`
            : '';
        return `
            <div class="nf-row" data-fio="${esc(fio)}" data-row-id="${safeId}"
                 style="border:1px solid var(--border-color); border-radius:8px; margin-bottom:10px; padding:10px;">
                <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:8px; margin-bottom:8px;">
                    <div style="flex:1; min-width:0;">
                        <div style="font-weight:600; font-size:13px; color:#1f2937; overflow-wrap:anywhere;">
                            ${esc(fio)}${sumHint}
                        </div>
                        <div style="font-size:11px; color:var(--text-secondary);">счёт ${esc(accountType)}</div>
                    </div>
                    <div style="display:flex; gap:6px; flex-wrap:wrap;">
                        <input type="number" data-nf-debt step="0.01" placeholder="Долг ₽" value="${debt || ''}" style="width:100px; font-size:12px;">
                        <input type="number" data-nf-overpay step="0.01" placeholder="Перепл. ₽" value="${overpay || ''}" style="width:100px; font-size:12px;">
                    </div>
                </div>
                <div style="display:flex; gap:6px; flex-wrap:wrap;">
                    <button data-nf-action="find" class="action-btn primary-btn" style="padding:5px 10px; font-size:12px;">
                        <i class="fa-solid fa-magnifying-glass"></i> Найти похожих
                    </button>
                    <button data-nf-action="create" class="action-btn success-btn" style="padding:5px 10px; font-size:12px;">
                        <i class="fa-solid fa-user-plus"></i> Создать жильца
                    </button>
                    <button data-nf-action="legacy" class="action-btn secondary-btn" style="padding:5px 10px; font-size:12px;">
                        <i class="fa-solid fa-keyboard"></i> Логин вручную
                    </button>
                </div>
                <div data-nf-pane="candidates" style="display:none; margin-top:10px; padding:10px; background:#f9fafb; border-radius:6px;"></div>
                <div data-nf-pane="create" style="display:none; margin-top:10px; padding:10px; background:#f0fdf4; border-radius:6px; border:1px solid #bbf7d0;"></div>
                <div data-nf-pane="legacy" style="display:none; margin-top:10px;"></div>
            </div>
        `;
    },

    _nfGetSums(row) {
        // Возвращает {debt, overpayment} из input полей в шапке строки.
        const debt = parseFloat(row.querySelector('[data-nf-debt]')?.value) || 0;
        const overpayment = parseFloat(row.querySelector('[data-nf-overpay]')?.value) || 0;
        return { debt, overpayment };
    },

    _nfShowPane(row, paneName) {
        // Прячет все панели в строке, потом показывает нужную.
        row.querySelectorAll('[data-nf-pane]').forEach(p => {
            p.style.display = p.dataset.nfPane === paneName ? '' : 'none';
        });
    },

    _nfRenderCandidates(cands, headerLabel) {
        if (!cands.length) {
            return `<div style="font-size:13px; color:var(--text-secondary); padding:8px;">
                Жильцов не нашлось.
            </div>`;
        }
        return `
            <div style="font-size:11px; color:var(--text-secondary); margin-bottom:8px; text-transform:uppercase;">
                ${headerLabel} (${cands.length})
            </div>
            ${cands.map(c => `
                <div class="nf-candidate" data-user-id="${c.id}" data-username="${esc(c.username)}"
                     style="display:flex; justify-content:space-between; align-items:center; gap:10px;
                            padding:8px 10px; background:#fff; border:1px solid var(--border-color); border-radius:6px; margin-bottom:6px;">
                    <div style="flex:1; min-width:0;">
                        <div class="nf-candidate-name-row" style="font-weight:600; font-size:13px; display:inline-flex; align-items:center; gap:6px;">
                            <span class="nf-candidate-username">${esc(c.username)}</span>
                            <span style="font-size:11px; color:var(--text-secondary);">${c.score}%</span>
                            <button data-nf-action="edit-fio" data-user-id="${c.id}" data-username="${esc(c.username)}"
                                    class="nf-edit-fio-btn"
                                    title="Исправить ФИО в базе (если в системе написано с ошибкой)">
                                <i class="fa-solid fa-pen"></i>
                            </button>
                        </div>
                        <div style="font-size:11px; color:var(--text-secondary);">
                            ${esc(c.room_label)} · ${c.residents_count} чел.
                        </div>
                        ${c.reason ? `<div style="font-size:11px; color:#92400e; margin-top:3px;">
                            <i class="fa-solid fa-circle-info"></i> ${esc(c.reason)}
                        </div>` : ''}
                    </div>
                    <button data-nf-action="pick-candidate" data-user-id="${c.id}" data-username="${esc(c.username)}"
                            class="action-btn primary-btn" style="padding:4px 10px; font-size:12px; white-space:nowrap;">
                        <i class="fa-solid fa-check"></i> Это он
                    </button>
                </div>
            `).join('')}`;
    },

    _nfEditFio(btn, userId, currentUsername) {
        // Превращаем span с username в input + кнопки Save/Cancel inline.
        const cand = btn.closest('.nf-candidate');
        if (!cand) return;
        const nameRow = cand.querySelector('.nf-candidate-name-row');
        if (!nameRow || nameRow.dataset.editing === '1') return;
        nameRow.dataset.editing = '1';
        const originalHtml = nameRow.innerHTML;
        nameRow.innerHTML = `
            <input type="text" data-nf-fio-input value="${esc(currentUsername)}"
                   style="width:65%; padding:3px 6px; font-size:13px; border:1px solid var(--border-color); border-radius:4px;">
            <button data-nf-fio-save class="action-btn success-btn" style="padding:3px 8px; font-size:11px; margin-left:4px;">
                <i class="fa-solid fa-check"></i>
            </button>
            <button data-nf-fio-cancel class="action-btn secondary-btn" style="padding:3px 8px; font-size:11px; margin-left:2px;">
                <i class="fa-solid fa-xmark"></i>
            </button>
        `;
        const input = nameRow.querySelector('[data-nf-fio-input]');
        input.focus();
        input.select();

        nameRow.querySelector('[data-nf-fio-cancel]').addEventListener('click', () => {
            nameRow.innerHTML = originalHtml;
            nameRow.dataset.editing = '';
        });
        const doSave = async () => {
            const newName = input.value.trim();
            if (!newName || newName.length < 3) {
                toast('Имя минимум 3 символа', 'warning');
                return;
            }
            if (newName === currentUsername) {
                nameRow.innerHTML = originalHtml;
                nameRow.dataset.editing = '';
                return;
            }
            try {
                await api.put(`/users/${userId}`, { username: newName });
                toast(`Имя жильца обновлено: ${newName}`, 'success');
                // Обновляем DOM на месте — не пересоздаём всю модалку.
                cand.dataset.username = newName;
                cand.querySelectorAll('button[data-username]').forEach(b => {
                    b.dataset.username = newName;
                });
                nameRow.innerHTML = originalHtml;
                nameRow.querySelector('.nf-candidate-username').textContent = newName;
                nameRow.querySelector('button[data-nf-action="edit-fio"]').dataset.username = newName;
                nameRow.dataset.editing = '';
            } catch (e) {
                toast('Ошибка обновления: ' + e.message, 'error');
            }
        };
        nameRow.querySelector('[data-nf-fio-save]').addEventListener('click', doSave);
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); doSave(); }
            if (e.key === 'Escape') {
                nameRow.innerHTML = originalHtml;
                nameRow.dataset.editing = '';
            }
        });
    },

    async _nfFindCandidates(row, fio, logId, accountType) {
        const pane = row.querySelector('[data-nf-pane="candidates"]');
        this._nfShowPane(row, 'candidates');

        // 1) Сначала рендерим контейнер с поиском и spinner-результатом —
        // чтобы input поиска появился сразу, не ждал API.
        pane.innerHTML = `
            <div style="margin-bottom:10px;">
                <input type="text" data-nf-search placeholder="🔍 Поиск по фамилии или имени (мин. 2 буквы)"
                       style="width:100%; padding:7px 10px; font-size:13px; border:1px solid var(--border-color); border-radius:6px;">
                <div style="font-size:11px; color:var(--text-secondary); margin-top:3px;">
                    Auto-suggest показывает похожих по импортированному ФИО. Введите запрос — найдёт по подстроке.
                </div>
            </div>
            <div data-nf-results>
                <div style="text-align:center; padding:14px; color:var(--text-secondary);">
                    <i class="fa-solid fa-spinner fa-spin"></i> Поиск похожих по «${esc(fio)}»…
                </div>
            </div>`;

        const results = pane.querySelector('[data-nf-results]');
        const input = pane.querySelector('[data-nf-search]');

        // 2) Auto-suggest по fio. Загрузим один раз и оставим как fallback
        // когда input пустой.
        let autoSuggestHtml = '';
        try {
            const data = await api.get(`/financier/debts/find-candidates?fio=${encodeURIComponent(fio)}&limit=15`);
            autoSuggestHtml = this._nfRenderCandidates(data.candidates || [], 'Похожие по импорту');
            results.innerHTML = autoSuggestHtml;
        } catch (e) {
            results.innerHTML = `<div style="color:#b91c1c; padding:8px;">Ошибка: ${esc(e.message)}</div>`;
        }

        // 3) Debounced ручной поиск
        let searchTimer = null;
        input.addEventListener('input', () => {
            clearTimeout(searchTimer);
            const q = input.value.trim();
            if (q.length < 2) {
                // Возвращаем auto-suggest по fio
                results.innerHTML = autoSuggestHtml;
                return;
            }
            results.innerHTML = `
                <div style="text-align:center; padding:14px; color:var(--text-secondary);">
                    <i class="fa-solid fa-spinner fa-spin"></i> Ищу «${esc(q)}»…
                </div>`;
            searchTimer = setTimeout(async () => {
                try {
                    const data = await api.get(`/financier/debts/find-candidates?q=${encodeURIComponent(q)}&limit=20`);
                    results.innerHTML = this._nfRenderCandidates(data.candidates || [], `Найдено по «${esc(q)}»`);
                } catch (err) {
                    results.innerHTML = `<div style="color:#b91c1c; padding:8px;">Ошибка: ${esc(err.message)}</div>`;
                }
            }, 250);
        });
    },

    async _nfPickCandidate(row, fio, logId, accountType, userId, username) {
        const { debt, overpayment } = this._nfGetSums(row);
        const fd = new FormData();
        fd.append('fio', fio);
        fd.append('user_id', String(userId));
        fd.append('debt', String(debt));
        fd.append('overpayment', String(overpayment));
        try {
            await api.post(`/financier/debts/import-history/${logId}/reassign`, fd);
            toast(`Привязано: ${fio} → ${username}`, 'success');
            row.remove();
            this.reload();
            this.loadImportHistory();
        } catch (e) {
            toast('Ошибка: ' + e.message, 'error');
        }
    },

    _nfShowCreateForm(row, fio, logId, accountType) {
        const pane = row.querySelector('[data-nf-pane="create"]');
        this._nfShowPane(row, 'create');
        // Генерим читаемый пароль (без 0/O, 1/l/I).
        const chars = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789';
        const arr = new Uint8Array(12);
        (window.crypto || window.msCrypto).getRandomValues(arr);
        let pwd = ''; for (let i = 0; i < 12; i++) pwd += chars[arr[i] % chars.length];
        pane.innerHTML = `
            <div style="font-size:11px; color:#166534; margin-bottom:8px; text-transform:uppercase;">
                Создать нового жильца + сразу записать долг
            </div>
            <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px;">
                <input type="text" data-nf-login placeholder="Логин (для входа)" value="${esc(fio)}" style="font-size:13px;">
                <input type="text" data-nf-pwd placeholder="Пароль" value="${pwd}" style="font-size:13px; font-family:monospace;">
                <input type="text" data-nf-dorm placeholder="Общежитие" style="font-size:13px;">
                <input type="text" data-nf-room placeholder="Номер комнаты" style="font-size:13px;">
                <input type="number" data-nf-residents value="1" min="1" max="20" placeholder="Жильцов в семье" style="font-size:13px;">
                <select data-nf-type style="font-size:13px;">
                    <option value="family">Семья (по счётчику)</option>
                    <option value="single">Одиночка (per capita)</option>
                </select>
            </div>
            <div style="margin-top:8px;">
                <button data-nf-action="submit-create" class="action-btn success-btn" style="padding:6px 12px; font-size:12px;">
                    <i class="fa-solid fa-check"></i> Создать и привязать
                </button>
            </div>
        `;
    },

    async _nfSubmitCreate(row, fio, logId, accountType) {
        const { debt, overpayment } = this._nfGetSums(row);
        const pane = row.querySelector('[data-nf-pane="create"]');
        const login = pane.querySelector('[data-nf-login]').value.trim();
        const password = pane.querySelector('[data-nf-pwd]').value.trim();
        const dorm = pane.querySelector('[data-nf-dorm]').value.trim();
        const roomNo = pane.querySelector('[data-nf-room]').value.trim();
        const residents = Number(pane.querySelector('[data-nf-residents]').value) || 1;
        const type = pane.querySelector('[data-nf-type]').value;

        if (!login || login.length < 3) return toast('Логин минимум 3 символа', 'warning');
        if (!password || password.length < 6) return toast('Пароль минимум 6 символов', 'warning');
        if (!dorm || !roomNo) return toast('Укажите общежитие и номер комнаты', 'warning');

        try {
            await api.post(`/financier/debts/import-history/${logId}/create-and-match`, {
                fio,
                username: login,
                password,
                dormitory_name: dorm,
                room_number: roomNo,
                debt,
                overpayment,
                residents_count: residents,
                resident_type: type,
            });
            toast(`Создан жилец «${login}», долг ${debt} ₽ записан`, 'success');
            row.remove();
            this.reload();
            this.loadImportHistory();
        } catch (e) {
            toast('Ошибка: ' + e.message, 'error');
        }
    },

    _nfShowLegacyForm(row, fio, logId, accountType) {
        const pane = row.querySelector('[data-nf-pane="legacy"]');
        this._nfShowPane(row, 'legacy');
        pane.innerHTML = `
            <div style="display:flex; gap:6px; align-items:center;">
                <input type="text" data-nf-legacy-login placeholder="Логин жильца" style="flex:1; font-size:13px;" autocomplete="off">
                <button data-nf-action="submit-legacy" class="action-btn primary-btn" style="padding:5px 10px; font-size:12px;">
                    <i class="fa-solid fa-link"></i> Привязать
                </button>
            </div>
            <div style="font-size:11px; color:var(--text-secondary); margin-top:4px;">
                Подходит когда знаешь точный логин жильца — поиск по точному совпадению.
            </div>
        `;
    },

    async _nfSubmitLegacy(row, fio, logId, accountType) {
        const { debt, overpayment } = this._nfGetSums(row);
        const pane = row.querySelector('[data-nf-pane="legacy"]');
        const login = pane.querySelector('[data-nf-legacy-login]').value.trim();
        if (!login) return toast('Укажите логин', 'warning');
        try {
            const userSearch = await api.get(`/users?page=1&limit=5&search=${encodeURIComponent(login)}`);
            const exact = (userSearch.items || []).find(u => u.username.toLowerCase() === login.toLowerCase());
            if (!exact) return toast(`Жилец «${login}» не найден`, 'error');

            const fd = new FormData();
            fd.append('fio', fio);
            fd.append('user_id', String(exact.id));
            fd.append('debt', String(debt));
            fd.append('overpayment', String(overpayment));
            await api.post(`/financier/debts/import-history/${logId}/reassign`, fd);
            toast(`Привязано: ${fio} → ${login}`, 'success');
            row.remove();
            this.reload();
            this.loadImportHistory();
        } catch (e) {
            toast('Ошибка: ' + e.message, 'error');
        }
    },

    closeNotFoundModal() {
        this.dom.notFoundModal?.classList.remove('open');
    },

    // ==========================================================================
    // DIFF МОДАЛКА — сравнение импорта с предыдущим того же счёта
    //
    // Открывается из кнопки «Diff» в строке истории импортов. Backend
    // /diff отдаёт 5 категорий жильцов; рисуем 5 collapsible-секций.
    // ==========================================================================
    async openDiffModal(logId) {
        // Overlay + skeleton сразу — чтобы юзер видел что клик сработал.
        const old = document.getElementById('debtDiffModal');
        if (old) old.remove();
        const modal = document.createElement('div');
        modal.id = 'debtDiffModal';
        modal.style.cssText = `
            position:fixed; inset:0; background:rgba(0,0,0,0.45); z-index:1000;
            display:flex; align-items:center; justify-content:center; padding:20px;`;
        modal.innerHTML = `
            <div style="background:var(--bg-card); border-radius:12px; max-width:1100px; width:100%;
                        max-height:90vh; display:flex; flex-direction:column; box-shadow:0 12px 40px rgba(0,0,0,0.3);">
                <div style="padding:14px 20px; border-bottom:1px solid var(--border-color);
                            display:flex; align-items:center; justify-content:space-between;">
                    <h3 style="margin:0; font-size:15px;">
                        <i class="fa-solid fa-code-compare" style="color:#4338ca;"></i>
                        Сравнение импорта №${logId}
                    </h3>
                    <button class="secondary-btn" data-close-diff style="padding:6px 12px;">
                        <i class="fa-solid fa-xmark"></i> Закрыть
                    </button>
                </div>
                <div id="debtDiffBody" style="padding:14px 20px; overflow:auto; flex:1;">
                    <div style="text-align:center; padding:40px; color:var(--text-secondary);">
                        <i class="fa-solid fa-spinner fa-spin"></i> Загрузка diff…
                    </div>
                </div>
            </div>`;
        document.body.appendChild(modal);
        modal.addEventListener('click', (e) => {
            if (e.target === modal || e.target.closest('[data-close-diff]')) modal.remove();
        });
        const escHandler = (e) => { if (e.key === 'Escape') { modal.remove(); document.removeEventListener('keydown', escHandler); } };
        document.addEventListener('keydown', escHandler);

        try {
            const data = await api.get(`/financier/debts/import-history/${logId}/diff`);
            this._renderDiff(data);
        } catch (e) {
            const body = document.getElementById('debtDiffBody');
            if (body) body.innerHTML = `
                <div style="padding:16px; background:#fef2f2; border:1px solid #fecaca; border-radius:8px; color:#991b1b;">
                    Ошибка загрузки: ${esc(e.message)}
                </div>`;
        }
    },

    _renderDiff(data) {
        const body = document.getElementById('debtDiffBody');
        if (!body) return;

        if (data.fatal) {
            body.innerHTML = `
                <div style="padding:30px; text-align:center; color:var(--text-secondary);">
                    <i class="fa-solid fa-circle-info" style="font-size:24px; color:#3b82f6;"></i>
                    <div style="margin-top:10px;">${esc(data.fatal)}</div>
                </div>`;
            return;
        }

        const s = data.summary || {};
        const acc = data.account_type;
        const prevDate = data.previous_started_at
            ? new Date(data.previous_started_at).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric' })
            : '—';
        const curDate = data.current_started_at
            ? new Date(data.current_started_at).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric' })
            : '—';

        const header = `
            <div style="margin-bottom:18px; padding:12px 14px; background:#f9fafb; border:1px solid var(--border-color); border-radius:8px; font-size:13px;">
                <div style="margin-bottom:6px;">
                    <b>Счёт ${esc(acc)}:</b> сравнение
                    <span style="color:var(--text-secondary);">№${data.previous_id} (${esc(prevDate)})</span>
                    <i class="fa-solid fa-arrow-right" style="margin:0 6px;"></i>
                    <b>№${data.current_id} (${esc(curDate)})</b>
                </div>
                <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(160px, 1fr)); gap:8px; margin-top:8px;">
                    <div style="padding:8px 10px; background:#fef2f2; border-radius:6px; border:1px solid #fecaca;">
                        <div style="font-size:11px; color:#991b1b; text-transform:uppercase;">Новые должники</div>
                        <div style="font-size:18px; font-weight:700; color:#dc2626;">${s.new_debtors_count || 0}</div>
                    </div>
                    <div style="padding:8px 10px; background:#fff7ed; border-radius:6px; border:1px solid #fed7aa;">
                        <div style="font-size:11px; color:#9a3412; text-transform:uppercase;">Долг вырос</div>
                        <div style="font-size:18px; font-weight:700; color:#ea580c;">${s.debt_grew_count || 0}</div>
                        <div style="font-size:11px; color:var(--text-secondary);">+${fmtMoney(s.sum_new_and_grew || 0)}</div>
                    </div>
                    <div style="padding:8px 10px; background:#f0fdf4; border-radius:6px; border:1px solid #bbf7d0;">
                        <div style="font-size:11px; color:#166534; text-transform:uppercase;">Долг упал</div>
                        <div style="font-size:18px; font-weight:700; color:#16a34a;">${s.debt_dropped_count || 0}</div>
                        <div style="font-size:11px; color:var(--text-secondary);">−${fmtMoney(s.sum_dropped || 0)}</div>
                    </div>
                    <div style="padding:8px 10px; background:#ecfdf5; border-radius:6px; border:1px solid #a7f3d0;">
                        <div style="font-size:11px; color:#065f46; text-transform:uppercase;">Долг закрыт</div>
                        <div style="font-size:18px; font-weight:700; color:#10b981;">${s.debt_closed_count || 0}</div>
                        <div style="font-size:11px; color:var(--text-secondary);">−${fmtMoney(s.sum_closed || 0)}</div>
                    </div>
                    <div style="padding:8px 10px; background:#ede9fe; border-radius:6px; border:1px solid #ddd6fe;">
                        <div style="font-size:11px; color:#5b21b6; text-transform:uppercase;">Новые переплаты</div>
                        <div style="font-size:18px; font-weight:700; color:#7c3aed;">${s.new_overpay_count || 0}</div>
                    </div>
                </div>
            </div>`;

        const sec = (title, items, kind) => {
            if (!items || !items.length) return '';
            const colorMap = {
                new_debtors: '#dc2626',
                debt_grew:   '#ea580c',
                debt_dropped:'#16a34a',
                debt_closed: '#10b981',
                new_overpay: '#7c3aed',
            };
            const c = colorMap[kind] || '#6b7280';
            const rows = items.map(it => {
                const valueCell = kind === 'new_overpay'
                    ? `<td style="text-align:right; font-weight:600; color:${c};">${fmtMoney(it.overpayment)}</td>`
                    : `<td style="text-align:right; color:var(--text-secondary);">${fmtMoney(it.prev_debt)}</td>
                       <td style="text-align:right; font-weight:600;">${fmtMoney(it.current_debt)}</td>
                       <td style="text-align:right; font-weight:600; color:${c};">${it.delta >= 0 ? '+' : ''}${fmtMoney(it.delta)}</td>`;
                return `
                    <tr style="border-bottom:1px solid var(--border-color);">
                        <td style="padding:6px 10px;">${esc(it.username)}</td>
                        <td style="padding:6px 10px; color:var(--text-secondary); font-size:11px;">${esc(it.room_label)}</td>
                        ${valueCell}
                    </tr>`;
            }).join('');
            const headers = kind === 'new_overpay'
                ? '<th style="text-align:left; padding:6px 10px;">Жилец</th><th style="text-align:left; padding:6px 10px;">Комната</th><th style="text-align:right; padding:6px 10px;">Переплата</th>'
                : '<th style="text-align:left; padding:6px 10px;">Жилец</th><th style="text-align:left; padding:6px 10px;">Комната</th><th style="text-align:right; padding:6px 10px;">Было</th><th style="text-align:right; padding:6px 10px;">Стало</th><th style="text-align:right; padding:6px 10px;">Δ</th>';
            return `
                <details style="margin-bottom:14px; border:1px solid var(--border-color); border-radius:8px; overflow:hidden;" open>
                    <summary style="padding:10px 14px; cursor:pointer; background:${c}11; color:${c}; font-weight:600; font-size:13px;">
                        ${esc(title)} (${items.length})
                    </summary>
                    <table style="width:100%; border-collapse:collapse; font-size:12px;">
                        <thead style="background:var(--bg-page); font-size:11px; color:var(--text-secondary); text-transform:uppercase;">
                            <tr>${headers}</tr>
                        </thead>
                        <tbody>${rows}</tbody>
                    </table>
                </details>`;
        };

        body.innerHTML = header
            + sec('Новые должники', data.new_debtors, 'new_debtors')
            + sec('Долг вырос', data.debt_grew, 'debt_grew')
            + sec('Долг упал', data.debt_dropped, 'debt_dropped')
            + sec('Долг закрыт', data.debt_closed, 'debt_closed')
            + sec('Появились переплаты', data.new_overpay, 'new_overpay');

        // Если все секции пусты — показать «всё то же самое»
        if (!data.new_debtors?.length && !data.debt_grew?.length
            && !data.debt_dropped?.length && !data.debt_closed?.length
            && !data.new_overpay?.length) {
            body.innerHTML = header + `
                <div style="text-align:center; padding:40px; color:var(--text-secondary);">
                    <i class="fa-solid fa-equals" style="font-size:24px; color:#10b981;"></i>
                    <div style="margin-top:10px;">Изменений нет — суммы совпадают с прошлым импортом.</div>
                </div>`;
        }
    },

    // ==========================================================================
    // ИСТОРИЯ ДОЛГОВ ЖИЛЬЦА — sparkline 209 + 205 через все импорты
    // ==========================================================================
    async openUserDebtHistory(userId, username) {
        document.getElementById('debtUserHistoryModal')?.remove();
        const modal = document.createElement('div');
        modal.id = 'debtUserHistoryModal';
        modal.style.cssText = `
            position:fixed; inset:0; background:rgba(0,0,0,0.45); z-index:1000;
            display:flex; align-items:center; justify-content:center; padding:20px;`;
        modal.innerHTML = `
            <div style="background:var(--bg-card); border-radius:12px; max-width:820px; width:100%;
                        max-height:85vh; display:flex; flex-direction:column; box-shadow:0 12px 40px rgba(0,0,0,0.3);">
                <div style="padding:14px 20px; border-bottom:1px solid var(--border-color);
                            display:flex; align-items:center; justify-content:space-between;">
                    <h3 style="margin:0; font-size:15px;">
                        <i class="fa-solid fa-chart-line" style="color:#4338ca;"></i>
                        История долгов: ${esc(username)}
                    </h3>
                    <button class="secondary-btn" data-close-uh style="padding:6px 12px;">
                        <i class="fa-solid fa-xmark"></i> Закрыть
                    </button>
                </div>
                <div id="debtUserHistoryBody" style="padding:14px 20px; overflow:auto; flex:1;">
                    <div style="text-align:center; padding:40px; color:var(--text-secondary);">
                        <i class="fa-solid fa-spinner fa-spin"></i> Загрузка…
                    </div>
                </div>
            </div>`;
        document.body.appendChild(modal);
        modal.addEventListener('click', (e) => {
            if (e.target === modal || e.target.closest('[data-close-uh]')) modal.remove();
        });
        const escHandler = (e) => { if (e.key === 'Escape') { modal.remove(); document.removeEventListener('keydown', escHandler); } };
        document.addEventListener('keydown', escHandler);

        try {
            const data = await api.get(`/financier/debts/user-debt-history/${userId}`);
            this._renderUserDebtHistory(data);
        } catch (e) {
            const body = document.getElementById('debtUserHistoryBody');
            if (body) body.innerHTML = `
                <div style="padding:16px; background:#fef2f2; border:1px solid #fecaca; border-radius:8px; color:#991b1b;">
                    Ошибка: ${esc(e.message)}
                </div>`;
        }
    },

    _renderUserDebtHistory(data) {
        const body = document.getElementById('debtUserHistoryBody');
        if (!body) return;

        if (data.fatal) {
            body.innerHTML = `
                <div style="padding:30px; text-align:center; color:var(--text-secondary);">
                    <i class="fa-solid fa-circle-info" style="font-size:24px; color:#f59e0b;"></i>
                    <div style="margin-top:10px;">${esc(data.fatal)}</div>
                </div>`;
            return;
        }
        if (!data.points || !data.points.length) {
            body.innerHTML = `
                <div style="padding:30px; text-align:center; color:var(--text-secondary);">
                    Жилец не встречался ни в одном импорте — долгов нет.
                </div>`;
            return;
        }

        // Разделяем точки на 209 / 205, рисуем 2 sparkline + table
        const points209 = data.points.filter(p => p.account_type === '209');
        const points205 = data.points.filter(p => p.account_type === '205');

        // SVG sparkline — высота 60, ширина ~600
        const renderSpark = (pts, color, account) => {
            if (!pts.length) {
                return `<div style="color:var(--text-secondary); font-size:12px; padding:14px;">${account}: данных нет</div>`;
            }
            const W = 580, H = 60, P = 20;
            const debts = pts.map(p => p.debt);
            const maxD = Math.max(...debts, 1);
            const step = pts.length > 1 ? (W - 2 * P) / (pts.length - 1) : 0;
            const polyline = pts.map((p, i) => {
                const x = P + i * step;
                const y = H - P - (p.debt / maxD) * (H - 2 * P);
                return `${x.toFixed(1)},${y.toFixed(1)}`;
            }).join(' ');
            const last = pts[pts.length - 1];
            return `
                <div style="margin-bottom:12px;">
                    <div style="font-size:12px; color:var(--text-secondary); margin-bottom:4px;">
                        Счёт <b>${account}</b>: ${pts.length} точек, max ${fmtMoney(maxD)}, последний долг <b style="color:${color};">${fmtMoney(last.debt)}</b>
                    </div>
                    <svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" style="border:1px solid var(--border-color); border-radius:6px; background:#fafafa; display:block;">
                        <polyline fill="none" stroke="${color}" stroke-width="2" points="${polyline}"/>
                        ${pts.map((p, i) => {
                            const x = P + i * step;
                            const y = H - P - (p.debt / maxD) * (H - 2 * P);
                            return `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="3" fill="${color}">
                                      <title>${new Date(p.started_at).toLocaleDateString('ru-RU')}: ${fmtMoney(p.debt)}</title>
                                    </circle>`;
                        }).join('')}
                    </svg>
                </div>`;
        };

        const tableRows = data.points
            .slice()
            .reverse()  // самый свежий импорт сверху
            .map(p => `
                <tr style="border-bottom:1px solid var(--border-color);">
                    <td style="padding:6px 10px; font-size:12px;">${new Date(p.started_at).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric' })}</td>
                    <td style="padding:6px 10px;"><span style="background:${p.account_type === '209' ? '#dbeafe' : '#fef3c7'}; color:${p.account_type === '209' ? '#1e40af' : '#92400e'}; padding:2px 6px; border-radius:4px; font-size:11px; font-weight:600;">${p.account_type}</span></td>
                    <td style="padding:6px 10px; text-align:right; font-weight:600; color:${p.debt > 0 ? '#dc2626' : 'var(--text-secondary)'};">${p.debt > 0 ? fmtMoney(p.debt) : '—'}</td>
                    <td style="padding:6px 10px; text-align:right; color:${p.overpayment > 0 ? '#7c3aed' : 'var(--text-secondary)'};">${p.overpayment > 0 ? fmtMoney(p.overpayment) : '—'}</td>
                    <td style="padding:6px 10px; color:var(--text-secondary); font-size:11px;">${esc(p.file_name || '—')}</td>
                </tr>`).join('');

        body.innerHTML = `
            <div style="margin-bottom:10px; color:var(--text-secondary); font-size:12px;">
                Комната: <b>${esc(data.room_label || '—')}</b> ·
                ${data.points.length} ${data.points.length === 1 ? 'импорт' : (data.points.length < 5 ? 'импорта' : 'импортов')}
            </div>
            ${renderSpark(points209, '#dc2626', '209 (Коммуналка)')}
            ${renderSpark(points205, '#ea580c', '205 (Найм)')}
            <table style="width:100%; margin-top:14px; border-collapse:collapse; font-size:13px;">
                <thead style="background:var(--bg-page); font-size:11px; color:var(--text-secondary); text-transform:uppercase;">
                    <tr>
                        <th style="text-align:left; padding:6px 10px;">Дата</th>
                        <th style="text-align:left; padding:6px 10px;">Счёт</th>
                        <th style="text-align:right; padding:6px 10px;">Долг</th>
                        <th style="text-align:right; padding:6px 10px;">Переплата</th>
                        <th style="text-align:left; padding:6px 10px;">Файл</th>
                    </tr>
                </thead>
                <tbody>${tableRows}</tbody>
            </table>`;
    },
};
