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
            // Кнопка импорта (ищем по соседству с инпутом)
            btnImport: document.querySelector('button[onclick="importUsers()"]')
                       || document.getElementById('btnImportUsers')
                       // Если в HTML кнопка без ID, добавим обработчик динамически ниже
        };

        // Modal elements
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
        if (this.dom.btnRefresh) this.dom.btnRefresh.addEventListener('click', () => this.load());
        if (this.dom.addForm) this.dom.addForm.addEventListener('submit', (e) => this.handleAdd(e));

        // Обработка кнопки импорта (костыль для старой верстки, если там onclick)
        const importBtn = document.querySelector('.card button[onclick="importUsers()"]');
        if (importBtn) {
            importBtn.removeAttribute('onclick');
            importBtn.addEventListener('click', () => this.handleImport(importBtn));
        }

        // Модалка редактирования
        if (this.modal.form) this.modal.form.addEventListener('submit', (e) => this.handleEditSubmit(e));
        if (this.modal.btnClose) this.modal.btnClose.addEventListener('click', () => this.closeModal());
    },

    async load() {
        this.dom.tbody.innerHTML = '<tr><td colspan="8" class="text-center">Загрузка...</td></tr>';

        try {
            const users = await api.get('/users');
            this.renderTable(users);
        } catch (e) {
            this.dom.tbody.innerHTML = `<tr><td colspan="8" class="text-danger">${e.message}</td></tr>`;
        }
    },

    renderTable(users) {
        this.dom.tbody.innerHTML = '';

        if (!users.length) {
            this.dom.tbody.innerHTML = '<tr><td colspan="8" class="text-center">Нет пользователей</td></tr>';
            return;
        }

        const fragment = document.createDocumentFragment();

        users.forEach(u => {
            const tr = el('tr', {},
                el('td', {}, String(u.id)),
                el('td', {}, el('strong', {}, u.username)),
                el('td', {}, el('span', { class: `role-badge ${u.role}` }, u.role)),
                el('td', {}, u.dormitory || '-'),
                el('td', {}, Number(u.apartment_area).toFixed(1)),
                el('td', {}, `${u.residents_count} / ${u.total_room_residents}`),
                el('td', {}, u.workplace || '-'),
                el('td', {},
                    el('button', {
                        class: 'btn-icon btn-edit',
                        title: 'Редактировать',
                        onclick: () => this.openEditModal(u.id)
                    }, '✎'),
                    el('button', {
                        class: 'btn-icon btn-delete',
                        title: 'Удалить',
                        onclick: () => this.deleteUser(u.id)
                    }, '🗑')
                )
            );
            fragment.appendChild(tr);
        });

        this.dom.tbody.appendChild(fragment);
    },

    // --- ДОБАВЛЕНИЕ ---

    async handleAdd(e) {
        e.preventDefault();
        const btn = this.dom.addForm.querySelector('button');

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

        setLoading(btn, true, 'Создание...');

        try {
            await api.post('/users', data);
            toast('Пользователь создан', 'success');
            this.dom.addForm.reset();
            this.load();
        } catch (e) {
            toast(e.message, 'error');
        } finally {
            setLoading(btn, false);
        }
    },

    // --- ИМПОРТ ---

    async handleImport(btn) {
        const file = this.dom.importInput.files[0];
        if (!file) {
            toast('Выберите файл .xlsx', 'info');
            return;
        }

        // Проверка расширения на клиенте
        if (!file.name.match(/\.(xlsx|xls)$/)) {
            toast('Только файлы Excel!', 'error');
            return;
        }

        const formData = new FormData();
        formData.append('file', file);

        setLoading(btn, true, 'Загрузка...');

        try {
            const res = await api.post('/users/import_excel', formData);

            // Если есть ошибки, покажем их, но не будем прерывать успех
            if (res.errors && res.errors.length > 0) {
                alert(`Импорт завершен с ошибками (${res.errors.length}):\n` + res.errors.slice(0, 5).join('\n') + '...');
            } else {
                toast(`Добавлено: ${res.added}, Обновлено: ${res.updated}`, 'success');
            }

            this.dom.importInput.value = '';
            this.load();
        } catch (e) {
            toast(e.message, 'error');
        } finally {
            setLoading(btn, false);
        }
    },

    // --- РЕДАКТИРОВАНИЕ ---

    async openEditModal(id) {
        try {
            const u = await api.get(`/users/${id}`);

            const i = this.modal.inputs;
            i.id.value = u.id;
            i.username.value = u.username;
            i.password.value = ''; // Пароль не показываем
            i.role.value = u.role;
            i.dorm.value = u.dormitory || '';
            i.area.value = u.apartment_area;
            i.residents.value = u.residents_count;
            i.total.value = u.total_room_residents;
            i.work.value = u.workplace || '';

            this.modal.window.classList.add('open');
        } catch (e) {
            toast('Ошибка загрузки данных: ' + e.message, 'error');
        }
    },

    closeModal() {
        this.modal.window.classList.remove('open');
    },

    async handleEditSubmit(e) {
        e.preventDefault();
        const btn = this.modal.form.querySelector('.confirm-btn');
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

        setLoading(btn, true, 'Сохранение...');

        try {
            await api.put(`/users/${id}`, data);
            toast('Обновлено успешно', 'success');
            this.closeModal();
            this.load();
        } catch (e) {
            toast(e.message, 'error');
        } finally {
            setLoading(btn, false);
        }
    },

    // --- УДАЛЕНИЕ ---

    async deleteUser(id) {
        if (!confirm('Удалить пользователя? Все его показания тоже будут удалены.')) return;

        try {
            await api.delete(`/users/${id}`);
            toast('Пользователь удален', 'success');
            // Удаляем строку из таблицы без перезагрузки всей таблицы (для скорости)
            // Но проще перезагрузить load(), чтобы ID обновились корректно
            this.load();
        } catch (e) {
            toast(e.message, 'error');
        }
    }
};