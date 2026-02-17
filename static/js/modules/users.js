// static/js/modules/users.js
import { api } from '../core/api.js';
import { el, toast, setLoading } from '../core/dom.js';
import { TableController } from '../core/table-controller.js';

export const UsersModule = {
    // Здесь будет храниться экземпляр контроллера таблицы
    table: null,
    isInitialized: false, // <--- ВАЖНО: Флаг инициализации

    init() {
        this.cacheDOM();

        // Вешаем обработчики событий ТОЛЬКО ОДИН РАЗ
        if (!this.isInitialized) {
            this.bindEvents();
            this.isInitialized = true;
        }

        // Инициализируем (или пересоздаем) таблицу при каждом заходе,
        // чтобы данные обновились
        this.initTable();
    },

    cacheDOM() {
        // Элементы управления вне таблицы (формы, кнопки импорта, кнопка обновления)
        this.dom = {
            addForm: document.getElementById('addUserForm'),
            importInput: document.getElementById('importUsersFile'),
            btnImport: document.getElementById('btnImportUsers'),
            btnRefresh: document.getElementById('btnRefreshUsers')
        };

        // Элементы модального окна редактирования
        this.modal = {
            window: document.getElementById('userEditModal'),
            form: document.getElementById('editUserForm'),
            inputs: {
                id: document.getElementById('editUserId'),
                username: document.getElementById('editUsername'),
                password: document.getElementById('editPassword'),
                role: document.getElementById('editRole'),
                dorm: document.getElementById('editDormitory'),
                area: document.getElementById('editArea'),
                residents: document.getElementById('editResidentsCount'),
                total: document.getElementById('editTotalRoomResidents'),
                work: document.getElementById('editWorkplace')
            },
            btnClose: document.querySelector('#userEditModal .close-btn')
        };
    },

    bindEvents() {
        // Обновление таблицы при нажатии кнопки Refresh
        if (this.dom.btnRefresh) {
            this.dom.btnRefresh.addEventListener('click', () => {
                if (this.table) this.table.refresh();
            });
        }

        // Обработка формы добавления пользователя
        if (this.dom.addForm) {
            this.dom.addForm.addEventListener('submit', (event) => {
                event.preventDefault();
                this.handleAdd(event);
            });
        }

        // Обработка импорта пользователей из Excel
        if (this.dom.btnImport) {
            this.dom.btnImport.addEventListener('click', (event) => {
                event.preventDefault();
                this.handleImport(this.dom.btnImport);
            });
        }

        // Обработка формы редактирования (сохранение)
        if (this.modal.form) {
            this.modal.form.addEventListener('submit', (event) => {
                event.preventDefault();
                this.handleEditSubmit(event);
            });
        }

        // Закрытие модального окна
        if (this.modal.btnClose) {
            this.modal.btnClose.addEventListener('click', () => {
                this.closeModal();
            });
        }
    },

    // Инициализация TableController для управления таблицей
    initTable() {
        // Если контроллер уже есть, можно просто обновить данные
        // Но для надежности при переключении вкладок создаем новый,
        // так как DOM таблицы мог быть перерисован
        this.table = new TableController({
            endpoint: '/users', // Базовый URL API для получения пользователей

            // Связываем контроллер с HTML-элементами из admin.html
            dom: {
                tableBody: 'usersTableBody',
                searchInput: 'usersSearchInput',
                limitSelect: 'usersLimitSelect',
                prevBtn: 'btnPrevUsers',
                nextBtn: 'btnNextUsers',
                pageInfo: 'usersPageInfo'
            },

            // Функция отрисовки одной строки таблицы (TR)
            renderRow: (user) => {
                return el('tr', { class: 'hover:bg-gray-50 transition-colors' },
                    // ID
                    el('td', { class: 'text-gray-500 text-sm' }, `#${user.id}`),

                    // Логин (жирный шрифт)
                    el('td', {},
                        el('div', { style: { fontWeight: '600' } }, user.username)
                    ),

                    // Роль (с цветным бейджем)
                    el('td', {}, el('span', { class: `role-badge ${user.role}` }, user.role)),

                    // Общежитие
                    el('td', {}, user.dormitory || '-'),

                    // Площадь (округляем до 1 знака)
                    el('td', {}, user.apartment_area ? Number(user.apartment_area).toFixed(1) : '-'),

                    // Жильцов / Всего мест
                    el('td', { class: 'text-center text-sm' }, `${user.residents_count} / ${user.total_room_residents}`),

                    // Место работы
                    el('td', {}, user.workplace || '-'),

                    // Действия (кнопки редактирования и удаления)
                    el('td', { class: 'text-center' },
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

        // Запускаем начальную загрузку данных
        this.table.init();
    },

    // ---------- ДОБАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯ ----------

    async handleAdd(event) {
        const button = this.dom.addForm.querySelector('button');

        // Сбор данных из формы добавления
        const data = {
            username: document.getElementById('newUsername').value,
            password: document.getElementById('newPassword').value,
            role: document.getElementById('newRole').value,
            dormitory: document.getElementById('dormitory').value,
            apartment_area: parseFloat(document.getElementById('area').value) || 0,
            residents_count: parseInt(document.getElementById('residentsCount').value) || 1,
            total_room_residents: parseInt(document.getElementById('totalRoomResidents').value) || 1,
            workplace: document.getElementById('workplace').value
        };

        setLoading(button, true, 'Создание...');

        try {
            await api.post('/users', data);
            toast('Пользователь успешно создан', 'success');

            // Очищаем форму
            this.dom.addForm.reset();

            // Обновляем таблицу через контроллер (подтянет новые данные с сервера)
            this.table.refresh();
        } catch (error) {
            toast(error.message, 'error');
        } finally {
            setLoading(button, false);
        }
    },

    // ---------- ИМПОРТ ИЗ EXCEL ----------

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
                alert(
                    `Импорт завершен с ошибками (${result.errors.length}):\n` +
                    result.errors.slice(0, 5).join('\n')
                );
            } else {
                toast(
                    `Добавлено: ${result.added}, Обновлено: ${result.updated}`,
                    'success'
                );
            }

            // Очищаем поле ввода файла
            this.dom.importInput.value = '';

            // Обновляем таблицу, чтобы показать изменения
            this.table.refresh();

        } catch (error) {
            toast(error.message, 'error');
        } finally {
            setLoading(button, false);
        }
    },

    // ---------- УДАЛЕНИЕ ----------

    async deleteUser(id) {
        if (!confirm('Вы действительно хотите удалить этого пользователя?')) return;

        try {
            await api.delete(`/users/${id}`);
            toast('Пользователь удален', 'success');

            // Обновляем таблицу через контроллер
            this.table.refresh();
        } catch (error) {
            toast(error.message, 'error');
        }
    },

    // ---------- РЕДАКТИРОВАНИЕ ----------

    async openEditModal(id) {
        try {
            // Загружаем актуальные данные пользователя перед открытием формы
            const user = await api.get(`/users/${id}`);
            const inputs = this.modal.inputs;

            // Заполняем поля формы
            inputs.id.value = user.id;
            inputs.username.value = user.username;
            inputs.password.value = ''; // Пароль не показываем, поле служит для его смены
            inputs.role.value = user.role;
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

        // Сбор данных из формы редактирования
        const data = {
            username: this.modal.inputs.username.value,
            role: this.modal.inputs.role.value,
            dormitory: this.modal.inputs.dorm.value,
            apartment_area: parseFloat(this.modal.inputs.area.value),
            residents_count: parseInt(this.modal.inputs.residents.value),
            total_room_residents: parseInt(this.modal.inputs.total.value),
            workplace: this.modal.inputs.work.value
        };

        // Если пароль введен, добавляем его в запрос, иначе не отправляем (чтобы не затереть старый)
        if (this.modal.inputs.password.value) {
            data.password = this.modal.inputs.password.value;
        }

        setLoading(button, true, 'Сохранение...');

        try {
            await api.put(`/users/${id}`, data);
            toast('Данные обновлены успешно', 'success');

            this.closeModal();

            // Обновляем таблицу через контроллер, чтобы увидеть изменения
            this.table.refresh();
        } catch (error) {
            toast(error.message, 'error');
        } finally {
            setLoading(button, false);
        }
    }
};