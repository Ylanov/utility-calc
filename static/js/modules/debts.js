import { api } from '../core/api.js';
import { el, toast, showPrompt, setLoading } from '../core/dom.js';

export const DebtsModule = {
    isInitialized: false,
    state: {
        page: 1, limit: 50, total: 0, search: '',
        importTaskId: null, pollTimer: null, isUploading: false, lastRequestId: 0,
        currentPollId: null
    },

    init() {
        this.cacheDOM();
        if (!this.isInitialized) {
            this.bindEvents();
            this.isInitialized = true;
        }
        this.loadUsers();
    },

    cacheDOM() {
        this.dom = {
            tableBody: document.getElementById('debtsTableBody'),
            btnRefresh: document.getElementById('btnRefreshDebts'),
            btnUpload: document.getElementById('btnUploadDebts'),
            inputUpload: document.getElementById('debtFile1C'),
            uploadResult: document.getElementById('uploadResult'),
            btnPrev: document.getElementById('btnPrevDebts'),
            btnNext: document.getElementById('btnNextDebts'),
            pageInfo: document.getElementById('debtsPageInfo'),
            searchInput: document.getElementById('debtsSearchInput')
        };
    },

    bindEvents() {
        if(this.dom.btnRefresh) this.dom.btnRefresh.addEventListener('click', ()=>this.reload());
        if(this.dom.btnUpload) this.dom.btnUpload.addEventListener('click', ()=>this.handleUpload());
        if(this.dom.btnPrev) this.dom.btnPrev.addEventListener('click', ()=>this.changePage(-1));
        if(this.dom.btnNext) this.dom.btnNext.addEventListener('click', ()=>this.changePage(1));
        if(this.dom.searchInput) {
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

    reload() { this.state.page = 1; this.loadUsers(); },

    changePage(delta) {
        const newPage = this.state.page + delta;
        if(newPage < 1) return;
        this.state.page = newPage;
        this.loadUsers();
    },

    clearPoll() {
        if(this.state.pollTimer) {
            clearTimeout(this.state.pollTimer);
            this.state.pollTimer = null;
        }
        this.state.currentPollId = null;
    },

    async handleUpload() {
        if(this.state.isUploading) return toast('Импорт уже выполняется', 'info');
        const file = this.dom.inputUpload.files[0];
        if(!file) return toast('Выберите файл .xlsx', 'error');

        const accountType = document.querySelector('input[name="accountType"]:checked').value;
        if(!confirm(`Загрузить долги для счета ${accountType}?`)) return;

        this.state.isUploading = true;
        if(this.dom.uploadResult) {
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
            toast(`Файл принят (Счет ${accountType}). Обработка...`, 'info');
            this.pollTask(res.task_id);
        } catch(e) {
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
            if(this.state.currentPollId !== taskId) return;
            attempts++;
            if (attempts > maxAttempts) {
                toast('Превышено время ожидания сервера.', 'warning');
                this.state.isUploading = false;
                setLoading(this.dom.btnUpload, false, '⬆ Загрузить');
                return;
            }

            try {
                const res = await api.get(`/admin/tasks/${taskId}`);
                if(this.state.currentPollId !== taskId) return;

                if(res.state === 'PENDING' || res.status === 'processing') {
                    this.state.pollTimer = setTimeout(check, 2000);
                    return;
                }
                if(res.status === 'done' || res.state === 'SUCCESS') {
                    this.renderUploadResult(res.result || res);
                    toast('Импорт завершен!', 'success');
                    this.reload();
                    this.state.isUploading = false;
                    setLoading(this.dom.btnUpload, false, '⬆ Загрузить');
                    return;
                }
                if(res.state === 'FAILURE') throw new Error(res.error || 'Ошибка воркера');
                throw new Error('Неизвестный статус задачи');
            } catch(e) {
                if(this.state.currentPollId !== taskId) return;
                toast('Ошибка задачи: ' + e.message, 'error');

                if(this.dom.uploadResult) {
                    this.dom.uploadResult.style.display = 'block';
                    this.dom.uploadResult.innerHTML = ''; // Безопасная очистка
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
        if(!this.dom.uploadResult || !res) return;

        // Очищаем контейнер безопасным способом
        this.dom.uploadResult.innerHTML = '';
        this.dom.uploadResult.style.display = 'block';

        // Создаем успешный блок
        const successBox = el('div', {
            style: {
                padding: '15px', background: '#e8f5e9', color: '#2e7d32',
                borderRadius: '6px', border: '1px solid #c8e6c9'
            }
        },
            el('h4', { style: { margin: '0 0 10px 0' } }, `✅ Импорт завершен (Счет ${res.account || '?'})`),
            el('ul', { style: { margin: 0, paddingLeft: '20px' } },
                el('li', {}, 'Обработано: ', el('strong', {}, String(res.processed))),
                el('li', {}, 'Обновлено: ', el('strong', {}, String(res.updated))),
                el('li', {}, 'Создано: ', el('strong', {}, String(res.created)))
            )
        );
        this.dom.uploadResult.appendChild(successBox);

        // Если есть ненайденные пользователи
        if (res.not_found_users && res.not_found_users.length) {
            const errorBox = el('div', {
                style: {
                    marginTop: '15px', padding: '15px', background: '#ffebee',
                    color: '#c62828', borderRadius: '6px', border: '1px solid #ffcdd2'
                }
            },
                el('h4', { style: { margin: '0 0 10px 0' } }, `⚠️ Не найдены (${res.not_found_users.length})`)
            );

            // Создаем контейнер для списка с прокруткой
            const scrollBox = el('div', {
                style: {
                    maxHeight: '100px', overflow: 'auto', fontSize: '13px',
                    background: 'rgba(255,255,255,.5)', padding: '5px'
                }
            });

            // Добавляем пользователей по одному через el() для защиты от XSS
            res.not_found_users.forEach(user => {
                scrollBox.appendChild(el('div', {}, String(user)));
            });

            errorBox.appendChild(scrollBox);
            this.dom.uploadResult.appendChild(errorBox);
        }
    },

    async loadUsers() {
        if(!this.dom.tableBody) return;
        const requestId = ++this.state.lastRequestId;
        this.dom.tableBody.innerHTML = '<tr><td colspan="9" style="text-align:center;padding:20px">Загрузка...</td></tr>';

        try {
            const search = encodeURIComponent(this.state.search || '');
            const query = `?page=${this.state.page}&limit=${this.state.limit}&search=${search}`;
            const data = await api.get(`/financier/users-status${query}`);
            if(requestId !== this.state.lastRequestId) return;

            this.state.total = data.total;
            this.renderUsers(data.items);
            this.updatePagination();
        } catch(e) {
            if(requestId !== this.state.lastRequestId) return;
            // Безопасный вывод ошибки загрузки таблицы
            this.dom.tableBody.innerHTML = '';
            this.dom.tableBody.appendChild(
                el('tr', {},
                    el('td', { colspan: '9', style: { color: 'red', textAlign: 'center', padding: '20px' } }, e.message)
                )
            );
        }
    },

    updatePagination() {
        if(!this.dom.pageInfo) return;
        const totalPages = Math.ceil(this.state.total / this.state.limit) || 1;
        this.dom.pageInfo.textContent = `Стр. ${this.state.page} из ${totalPages} (Всего: ${this.state.total})`;
        this.dom.btnPrev.disabled = this.state.page <= 1;
        this.dom.btnNext.disabled = this.state.page >= totalPages;
    },

    renderUsers(users) {
        this.dom.tableBody.innerHTML = '';
        if(!users || !users.length) {
            this.dom.tableBody.innerHTML = '<tr><td colspan="9" style="text-align:center;padding:20px">Нет данных</td></tr>';
            return;
        }

        const fragment = document.createDocumentFragment();
        users.forEach(u => {
            const d209 = parseFloat(u.debt_209 || 0), o209 = parseFloat(u.overpayment_209 || 0);
            const d205 = parseFloat(u.debt_205 || 0), o205 = parseFloat(u.overpayment_205 || 0);
            const total = parseFloat(u.current_total_cost || 0);

            const tr = el('tr', {class:'table-row'},
                el('td', {}, String(u.id)),
                el('td', {style:{fontWeight:'600'}}, u.username),
                el('td', {}, u.dormitory || '-'),
                el('td', {style:{color:d209>0?'#c0392b':'#ccc', borderLeft:'2px solid #eee'}}, d209>0?d209.toFixed(2):'-'),
                el('td', {style:{color:o209>0?'#27ae60':'#ccc'}}, o209>0?o209.toFixed(2):'-'),
                el('td', {style:{color:d205>0?'#d35400':'#ccc', borderLeft:'2px solid #eee'}}, d205>0?d205.toFixed(2):'-'),
                el('td', {style:{color:o205>0?'#27ae60':'#ccc'}}, o205>0?o205.toFixed(2):'-'),
                el('td', {style:{fontWeight:'bold'}}, total!==0?total.toFixed(2):'-'),
                el('td', {style:{textAlign:'right'}},
                    el('button', {
                        class:'action-btn', style:{padding:'4px 8px',fontSize:'12px',background:'#3498db'},
                        onclick: ()=>this.openDebtModal(u.id, u.username)
                    }, 'Корр.')
                )
            );
            fragment.appendChild(tr);
        });
        this.dom.tableBody.appendChild(fragment);
    },

    async openDebtModal(userId, username) {
        const amountStr = await showPrompt(`Корректировка: ${username}`, 'Введите сумму:');
        if(amountStr === null) return;
        const amount = parseFloat(amountStr);
        if(isNaN(amount)) return toast('Введите число', 'error');

        const desc = await showPrompt('Причина', 'Основание:', 'Ручная корректировка');
        if(!desc) return;

        const accType = await showPrompt('Тип счета', 'Введите 209 (Коммуналка) или 205 (Найм):', '209');
        if(accType !== '209' && accType !== '205') return toast('Неверный тип счета', 'error');

        try {
            await api.post('/admin/adjustments', { user_id: userId, amount, description: desc, account_type: accType });
            toast('Сохранено', 'success');
            this.reload();
        } catch(e) {
            toast(e.message, 'error');
        }
    }
};