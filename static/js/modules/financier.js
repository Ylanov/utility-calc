import {api} from '../core/api.js';
import {el, toast, showPrompt, setLoading} from '../core/dom.js';

export const FinancierApp = {
    state: {
        page: 1, limit: 50, total: 0, search: '',
        importTaskId: null, pollTimer: null, isUploading: false, lastRequestId: 0
    },

    init() {
        this.cacheDOM();
        this.bindEvents();
        this.loadUsers();
    },

    cacheDOM() {
        this.dom = {
            usersTableBody: document.getElementById('usersTableBody'),
            btnRefresh: document.getElementById('btnRefreshUsers'),
            btnUpload: document.getElementById('btnUploadDebts'),
            inputUpload: document.getElementById('debtFile1C'),
            uploadResult: document.getElementById('uploadResult'),
            btnPrev: document.getElementById('btnPrevPage'),
            btnNext: document.getElementById('btnNextPage'),
            pageInfo: document.getElementById('pageInfo'),
            searchInput: document.getElementById('userSearchInput')
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
    clearPoll() { if(this.state.pollTimer) { clearTimeout(this.state.pollTimer); this.state.pollTimer = null; } },

    async handleUpload() {
        if(this.state.isUploading) return toast('Импорт уже выполняется', 'info');
        const file = this.dom.inputUpload.files[0];
        if(!file) return toast('Выберите файл .xlsx', 'error');

        // Считываем тип счета
        const accountType = document.querySelector('input[name="accountType"]:checked').value;
        if(!confirm(`Загрузить долги для счета ${accountType}?`)) return;

        this.state.isUploading = true;
        if(this.dom.uploadResult) { this.dom.uploadResult.style.display = 'none'; this.dom.uploadResult.innerHTML = ''; }
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
        const check = async () => {
            try {
                const res = await api.get(`/admin/tasks/${taskId}`);
                if(res.state === 'PENDING' || res.status === 'processing') {
                    this.state.pollTimer = setTimeout(check, 2000);
                    return;
                }
                if(res.status === 'done' || res.state === 'SUCCESS') {
                    this.renderUploadResult(res.result || res);
                    toast('Импорт завершен!', 'success');
                    this.reload();
                    return;
                }
                if(res.state === 'FAILURE') throw new Error(res.error || 'Ошибка воркера');
                throw new Error('Неизвестный статус');
            } catch(e) {
                toast('Ошибка задачи: ' + e.message, 'error');
                if(this.dom.uploadResult) {
                    this.dom.uploadResult.style.display = 'block';
                    this.dom.uploadResult.innerHTML = `<div style="color:red">Сбой: ${e.message}</div>`;
                }
            } finally {
                if(!this.state.pollTimer) {
                    this.state.isUploading = false;
                    setLoading(this.dom.btnUpload, false, '⬆ Загрузить');
                }
            }
        };
        check();
    },

    renderUploadResult(res) {
        if(!this.dom.uploadResult || !res) return;
        let html = `<div style="padding:15px;background:#e8f5e9;color:#2e7d32;border-radius:6px;border:1px solid #c8e6c9">
            <h4 style="margin:0 0 10px 0">✅ Импорт завершен (Счет ${res.account || '?'})</h4>
            <ul style="margin:0;padding-left:20px"><li>Обработано: <strong>${res.processed}</strong></li><li>Обновлено: <strong>${res.updated}</strong></li><li>Создано: <strong>${res.created}</strong></li></ul></div>`;
        if(res.not_found_users && res.not_found_users.length) {
            html += `<div style="margin-top:15px;padding:15px;background:#ffebee;color:#c62828;border-radius:6px;border:1px solid #ffcdd2">
                <h4>⚠️ Не найдены (${res.not_found_users.length})</h4>
                <div style="max-height:100px;overflow:auto;font-size:13px;background:rgba(255,255,255,.5);padding:5px">${res.not_found_users.join('<br>')}</div></div>`;
        }
        this.dom.uploadResult.innerHTML = html;
        this.dom.uploadResult.style.display = 'block';
    },

    async loadUsers() {
        if(!this.dom.usersTableBody) return;
        const requestId = ++this.state.lastRequestId;
        this.dom.usersTableBody.innerHTML = '<tr><td colspan="9" style="text-align:center;padding:20px">Загрузка...</td></tr>';

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
            this.dom.usersTableBody.innerHTML = `<tr><td colspan="9" style="color:red;text-align:center">${e.message}</td></tr>`;
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
        this.dom.usersTableBody.innerHTML = '';
        if(!users || !users.length) {
            this.dom.usersTableBody.innerHTML = '<tr><td colspan="9" style="text-align:center;padding:20px">Нет данных</td></tr>';
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
                
                // Счет 209 (Коммуналка)
                el('td', {style:{color:d209>0?'#c0392b':'#ccc', borderLeft:'2px solid #eee'}}, d209>0?d209.toFixed(2):'-'),
                el('td', {style:{color:o209>0?'#27ae60':'#ccc'}}, o209>0?o209.toFixed(2):'-'),
                
                // Счет 205 (Найм)
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
        this.dom.usersTableBody.appendChild(fragment);
    },

    async openDebtModal(userId, username) {
        const amountStr = await showPrompt(`Корректировка: ${username}`, 'Введите сумму:');
        if(amountStr === null) return;
        const amount = parseFloat(amountStr);
        if(isNaN(amount)) return toast('Введите число', 'error');

        const desc = await showPrompt('Причина', 'Основание:', 'Ручная корректировка');
        if(!desc) return;

        // Новый шаг: выбор счета
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