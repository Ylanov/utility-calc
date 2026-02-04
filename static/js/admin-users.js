
// =========================================================
// 2. УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯМИ (USERS)
// =========================================================

/**
 * Загружает и отображает список всех пользователей
 */
async function loadUsers() {
    try {
        const response = await fetch('/api/users', {
            headers: { 'Authorization': `Bearer ${token}` }
        });

        if (response.ok) {
            const users = await response.json();
            const tbody = document.querySelector('#usersTable tbody');
            tbody.innerHTML = '';

            if (users.length === 0) {
                tbody.innerHTML = '<tr><td colspan="8" style="text-align:center">Нет пользователей</td></tr>';
                return;
            }

            users.forEach(user => {
                const tr = document.createElement('tr');
                tr.innerHTML = `
                    <td>${user.id}</td>
                    <td><strong>${user.username}</strong></td>
                    <td><span class="role-badge ${user.role}">${user.role}</span></td>
                    <td>${user.dormitory || '-'}</td>
                    <td>${user.apartment_area}</td>
                    <td>${user.residents_count} / ${user.total_room_residents}</td>
                    <td>${user.workplace || '-'}</td>
                    <td>
                        <button class="action-btn-small btn-edit" title="Редактировать" onclick="openUserEditModal(${user.id})">✏️</button>
                        <button class="action-btn-small btn-delete" title="Удалить" onclick="deleteUser(${user.id})">🗑️</button>
                    </td>
                `;
                tbody.appendChild(tr);
            });
        }
    } catch (e) {
        console.error("Ошибка загрузки пользователей:", e);
    }
}

/**
 * Обработчик формы добавления нового пользователя
 */
document.getElementById('addUserForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const submitBtn = e.target.querySelector('button[type="submit"]');
    submitBtn.disabled = true;
    submitBtn.innerText = "Создание...";

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

    try {
        const response = await fetch('/api/users', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
            body: JSON.stringify(data)
        });

        if (response.ok) {
            alert('Пользователь создан!');
            loadUsers();
            e.target.reset();
        } else {
            const err = await response.json();
            alert('Ошибка: ' + err.detail);
        }
    } catch (e) {
        alert('Ошибка сети');
    }
    finally {
        submitBtn.disabled = false;
        submitBtn.innerText = "Зарегистрировать пользователя";
    }
});


// <<< НОВЫЙ БЛОК: РЕДАКТИРОВАНИЕ И УДАЛЕНИЕ ПОЛЬЗОВАТЕЛЕЙ >>>

/**
 * Открывает модальное окно для редактирования пользователя
 * @param {number} userId - ID пользователя
 */
async function openUserEditModal(userId) {
    try {
        const response = await fetch(`/api/users/${userId}`, {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (!response.ok) throw new Error('Не удалось получить данные пользователя');

        const user = await response.json();

        // Заполняем форму в модальном окне
        document.getElementById('editUserId').value = user.id;
        document.getElementById('editUsername').value = user.username;
        document.getElementById('editPassword').value = ''; // Пароль всегда пустой для безопасности
        document.getElementById('editRole').value = user.role;
        document.getElementById('editDormitory').value = user.dormitory || '';
        document.getElementById('editWorkplace').value = user.workplace || '';
        document.getElementById('editArea').value = user.apartment_area;
        document.getElementById('editResidentsCount').value = user.residents_count;
        document.getElementById('editTotalRoomResidents').value = user.total_room_residents;

        // Показываем окно
        document.getElementById('userEditModal').classList.add('open');

    } catch (e) {
        alert(e.message);
    }
}

/**
 * Закрывает модальное окно редактирования
 */
function closeUserEditModal() {
    document.getElementById('userEditModal').classList.remove('open');
}

/**
 * Обработчик формы редактирования пользователя
 */
document.getElementById('editUserForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const submitBtn = e.target.querySelector('button[type="submit"]');
    submitBtn.disabled = true;

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
    
    // Добавляем пароль, только если он был введен
    const password = document.getElementById('editPassword').value;
    if (password) {
        data.password = password;
    }

    try {
        const response = await fetch(`/api/users/${userId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
            body: JSON.stringify(data)
        });

        if (response.ok) {
            alert('Данные обновлены!');
            closeUserEditModal();
            loadUsers();
        } else {
            const err = await response.json();
            alert('Ошибка: ' + err.detail);
        }
    } catch (e) {
        alert('Ошибка сети');
    } finally {
        submitBtn.disabled = false;
    }
});

/**
 * Удаляет пользователя
 * @param {number} userId - ID пользователя
 */
async function deleteUser(userId) {
    if (!confirm('Вы уверены, что хотите удалить этого пользователя? Это действие необратимо.')) {
        return;
    }
    
    try {
        const response = await fetch(`/api/users/${userId}`, {
            method: 'DELETE',
            headers: { 'Authorization': `Bearer ${token}` }
        });
        
        if (response.ok) {
            alert('Пользователь удален.');
            loadUsers();
        } else {
            const err = await response.json();
            alert('Ошибка удаления: ' + err.detail);
        }
    } catch (e) {
        alert('Ошибка сети');
    }
}


async function importUsers() {
    const fileInput = document.getElementById('importUsersFile');
    const file = fileInput.files[0];

    if (!file) {
        alert("Выберите файл .xlsx");
        return;
    }

    const formData = new FormData();
    formData.append('file', file);

    const btn = document.querySelector('button[onclick="importUsers()"]');
    btn.disabled = true;
    btn.innerText = "Загрузка...";

    try {
        const response = await fetch('/api/users/import_excel', {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${token}` },
            body: formData
        });

        const res = await response.json();

        if (response.ok) {
            let msg = `Успешно добавлено: ${res.added}\n`;
            if (res.errors.length > 0) {
                msg += `Ошибки (${res.errors.length}):\n` + res.errors.slice(0, 5).join('\n') + (res.errors.length > 5 ? '\n...' : '');
            }
            alert(msg);
            loadUsers();
            fileInput.value = '';
        } else {
            alert("Ошибка: " + res.detail);
        }
    } catch (e) {
        alert("Ошибка сети");
    } finally {
        btn.disabled = false;
        btn.innerText = "Загрузить";
    }
}
