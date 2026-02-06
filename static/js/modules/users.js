// static/js/modules/users.js
import { api } from '../core/api.js';
import { el, clear, setLoading } from '../core/dom.js';

export const UsersModule = {
    // ============================================================
    // ИНИЦИАЛИЗАЦИЯ
    // ============================================================
    init() {
        // --- Кнопка обновления таблицы ---
        const btnRefresh = document.getElementById('btnRefreshUsers');
        if (btnRefresh) {
            // Удаляем старые листенеры через клонирование (если были) или просто вешаем новый
            // Так как init запускается один раз, просто вешаем:
            btnRefresh.addEventListener('click', () => this.load());
        }

        // --- Форма добавления пользователя ---
        const addUserForm = document.getElementById('addUserForm');
        if (addUserForm) {
            addUserForm.addEventListener('submit', (e) => this.submitAddUser(e));
        }

        // --- Форма редактирования пользователя (в модальном окне) ---
        const editUserForm = document.getElementById('editUserForm');
        if (editUserForm) {
            editUserForm.addEventListener('submit', (e) => this.submitEditUser(e));
        }

        // --- Кнопки закрытия модального окна ---
        // Ищем по классу close-btn внутри модалки редактирования
        const editModal = document.getElementById('userEditModal');
        if (editModal) {
            const closeBtns = editModal.querySelectorAll('.close-btn');
            closeBtns.forEach(btn => btn.addEventListener('click', () => this.closeEditModal()));
        }

        // --- Импорт Excel ---
        // Ищем кнопку по onclick="importUsers()" или добавляем ID в HTML
        // Для надежности лучше добавить ID в HTML, но пока найдем по селектору кнопки рядом с инпутом
        const importBtn = document.querySelector('button[onclick="importUsers()"]');
        if (importBtn) {
            // Убираем старый атрибут onclick, чтобы не двоилось
            importBtn.removeAttribute('onclick');
            importBtn.addEventListener('click', () => this.importUsers(importBtn));
        }

        // Первичная загрузка
        this.load();
    },

    // ============================================================
    // ЗАГРУЗКА И ОТОБРАЖЕНИЕ
    // ============================================================
    async load() {
        const tbody = clear('usersTableBody'); // Убедись, что в HTML у tbody есть этот ID

        // Если ID не найден, пробуем найти по селектору (для совместимости)
        const targetBody = tbody || document.querySelector('#usersTable tbody');
        if (!targetBody) return;

        targetBody.innerHTML = ''; // Очистка на всякий случай
        targetBody.appendChild(el('tr', {}, el('td', { colspan: 8, style: { textAlign: 'center', padding: '20px' } }, 'Загрузка...')));

        try {
            const users = await api.get('/users');

            targetBody.innerHTML = ''; // Очищаем спиннер

            if (!users || users.length === 0) {
                targetBody.appendChild(el('tr', {},
                    el('td', { colspan: 8, style: { textAlign: 'center', padding: '20px' } }, 'Нет пользователей')
                ));
                return;
            }

            users.forEach(user => {
                const tr = el('tr', {},
                    el('td', {}, String(user.id)),
                    el('td', {}, el('strong', {}, user.username)),
                    el('td', {},
                        el('span', { class: `role-badge ${user.role}` }, user.role)
                    ),
                    el('td', {}, user.dormitory || '-'),
                    el('td', {}, String(user.apartment_area)),
                    el('td', {}, `${user.residents_count} / ${user.total_room_residents}`),
                    el('td', {}, user.workplace || '-'),
                    el('td', {},
                        // Кнопка редактировать
                        el('button', {
                            class: 'action-btn-small btn-edit',
                            title: 'Редактировать',
                            style: { marginRight: '5px' },
                            onclick: () => this.openEditModal(user.id)
                        }, '✏️'),
                        // Кнопка удалить
                        el('button', {
                            class: 'action-btn-small btn-delete',
                            title: 'Удалить',
                            onclick: () => this.deleteUser(user.id)
                        }, '🗑️')
                    )
                );
                targetBody.appendChild(tr);
            });

        } catch (error) {
            if (targetBody) {
                targetBody.innerHTML = '';
                targetBody.appendChild(el('tr', {},
                    el('td', { colspan: 8, style: { color: 'red', textAlign: 'center' } }, error.message)
                ));
            }
        }
    },

    // ============================================================
    // ДОБАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯ
    // ============================================================
    async submitAddUser(e) {
        e.preventDefault();
        const form = e.target;
        const btn = form.querySelector('button[type="submit"]');

        const data = {
            username: document.getElementById('newUsername').value,
            password: document.getElementById('newPassword').value,
            role: document.getElementById('newRole').value,
            dormitory: document.getElementById('dormitory').value,
            workplace: document.getElementById('workplace').value,
            residents_count: parseInt(document.getElementById('residentsCount').value) || 1,
            total_room_residents: parseInt(document.getElementById('totalRoomResidents').value) || 1,
            apartment_area: parseFloat(document.getElementById('area').value) || 0
        };

        setLoading(btn, true, 'Создание...');

        try {
            await api.post('/users', data);
            alert('Пользователь создан!');
            form.reset();
            this.load();
        } catch (error) {
            alert('Ошибка: ' + error.message);
        } finally {
            setLoading(btn, false);
        }
    },

    // ============================================================
    // РЕДАКТИРОВАНИЕ
    // ============================================================
    async openEditModal(userId) {
        try {
            const user = await api.get(`/users/${userId}`);

            // Заполняем форму
            document.getElementById('editUserId').value = user.id;
            document.getElementById('editUsername').value = user.username;
            document.getElementById('editPassword').value = ''; // Сброс пароля
            document.getElementById('editRole').value = user.role;
            document.getElementById('editDormitory').value = user.dormitory || '';
            document.getElementById('editWorkplace').value = user.workplace || '';
            document.getElementById('editArea').value = user.apartment_area;
            document.getElementById('editResidentsCount').value = user.residents_count;
            document.getElementById('editTotalRoomResidents').value = user.total_room_residents;

            // Открываем модалку
            document.getElementById('userEditModal').classList.add('open');

        } catch (error) {
            alert('Не удалось загрузить данные: ' + error.message);
        }
    },

    closeEditModal() {
        document.getElementById('userEditModal').classList.remove('open');
    },

    async submitEditUser(e) {
        e.preventDefault();
        const btn = e.target.querySelector('button[type="submit"]');
        const userId = document.getElementById('editUserId').value;

        const data = {
            username: document.getElementById('editUsername').value,
            role: document.getElementById('editRole').value,
            dormitory: document.getElementById('editDormitory').value,
            workplace: document.getElementById('editWorkplace').value,
            residents_count: parseInt(document.getElementById('editResidentsCount').value) || 1,
            total_room_residents: parseInt(document.getElementById('editTotalRoomResidents').value) || 1,
            apartment_area: parseFloat(document.getElementById('editArea').value) || 0
        };

        const password = document.getElementById('editPassword').value;
        if (password) {
            data.password = password;
        }

        setLoading(btn, true, 'Сохранение...');

        try {
            await api.put(`/users/${userId}`, data);
            alert('Данные обновлены!');
            this.closeEditModal();
            this.load();
        } catch (error) {
            alert('Ошибка: ' + error.message);
        } finally {
            setLoading(btn, false);
        }
    },

    // ============================================================
    // УДАЛЕНИЕ
    // ============================================================
    async deleteUser(userId) {
        if (!confirm('Вы уверены, что хотите удалить этого пользователя? Это действие необратимо.')) {
            return;
        }

        try {
            await api.delete(`/users/${userId}`);
            alert('Пользователь удален.');
            this.load();
        } catch (error) {
            alert('Ошибка удаления: ' + error.message);
        }
    },

    // ============================================================
    // ИМПОРТ EXCEL
    // ============================================================
    async importUsers(btnElement) {
        const fileInput = document.getElementById('importUsersFile');
        const file = fileInput.files[0];

        if (!file) {
            alert("Выберите файл .xlsx");
            return;
        }

        const formData = new FormData();
        formData.append('file', file);

        setLoading(btnElement, true, 'Загрузка...');

        try {
            // api.js автоматически определит FormData и не будет ставить JSON хедеры
            const res = await api.post('/users/import_excel', formData);

            let msg = `Успешно добавлено: ${res.added}\n`;
            if (res.errors && res.errors.length > 0) {
                msg += `Ошибки (${res.errors.length}):\n` + res.errors.slice(0, 5).join('\n') + (res.errors.length > 5 ? '\n...' : '');
            }
            alert(msg);
            this.load();
            fileInput.value = '';
        } catch (error) {
            alert("Ошибка импорта: " + error.message);
        } finally {
            setLoading(btnElement, false);
        }
    }
};