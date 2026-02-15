// static/js/modules/financier.js

import { api } from '../core/api.js';
import { el, toast, showPrompt, setLoading } from '../core/dom.js';


export const FinancierApp = {

    // ==================================================
    // STATE
    // ==================================================

    state: {
        page: 1,
        limit: 50,
        total: 0,
        search: '',
        importTaskId: null,
        pollTimer: null,
        isUploading: false,
        lastRequestId: 0
    },


    // ==================================================
    // INIT
    // ==================================================

    init() {
        this.cacheDOM();
        this.bindEvents();
        this.loadUsers();
    },


    // ==================================================
    // DOM CACHE
    // ==================================================

    cacheDOM() {

        this.dom = {

            usersTableBody: document.getElementById('usersTableBody'),

            btnRefresh: document.getElementById('btnRefreshUsers'),

            btnUpload: document.getElementById('btnUploadDebts'),

            inputUpload: document.getElementById('debtFile1C'),

            uploadResult: document.getElementById('uploadResult'),

            // Pagination
            btnPrev: document.getElementById('btnPrevPage'),
            btnNext: document.getElementById('btnNextPage'),
            pageInfo: document.getElementById('pageInfo'),

            // Search
            searchInput: document.getElementById('userSearchInput')
        };
    },


    // ==================================================
    // EVENTS
    // ==================================================

    bindEvents() {

        if (this.dom.btnRefresh) {
            this.dom.btnRefresh.addEventListener(
                'click',
                () => this.reload()
            );
        }

        if (this.dom.btnUpload) {
            this.dom.btnUpload.addEventListener(
                'click',
                () => this.handleUpload()
            );
        }

        if (this.dom.btnPrev) {
            this.dom.btnPrev.addEventListener(
                'click',
                () => this.changePage(-1)
            );
        }

        if (this.dom.btnNext) {
            this.dom.btnNext.addEventListener(
                'click',
                () => this.changePage(1)
            );
        }


        // Debounced search
        if (this.dom.searchInput) {

            let timeout;

            this.dom.searchInput.addEventListener('input', e => {

                clearTimeout(timeout);

                timeout = setTimeout(() => {

                    this.state.search = e.target.value || '';
                    this.state.page = 1;

                    this.loadUsers();

                }, 400);

            });

        }

    },


    // ==================================================
    // HELPERS
    // ==================================================

    reload() {
        this.state.page = 1;
        this.loadUsers();
    },


    changePage(delta) {

        const newPage = this.state.page + delta;

        if (newPage < 1) return;

        this.state.page = newPage;

        this.loadUsers();
    },


    clearPoll() {

        if (this.state.pollTimer) {
            clearTimeout(this.state.pollTimer);
            this.state.pollTimer = null;
        }
    },


    // ==================================================
    // UPLOAD
    // ==================================================

    async handleUpload() {

        if (this.state.isUploading) {
            toast('Импорт уже выполняется', 'info');
            return;
        }

        const file = this.dom.inputUpload.files[0];

        if (!file) {
            toast('Выберите файл .xlsx', 'error');
            return;
        }

        this.state.isUploading = true;

        if (this.dom.uploadResult) {
            this.dom.uploadResult.style.display = 'none';
            this.dom.uploadResult.innerHTML = '';
        }

        setLoading(this.dom.btnUpload, true, 'Загрузка...');


        const formData = new FormData();
        formData.append('file', file);


        try {

            const res = await api.post(
                '/financier/import-debts',
                formData
            );

            this.dom.inputUpload.value = '';

            toast('Файл принят. Началась обработка...', 'info');

            this.pollTask(res.task_id);

        } catch (e) {

            toast(`Ошибка запуска: ${e.message}`, 'error');

            this.state.isUploading = false;

            setLoading(
                this.dom.btnUpload,
                false,
                '⬆ Загрузить долги'
            );

        }

    },


    // ==================================================
    // TASK POLLING
    // ==================================================

    async pollTask(taskId) {

        this.clearPoll();

        const check = async () => {

            try {

                const res = await api.get(
                    `/admin/tasks/${taskId}`
                );

                if (
                    res.state === 'PENDING' ||
                    res.status === 'processing'
                ) {

                    this.state.pollTimer = setTimeout(
                        check,
                        2000
                    );

                    return;
                }


                if (
                    res.status === 'done' ||
                    res.state === 'SUCCESS'
                ) {

                    const data = res.result || res;

                    this.renderUploadResult(data);

                    toast('Импорт завершен!', 'success');

                    this.reload();

                    return;
                }


                if (res.state === 'FAILURE') {
                    throw new Error(
                        res.error || 'Ошибка воркера'
                    );
                }


                throw new Error('Неизвестный статус');

            } catch (e) {

                toast('Ошибка задачи: ' + e.message, 'error');

                if (this.dom.uploadResult) {

                    this.dom.uploadResult.style.display = 'block';

                    this.dom.uploadResult.innerHTML =
                        `<div style="color:red">Сбой: ${e.message}</div>`;

                }

            } finally {

                if (
                    !this.state.pollTimer ||
                    this.state.pollTimer === null
                ) {

                    this.state.isUploading = false;

                    setLoading(
                        this.dom.btnUpload,
                        false,
                        '⬆ Загрузить долги'
                    );
                }

            }

        };

        check();
    },


    // ==================================================
    // UPLOAD RESULT
    // ==================================================

    renderUploadResult(res) {

        if (!this.dom.uploadResult || !res) return;


        let html = `
            <div style="
                padding: 15px;
                background: #e8f5e9;
                color: #2e7d32;
                border-radius: 6px;
                border: 1px solid #c8e6c9;
            ">
                <h4 style="margin:0 0 10px 0;">
                    ✅ Импорт завершен
                </h4>

                <ul style="margin:0; padding-left:20px;">
                    <li>Обработано: <strong>${res.processed}</strong></li>
                    <li>Обновлено: <strong>${res.updated}</strong></li>
                </ul>
            </div>
        `;


        if (
            res.not_found_users &&
            res.not_found_users.length
        ) {

            html += `
                <div style="
                    margin-top:15px;
                    padding:15px;
                    background:#ffebee;
                    color:#c62828;
                    border-radius:6px;
                    border:1px solid #ffcdd2;
                ">
                    <h4>
                        ⚠️ Не найдены (${res.not_found_users.length})
                    </h4>

                    <div style="
                        max-height:100px;
                        overflow:auto;
                        font-size:13px;
                        background:rgba(255,255,255,.5);
                        padding:5px;
                    ">
                        ${res.not_found_users.join('<br>')}
                    </div>
                </div>
            `;

        }


        this.dom.uploadResult.innerHTML = html;

        this.dom.uploadResult.style.display = 'block';
    },


    // ==================================================
    // LOAD USERS
    // ==================================================

    async loadUsers() {

        if (!this.dom.usersTableBody) return;


        const requestId = ++this.state.lastRequestId;


        this.dom.usersTableBody.innerHTML = `
            <tr>
                <td colspan="7"
                    style="text-align:center; padding:20px;">
                    Загрузка...
                </td>
            </tr>
        `;


        try {

            const search = encodeURIComponent(
                this.state.search || ''
            );

            const query =
                `?page=${this.state.page}` +
                `&limit=${this.state.limit}` +
                `&search=${search}`;


            const data = await api.get(
                `/financier/users-status${query}`
            );


            if (requestId !== this.state.lastRequestId) {
                return;
            }


            this.state.total = data.total;

            this.renderUsers(data.items);

            this.updatePagination();


        } catch (e) {

            if (requestId !== this.state.lastRequestId) {
                return;
            }

            this.dom.usersTableBody.innerHTML = `
                <tr>
                    <td colspan="7"
                        style="color:red; text-align:center;">
                        ${e.message}
                    </td>
                </tr>
            `;

        }

    },


    updatePagination() {

        if (!this.dom.pageInfo) return;


        const totalPages =
            Math.ceil(
                this.state.total / this.state.limit
            ) || 1;


        this.dom.pageInfo.textContent =
            `Стр. ${this.state.page} из ${totalPages} ` +
            `(Всего: ${this.state.total})`;


        this.dom.btnPrev.disabled =
            this.state.page <= 1;

        this.dom.btnNext.disabled =
            this.state.page >= totalPages;
    },


    // ==================================================
    // RENDER USERS
    // ==================================================

    renderUsers(users) {

        this.dom.usersTableBody.innerHTML = '';


        if (!users || !users.length) {

            this.dom.usersTableBody.innerHTML = `
                <tr>
                    <td colspan="7"
                        style="text-align:center; padding:20px;">
                        Нет данных
                    </td>
                </tr>
            `;

            return;
        }


        const fragment =
            document.createDocumentFragment();


        users.forEach(u => {

            const debt = parseFloat(u.initial_debt || 0);
            const over = parseFloat(u.initial_overpayment || 0);
            const total = parseFloat(u.current_total_cost || 0);


            const debtStyle =
                debt > 0
                    ? 'color:#c0392b;font-weight:bold;'
                    : 'color:#ccc;';


            const overStyle =
                over > 0
                    ? 'color:#27ae60;font-weight:bold;'
                    : 'color:#ccc;';


            const tr = el(
                'tr',
                { class: 'table-row' },

                el('td', {}, String(u.id)),

                el(
                    'td',
                    { style: { fontWeight: '600' } },
                    u.username
                ),

                el('td', {}, u.dormitory || '-'),

                el(
                    'td',
                    { style: debtStyle },
                    debt > 0 ? debt.toFixed(2) : '-'
                ),

                el(
                    'td',
                    { style: overStyle },
                    over > 0 ? over.toFixed(2) : '-'
                ),

                el(
                    'td',
                    { style: { fontWeight: 'bold' } },
                    total !== 0 ? total.toFixed(2) : '-'
                ),

                el(
                    'td',
                    { style: { textAlign: 'right' } },

                    el(
                        'button',
                        {
                            class: 'action-btn',
                            style: {
                                padding: '4px 8px',
                                fontSize: '12px',
                                background: '#3498db'
                            },
                            onclick: () =>
                                this.openDebtModal(
                                    u.id,
                                    u.username
                                )
                        },
                        'Корр.'
                    )

                )

            );

            fragment.appendChild(tr);

        });


        this.dom.usersTableBody.appendChild(fragment);
    },


    // ==================================================
    // ADJUSTMENTS
    // ==================================================

    async openDebtModal(userId, username) {

        const amountStr = await showPrompt(
            `Корректировка: ${username}`,
            'Введите сумму:'
        );

        if (amountStr === null) return;


        const amount = parseFloat(amountStr);

        if (isNaN(amount)) {
            toast('Введите число', 'error');
            return;
        }


        const desc = await showPrompt(
            'Причина',
            'Основание:',
            'Ручная'
        );

        if (!desc) return;


        try {

            await api.post(
                '/admin/adjustments',
                {
                    user_id: userId,
                    amount,
                    description: desc
                }
            );

            toast('Сохранено', 'success');

            this.reload();

        } catch (e) {

            toast(e.message, 'error');

        }

    }

};
