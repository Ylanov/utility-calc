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
                tbody.innerHTML = '<tr><td colspan="7" style="text-align:center">Нет пользователей</td></tr>';
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