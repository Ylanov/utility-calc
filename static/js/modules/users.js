// static/js/modules/users.js
import { api } from '../core/api.js';
import { el, toast, setLoading } from '../core/dom.js';
import { TableController } from '../core/table-controller.js';

export const UsersModule = {
    table: null,
    isInitialized: false,
    tariffs:[], // Хранилище загруженных тарифов

    async init() {
        this.cacheDOM();

        if (!this.isInitialized) {
            this.bindEvents();
            this.isInitialized = true;
        }

        // 1. Сначала загружаем список тарифов (с кэшированием)
        await this.loadTariffs();

        // 2. Затем инициализируем таблицу пользователей
        this.initTable();
    },

    cacheDOM() {
        this.dom = {
            addForm: document.getElementById('addUserForm'),
            importInput: document.getElementById('importUsersFile'),
            btnImport: document.getElementById('btnImportUsers'),
            btnRefresh: document.getElementById('btnRefreshUsers'),
            newTariffSelect: document.getElementById('newTariffId') // Селект тарифа при создании
        };

        this.modal = {
            window: document.getElementById('userEditModal'),
            form: document.getElementById('editUserForm'),
            inputs: {
                id: document.getElementById('editUserId'),
                username: document.getElementById('editUsername'),
                password: document.getElementById('editPassword'),
                role: document.getElementById('editRole'),
                tariff: document.getElementById('editTariffId'), // Селект тарифа при редактировании
                dorm: document.getElementById('editDormitory'),
                area: document.getElementById('editArea'),
                residents: document.getElementById('editResidentsCount'),
                total: document.getElementById('editTotalRoomResidents'),
                work: document.getElementById('editWorkplace')
            },
            btnClose: document.querySelector('#userEditModal .close-btn')
        };

        this.otc = {
            modal: document.getElementById('oneTimeChargeModal'),
            form: document.getElementById('oneTimeChargeForm'),
            userId: document.getElementById('otcUserId'),
            userName: document.getElementById('otcUserName'),
            totalDays: document.getElementById('otcTotalDays'),
            daysLived: document.getElementById('otcDaysLived'),
            hot: document.getElementById('otcHot'),
            cold: document.getElementById('otcCold'),
            elect: document.getElementById('otcElect'),
            isMovingOut: document.getElementById('otcIsMovingOut'),
            btnClose: document.getElementById('btnCloseOneTime'),
            btnCancel: document.getElementById('btnCancelOneTime'),
            btnSubmit: document.getElementById('btnSubmitOneTime')
        };
    },

    bindEvents() {
        if (this.dom.btnRefresh) {
            this.dom.btnRefresh.addEventListener('click', () => {
                if (this.table) this.table.refresh();
            });
        }

        if (this.dom.addForm) {
            this.dom.addForm.addEventListener('submit', (event) => {
                event.preventDefault();
                this.handleAdd(event);
            });
        }

        if (this.dom.btnImport) {
            this.dom.btnImport.addEventListener('click', (event) => {
                event.preventDefault();
                this.handleImport(this.dom.btnImport);
            });
        }

        if (this.modal.form) {
            this.modal.form.addEventListener('submit', (event) => {
                event.preventDefault();
                this.handleEditSubmit(event);
            });
        }

        if (this.modal.btnClose) {
            this.modal.btnClose.addEventListener('click', () => {
                this.closeModal();
            });
        }

        if (this.otc.form) {
            this.otc.form.addEventListener('submit', (e) => this.handleOneTimeSubmit(e));
            this.otc.btnClose.addEventListener('click', (e) => { e.preventDefault(); this.otc.modal.classList.remove('open'); });
            this.otc.btnCancel.addEventListener('click', (e) => { e.preventDefault(); this.otc.modal.classList.remove('open'); });
        }
    },

    // ОПТИМИЗАЦИЯ: Кэширование тарифов в браузере
    async loadTariffs() {
        try {
            const cached = sessionStorage.getItem('tariffs_cache');
            if (cached) {
                this.tariffs = JSON.parse(cached);
                this.populateTariffSelects();
                return; // Берем из кэша, экономим запрос к БД
            }

            this.tariffs = await api.get('/tariffs');
            sessionStorage.setItem('tariffs_cache', JSON.stringify(this.tariffs));
            this.populateTariffSelects();
        } catch (error) {
            console.error('Ошибка загрузки профилей тарифов:', error);
            toast('Не удалось загрузить список тарифов', 'error');
        }
    },

    populateTariffSelects() {
        const optionsHtml = '<option value="">Базовый тариф (По умолчанию)</option>' +
            this.tariffs.map(t => `<option value="${t.id}">${t.name}</option>`).join('');

        if (this.dom.newTariffSelect) {
            this.dom.newTariffSelect.innerHTML = optionsHtml;
        }
        if (this.modal.inputs.tariff) {
            this.modal.inputs.tariff.innerHTML = optionsHtml;
        }
    },

    initTable() {
        this.table = new TableController({
            endpoint: '/users',

            dom: {
                tableBody: 'usersTableBody',
                searchInput: 'usersSearchInput',
                limitSelect: 'usersLimitSelect',
                prevBtn: 'btnPrevUsers',
                nextBtn: 'btnNextUsers',
                pageInfo: 'usersPageInfo'
            },

            renderRow: (user) => {
                // ИЗМЕНЕНИЕ: Безопасно извлекаем данные из объекта room (если он есть)
                const address = user.room ? `${user.room.dormitory_name} / ком. ${user.room.room_number}` : '-';
                const area = user.room && user.room.apartment_area ? Number(user.room.apartment_area).toFixed(1) : '-';
                const totalResidents = user.room ? user.room.total_room_residents : 1;

                return el('tr', { class: 'hover:bg-gray-50 transition-colors' },
                    el('td', { class: 'text-gray-500 text-sm' }, `#${user.id}`),
                    el('td', {}, el('div', { style: { fontWeight: '600' } }, user.username)),
                    el('td', {}, el('span', { class: `role-badge ${user.role}` }, user.role)),
                    el('td', {}, address), // <-- Используем склеенный адрес
                    el('td', {}, area),    // <-- Используем площадь из комнаты
                    el('td', { class: 'text-center text-sm' }, `${user.residents_count} / ${totalResidents}`), // <-- Берем из комнаты
                    el('td', {}, user.workplace || '-'),
                    el('td', { class: 'text-center' },
                        el('button', {
                            class: 'btn-icon',
                            title: 'Разовое начисление / Выселение',
                            style: { marginRight: '5px', background: '#fef3c7', color: '#d97706', borderColor: '#fde68a' },
                            onclick: () => this.openOneTimeModal(user)
                        }, '⏳'),
                        el('button', {
                            class: 'btn-icon btn-edit',
                            title: 'Редактировать',
                            style: { marginRight: '5px' },
                            onclick: () => this.openEditModal(user.id)
                        }, '✎'),
                        el('button', {
                            class: 'btn-icon btn-delete',
                            title: 'Удалить',
                            onclick: () => this.deleteUser(user.id)
                        }, '🗑')
                    )
                );
            }
        });

        this.table.init();
    },

    async handleAdd(event) {
        const button = this.dom.addForm.querySelector('button');
        const tariffIdVal = this.dom.newTariffSelect.value;

        // Данные формы не меняются, бэкенд сам разобьет dormitory на название и номер комнаты
        const data = {
            username: document.getElementById('newUsername').value.trim(),
            password: document.getElementById('newPassword').value,
            role: document.getElementById('newRole').value,
            tariff_id: tariffIdVal ? parseInt(tariffIdVal) : null,
            dormitory: document.getElementById('dormitory').value.trim(),
            apartment_area: parseFloat(document.getElementById('area').value) || 0,
            residents_count: parseInt(document.getElementById('residentsCount').value) || 1,
            total_room_residents: parseInt(document.getElementById('totalRoomResidents').value) || 1,
            workplace: document.getElementById('workplace').value.trim()
        };

        setLoading(button, true, 'Создание...');

        try {
            await api.post('/users', data);
            toast('Пользователь успешно создан', 'success');
            this.dom.addForm.reset();
            this.table.refresh();
        } catch (error) {
            toast(error.message, 'error');
        } finally {
            setLoading(button, false);
        }
    },

    async handleImport(button) {
        const file = this.dom.importInput.files[0];

        if (!file) {
            toast('Выберите файл Excel', 'info');
            return;
        }

        if (!file.name.match(/\.(xlsx|xls)$/)) {
            toast('Разрешены только файлы Excel (.xlsx, .xls)', 'error');
            return;
        }

        const formData = new FormData();
        formData.append('file', file);

        setLoading(button, true, 'Загрузка...');

        try {
            const result = await api.post('/users/import_excel', formData);

            // UX УЛУЧШЕНИЕ: Вызываем красивую модалку вместо alert()
            this.showImportResultModal(result);

            this.dom.importInput.value = '';
            this.table.refresh();

        } catch (error) {
            toast(error.message, 'error');
        } finally {
            setLoading(button, false);
        }
    },

    // Динамическое создание модалки для показа результатов импорта 20к жильцов
    showImportResultModal(result) {
        const hasErrors = result.errors && result.errors.length > 0;

        const overlay = el('div', { class: 'modal-overlay open', style: { zIndex: 10000 } });
        const headerTitle = hasErrors ? '⚠️ Результат импорта (Есть ошибки)' : '✅ Импорт успешно завершен';
        const headerColor = hasErrors ? '#d97706' : '#059669';

        const closeBtn = el('button', { class: 'close-icon' }, '×');
        closeBtn.onclick = () => document.body.removeChild(overlay);

        const content = el('div', { class: 'modal-form' },
            el('ul', { style: { marginBottom: '15px', paddingLeft: '20px', fontSize: '15px', color: '#374151' } },
                el('li', { style: { marginBottom: '5px' } }, `Добавлено новых жильцов: `, el('strong', { style: { color: '#059669'} }, String(result.added))),
                el('li', {}, `Обновлено существующих: `, el('strong', { style: { color: '#2563eb'} }, String(result.updated)))
            )
        );

        if (hasErrors) {
            // Контейнер с прокруткой для ошибок
            const errorBox = el('div', {
                style: {
                    maxHeight: '250px', overflowY: 'auto', background: '#fef2f2',
                    border: '1px solid #fecaca', borderRadius: '8px', padding: '12px',
                    fontSize: '13px', color: '#991b1b', fontFamily: 'monospace'
                }
            });

            result.errors.forEach(err => {
                errorBox.appendChild(el('div', {
                    style: { marginBottom: '6px', borderBottom: '1px dashed #fca5a5', paddingBottom: '6px' }
                }, String(err))); // String() + el() защищает от XSS
            });

            content.appendChild(el('h4', { style: { marginBottom: '10px', color: '#dc2626', fontSize: '14px' } }, `Ошибки (${result.errors.length}):`));
            content.appendChild(errorBox);
        }

        const btnOk = el('button', { class: 'action-btn primary-btn full-width', style: { marginTop: '20px' } }, 'Понятно, закрыть');
        btnOk.onclick = () => document.body.removeChild(overlay);
        content.appendChild(btnOk);

        const modalWindow = el('div', { class: 'modal-window', style: { width: '550px' } },
            el('div', { class: 'modal-header' },
                el('h3', { style: { color: headerColor } }, headerTitle),
                closeBtn
            ),
            content
        );

        overlay.appendChild(modalWindow);
        document.body.appendChild(overlay); // Вставляем в DOM на лету
    },

    async deleteUser(id) {
        if (!confirm('Вы действительно хотите удалить этого пользователя? (Будет произведено мягкое удаление. История показаний сохранится за комнатой.)')) return;

        try {
            await api.delete(`/users/${id}`);
            toast('Пользователь удален', 'success');
            this.table.refresh();
        } catch (error) {
            toast(error.message, 'error');
        }
    },

    async openEditModal(id) {
        try {
            const user = await api.get(`/users/${id}`);
            const inputs = this.modal.inputs;

            inputs.id.value = user.id;
            inputs.username.value = user.username;
            inputs.password.value = '';
            inputs.role.value = user.role;
            inputs.tariff.value = user.tariff_id || '';

            // ИЗМЕНЕНИЕ: Безопасно заполняем поля модалки из объекта room
            if (user.room) {
                // Склеиваем обратно для формы, бэкенд разделит по последнему пробелу
                inputs.dorm.value = `${user.room.dormitory_name} ${user.room.room_number}`.trim();
                inputs.area.value = user.room.apartment_area;
                inputs.total.value = user.room.total_room_residents;
            } else {
                inputs.dorm.value = '';
                inputs.area.value = 0;
                inputs.total.value = 1;
            }

            inputs.residents.value = user.residents_count;
            inputs.work.value = user.workplace || '';

            this.modal.window.classList.add('open');

        } catch (error) {
            toast('Ошибка загрузки данных пользователя: ' + error.message, 'error');
        }
    },

    closeModal() {
        this.modal.window.classList.remove('open');
    },

    async handleEditSubmit(event) {
        const button = this.modal.form.querySelector('.confirm-btn');
        const id = this.modal.inputs.id.value;
        const tariffIdVal = this.modal.inputs.tariff.value;

        const data = {
            username: this.modal.inputs.username.value.trim(),
            role: this.modal.inputs.role.value,
            tariff_id: tariffIdVal ? parseInt(tariffIdVal) : null,
            dormitory: this.modal.inputs.dorm.value.trim(),
            apartment_area: parseFloat(this.modal.inputs.area.value),
            residents_count: parseInt(this.modal.inputs.residents.value),
            total_room_residents: parseInt(this.modal.inputs.total.value),
            workplace: this.modal.inputs.work.value.trim()
        };

        if (this.modal.inputs.password.value) {
            data.password = this.modal.inputs.password.value;
        }

        setLoading(button, true, 'Сохранение...');

        try {
            await api.put(`/users/${id}`, data);
            toast('Данные обновлены успешно', 'success');
            this.closeModal();
            this.table.refresh();
        } catch (error) {
            toast(error.message, 'error');
        } finally {
            setLoading(button, false);
        }
    },

    async openOneTimeModal(user) {
        if (!this.otc.modal) return;

        // ИЗМЕНЕНИЕ: Выводим правильный адрес
        const address = user.room ? `${user.room.dormitory_name} ком. ${user.room.room_number}` : 'без адреса';

        this.otc.userId.value = user.id;
        this.otc.userName.textContent = `${user.username} (${address})`;

        const date = new Date();
        const totalDays = new Date(date.getFullYear(), date.getMonth() + 1, 0).getDate();
        this.otc.totalDays.value = totalDays;
        this.otc.daysLived.value = date.getDate();

        this.otc.hot.value = '';
        this.otc.cold.value = '';
        this.otc.elect.value = '';
        this.otc.isMovingOut.checked = false;

        this.otc.hot.placeholder = `Загрузка...`;
        this.otc.cold.placeholder = `Загрузка...`;
        this.otc.elect.placeholder = `Загрузка...`;

        this.otc.modal.classList.add('open');

        try {
            const state = await api.get(`/admin/readings/manual-state/${user.id}`);
            this.otc.hot.placeholder = `Пред: ${state.prev_hot}`;
            this.otc.cold.placeholder = `Пред: ${state.prev_cold}`;
            this.otc.elect.placeholder = `Пред: ${state.prev_elect}`;
        } catch (e) {
            console.warn(e);
            this.otc.hot.placeholder = `Ошибка`;
            this.otc.cold.placeholder = `Ошибка`;
            this.otc.elect.placeholder = `Ошибка`;
        }
    },

    async handleOneTimeSubmit(e) {
        e.preventDefault();

        const payload = {
            user_id: parseInt(this.otc.userId.value),
            total_days_in_month: parseInt(this.otc.totalDays.value),
            days_lived: parseInt(this.otc.daysLived.value),
            hot_water: parseFloat(this.otc.hot.value),
            cold_water: parseFloat(this.otc.cold.value),
            electricity: parseFloat(this.otc.elect.value),
            is_moving_out: this.otc.isMovingOut.checked
        };

        if (payload.is_moving_out) {
            if (!confirm('ВНИМАНИЕ! Вы выбрали выселение. Пользователь будет помечен как удаленный, комната будет освобождена. Продолжить?')) return;
        }

        setLoading(this.otc.btnSubmit, true, 'Расчет...');
        try {
            await api.post('/admin/readings/one-time', payload);
            toast('Разовое начисление успешно проведено и утверждено!', 'success');
            this.otc.modal.classList.remove('open');
            this.table.refresh();
        } catch (err) {
            toast(err.message, 'error');
        } finally {
            setLoading(this.otc.btnSubmit, false, 'Сформировать квитанцию');
        }
    }
};