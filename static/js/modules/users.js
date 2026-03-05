// static/js/modules/users.js
import { api } from '../core/api.js';
import { el, toast, setLoading } from '../core/dom.js';
import { TableController } from '../core/table-controller.js';

export const UsersModule = {
    table: null,
    isInitialized: false,
    tariffs: [], // Хранилище загруженных тарифов

    async init() {
        this.cacheDOM();

        if (!this.isInitialized) {
            this.bindEvents();
            this.isInitialized = true;
        }

        // 1. Сначала загружаем список тарифов, чтобы заполнить выпадающие списки
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

        // ДОБАВЛЕНО: Элементы модалки Разового начисления (Выселение/Переезд)
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

        // ДОБАВЛЕНО: Обработчики для модалки Разового начисления
        if (this.otc.form) {
            this.otc.form.addEventListener('submit', (e) => this.handleOneTimeSubmit(e));
            this.otc.btnClose.addEventListener('click', (e) => { e.preventDefault(); this.otc.modal.classList.remove('open'); });
            this.otc.btnCancel.addEventListener('click', (e) => { e.preventDefault(); this.otc.modal.classList.remove('open'); });
        }
    },

    // Загрузка и рендер тарифов
    async loadTariffs() {
        try {
            this.tariffs = await api.get('/tariffs');
            this.populateTariffSelects();
        } catch (error) {
            console.error('Ошибка загрузки профилей тарифов:', error);
            toast('Не удалось загрузить список тарифов', 'error');
        }
    },

    populateTariffSelects() {
        // Формируем HTML для опций
        const optionsHtml = '<option value="">Базовый тариф (По умолчанию)</option>' +
            this.tariffs.map(t => `<option value="${t.id}">${t.name}</option>`).join('');

        // Вставляем в форму создания
        if (this.dom.newTariffSelect) {
            this.dom.newTariffSelect.innerHTML = optionsHtml;
        }

        // Вставляем в форму редактирования
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
                return el('tr', { class: 'hover:bg-gray-50 transition-colors' },
                    el('td', { class: 'text-gray-500 text-sm' }, `#${user.id}`),
                    el('td', {}, el('div', { style: { fontWeight: '600' } }, user.username)),
                    el('td', {}, el('span', { class: `role-badge ${user.role}` }, user.role)),
                    el('td', {}, user.dormitory || '-'),
                    el('td', {}, user.apartment_area ? Number(user.apartment_area).toFixed(1) : '-'),
                    el('td', { class: 'text-center text-sm' }, `${user.residents_count} / ${user.total_room_residents}`),
                    el('td', {}, user.workplace || '-'),
                    el('td', { class: 'text-center' },
                        // ДОБАВЛЕНО: Кнопка Разового начисления (Песочные часы)
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

        // Читаем выбранный тариф
        const tariffIdVal = this.dom.newTariffSelect.value;

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

            if (result.errors && result.errors.length > 0) {
                alert(`Импорт завершен с ошибками (${result.errors.length}):\n` + result.errors.slice(0, 5).join('\n'));
            } else {
                toast(`Добавлено: ${result.added}, Обновлено: ${result.updated}`, 'success');
            }

            this.dom.importInput.value = '';
            this.table.refresh();

        } catch (error) {
            toast(error.message, 'error');
        } finally {
            setLoading(button, false);
        }
    },

    async deleteUser(id) {
        if (!confirm('Вы действительно хотите удалить этого пользователя?')) return;

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
            inputs.dorm.value = user.dormitory || '';
            inputs.area.value = user.apartment_area;
            inputs.residents.value = user.residents_count;
            inputs.total.value = user.total_room_residents;
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

    // ДОБАВЛЕНО: Функции для модалки Разового начисления
    async openOneTimeModal(user) {
        if (!this.otc.modal) return;

        this.otc.userId.value = user.id;
        this.otc.userName.textContent = `${user.username} (${user.dormitory || 'без адреса'})`;

        // Автоматически считаем дни в текущем месяце
        const date = new Date();
        const totalDays = new Date(date.getFullYear(), date.getMonth() + 1, 0).getDate();
        this.otc.totalDays.value = totalDays;
        this.otc.daysLived.value = date.getDate(); // По умолчанию прожил до сегодняшнего дня

        this.otc.hot.value = '';
        this.otc.cold.value = '';
        this.otc.elect.value = '';
        this.otc.isMovingOut.checked = false;

        this.otc.hot.placeholder = `Загрузка...`;
        this.otc.cold.placeholder = `Загрузка...`;
        this.otc.elect.placeholder = `Загрузка...`;

        this.otc.modal.classList.add('open');

        // Пытаемся подгрузить предыдущие показания, чтобы админ не ошибся
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
            if (!confirm('ВНИМАНИЕ! Вы выбрали выселение. Пользователь будет помечен как удаленный, а его логин освободится. Продолжить?')) return;
        }

        setLoading(this.otc.btnSubmit, true, 'Расчет...');
        try {
            await api.post('/admin/readings/one-time', payload);
            toast('Разовое начисление успешно проведено и утверждено!', 'success');
            this.otc.modal.classList.remove('open');
            this.table.refresh(); // Обновляем таблицу (юзер может пропасть, если выселен)
        } catch (err) {
            toast(err.message, 'error');
        } finally {
            setLoading(this.otc.btnSubmit, false, 'Сформировать квитанцию');
        }
    }
};