// static/js/modules/users.js
import { api } from '../core/api.js';
import { el, clear, setLoading, toast } from '../core/dom.js';

export const UsersModule = {

    init() {
        this.cacheDOM();
        this.bindEvents();
        this.load();
    },

    cacheDOM() {
        this.dom = {
            tbody: document.getElementById('usersTableBody'),
            btnRefresh: document.getElementById('btnRefreshUsers'),
            addForm: document.getElementById('addUserForm'),
            importInput: document.getElementById('importUsersFile'),
            btnImport: document.getElementById('btnImportUsers')
        };

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

        if (this.dom.btnRefresh) {
            this.dom.btnRefresh.addEventListener('click', () => {
                this.load();
            });
        }

        if (this.dom.addForm) {
            this.dom.addForm.addEventListener('submit', (event) => {
                event.preventDefault();
                this.handleAdd(event);
            });
        }

        // ✅ НОРМАЛЬНАЯ ПРИВЯЗКА КНОПКИ ИМПОРТА
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
    },

    async load() {
        this.dom.tbody.innerHTML =
            '<tr><td colspan="8" class="text-center">Загрузка...</td></tr>';

        try {
            const users = await api.get('/users');
            this.renderTable(users);
        } catch (error) {
            this.dom.tbody.innerHTML =
                `<tr><td colspan="8" class="text-danger">${error.message}</td></tr>`;
        }
    },

    renderTable(users) {
        clear(this.dom.tbody);

        if (!users.length) {
            this.dom.tbody.innerHTML =
                '<tr><td colspan="8" class="text-center">Нет пользователей</td></tr>';
            return;
        }

        const fragment = document.createDocumentFragment();

        users.forEach(user => {
            const row = el('tr', {},
                el('td', {}, String(user.id)),
                el('td', {}, el('strong', {}, user.username)),
                el('td', {}, el('span', {
                    class: `role-badge ${user.role}`
                }, user.role)),
                el('td', {}, user.dormitory || '-'),
                el('td', {}, Number(user.apartment_area).toFixed(1)),
                el('td', {}, `${user.residents_count} / ${user.total_room_residents}`),
                el('td', {}, user.workplace || '-'),
                el('td', {},
                    el('button', {
                        class: 'btn-icon btn-edit',
                        title: 'Редактировать',
                        onclick: () => this.openEditModal(user.id)
                    }, '✎'),
                    el('button', {
                        class: 'btn-icon btn-delete',
                        title: 'Удалить',
                        onclick: () => this.deleteUser(user.id)
                    }, '🗑')
                )
            );

            fragment.appendChild(row);
        });

        this.dom.tbody.appendChild(fragment);
    },

    // ---------- ДОБАВЛЕНИЕ ----------

    async handleAdd(event) {
        const button = this.dom.addForm.querySelector('button');

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
            toast('Пользователь создан', 'success');
            this.dom.addForm.reset();
            this.load();
        } catch (error) {
            toast(error.message, 'error');
        } finally {
            setLoading(button, false);
        }
    },

    // ---------- ИМПОРТ ----------

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

            this.dom.importInput.value = '';
            this.load();

        } catch (error) {
            toast(error.message, 'error');
        } finally {
            setLoading(button, false);
        }
    },

    // ---------- РЕДАКТИРОВАНИЕ ----------

    async openEditModal(id) {
        try {
            const user = await api.get(`/users/${id}`);

            const inputs = this.modal.inputs;

            inputs.id.value = user.id;
            inputs.username.value = user.username;
            inputs.password.value = '';
            inputs.role.value = user.role;
            inputs.dorm.value = user.dormitory || '';
            inputs.area.value = user.apartment_area;
            inputs.residents.value = user.residents_count;
            inputs.total.value = user.total_room_residents;
            inputs.work.value = user.workplace || '';

            this.modal.window.classList.add('open');

        } catch (error) {
            toast('Ошибка загрузки: ' + error.message, 'error');
        }
    },

    closeModal() {
        this.modal.window.classList.remove('open');
    },

    async handleEditSubmit(event) {
        const button = this.modal.form.querySelector('.confirm-btn');
        const id = this.modal.inputs.id.value;

        const data = {
            username: this.modal.inputs.username.value,
            role: this.modal.inputs.role.value,
            dormitory: this.modal.inputs.dorm.value,
            apartment_area: parseFloat(this.modal.inputs.area.value),
            residents_count: parseInt(this.modal.inputs.residents.value),
            total_room_residents: parseInt(this.modal.inputs.total.value),
            workplace: this.modal.inputs.work.value
        };

        if (this.modal.inputs.password.value) {
            data.password = this.modal.inputs.password.value;
        }

        setLoading(button, true, 'Сохранение...');

        try {
            await api.put(`/users/${id}`, data);
            toast('Обновлено успешно', 'success');
            this.closeModal();
            this.load();
        } catch (error) {
            toast(error.message, 'error');
        } finally {
            setLoading(button, false);
        }
    },

    // ---------- УДАЛЕНИЕ ----------

    async deleteUser(id) {
        if (!confirm('Удалить пользователя?')) return;

        try {
            await api.delete(`/users/${id}`);
            toast('Пользователь удален', 'success');
            this.load();
        } catch (error) {
            toast(error.message, 'error');
        }
    }
};
