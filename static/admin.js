// Проверка авторизации
if (!localStorage.getItem('token')) {
    window.location.href = 'login.html';
}

const token = localStorage.getItem('token');

// --- Логика Вкладок (Tabs) ---
function openTab(tabId) {
    // Скрыть все вкладки
    document.querySelectorAll('.tab-content').forEach(tab => {
        tab.classList.remove('active');
    });
    // Убрать активность у кнопок
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.classList.remove('active');
    });

    // Показать нужную вкладку
    const activeTab = document.getElementById(tabId);
    if (activeTab) {
        activeTab.classList.add('active');
    }

    // Активировать нужную кнопку
    const buttons = document.querySelectorAll('.tab-btn');
    if (tabId === 'readings') buttons[0].classList.add('active');
    if (tabId === 'users') buttons[1].classList.add('active');
    if (tabId === 'tariffs') buttons[2].classList.add('active');
}

// --- 1. СВЕРКА ПОКАЗАНИЙ (READINGS) ---

async function loadReadings() {
    try {
        const response = await fetch('/api/admin/readings', {
            headers: { 'Authorization': `Bearer ${token}` }
        });

        if (response.ok) {
            const data = await response.json();
            const tbody = document.querySelector('#readingsTable tbody');
            tbody.innerHTML = '';

            if (data.length === 0) {
                tbody.innerHTML = '<tr><td colspan="8" style="text-align:center; padding: 20px;">Нет новых показаний для проверки</td></tr>';
                return;
            }

            data.forEach(r => {
                const tr = document.createElement('tr');

                // Считаем черновую дельту (расход) для отображения
                const dHot = (r.cur_hot - r.prev_hot).toFixed(2);
                const dCold = (r.cur_cold - r.prev_cold).toFixed(2);
                const dElect = (r.cur_elect - r.prev_elect).toFixed(2);

                tr.innerHTML = `
                    <td>
                        <strong>${r.username}</strong>
                        <div style="font-size:11px; color:#888;">${r.dormitory || ''}</div>
                    </td>
                    <td>
                        ${r.prev_hot} → <strong>${r.cur_hot}</strong><br>
                        <span style="font-size:11px; color:#555;">(Расход: ${dHot})</span>
                    </td>
                    <td>
                        <input type="number" step="0.01" class="corr-input" id="hot_corr_${r.id}" value="0">
                    </td>
                    <td>
                        ${r.prev_cold} → <strong>${r.cur_cold}</strong><br>
                        <span style="font-size:11px; color:#555;">(Расход: ${dCold})</span>
                    </td>
                    <td>
                        <input type="number" step="0.01" class="corr-input" id="cold_corr_${r.id}" value="0">
                    </td>
                    <td>
                         ${r.prev_elect} → <strong>${r.cur_elect}</strong><br>
                         <span style="font-size:11px; color:#555;">(Расход: ${dElect})</span>
                    </td>
                    <td style="color:#666;">
                        ~ ${r.total_cost.toFixed(2)} ₽
                    </td>
                    <td>
                        <button onclick="approveReading(${r.id})" class="action-btn" style="padding: 5px 10px; font-size: 12px; background: #28a745; margin-top:0;">
                            ✅ Утвердить
                        </button>
                    </td>
                `;
                tbody.appendChild(tr);
            });
        } else {
            if (response.status === 401) logout();
        }
    } catch (e) {
        console.error("Ошибка загрузки показаний", e);
    }
}

async function approveReading(id) {
    // 1. Считываем значения коррекций из инпутов
    const hotCorrection = parseFloat(document.getElementById(`hot_corr_${id}`).value) || 0;
    const coldCorrection = parseFloat(document.getElementById(`cold_corr_${id}`).value) || 0;

    if (!confirm(`Утвердить показания с коррекцией?\n\nГорячая вода: -${hotCorrection} куб.\nХолодная вода: -${coldCorrection} куб.\n\nИтоговая сумма будет пересчитана.`)) {
        return;
    }

    try {
        // 2. Отправляем данные на сервер
        const response = await fetch(`/api/admin/approve/${id}`, {
            method: 'POST',
            headers: {
                'Authorization': `Bearer ${token}`,
                'Content-Type': 'application/json'  // Важный заголовок!
            },
            body: JSON.stringify({
                hot_correction: hotCorrection,
                cold_correction: coldCorrection
            })
        });

        if (response.ok) {
            alert("Показания утверждены, сумма пересчитана!");
            loadReadings(); // Обновляем список, чтобы утвержденная запись исчезла
        } else {
            const err = await response.json();
            alert("Ошибка при утверждении: " + (err.detail || 'Неизвестная ошибка'));
        }
    } catch (e) {
        alert("Ошибка сети");
    }
}


// --- 2. УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯМИ (USERS) ---

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
                    <td><span style="background:${user.role === 'accountant' ? '#e8f5e9' : '#e3f2fd'}; padding: 3px 8px; border-radius:10px; font-size:12px;">${user.role}</span></td>
                    <td>${user.dormitory || '-'}</td>
                    <td>${user.apartment_area}</td>
                    <td>${user.residents_count}</td>
                    <td>${user.workplace || '-'}</td>
                `;
                tbody.appendChild(tr);
            });
        }
    } catch (e) {
        console.error("Ошибка загрузки пользователей", e);
    }
}

document.getElementById('addUserForm').addEventListener('submit', async (e) => {
    e.preventDefault();

    const submitBtn = e.target.querySelector('button[type="submit"]');
    submitBtn.disabled = true;
    submitBtn.innerText = "Сохранение...";

    const data = {
        username: document.getElementById('newUsername').value,
        password: document.getElementById('newPassword').value,
        role: document.getElementById('newRole').value,
        dormitory: document.getElementById('dormitory').value,
        workplace: document.getElementById('workplace').value,
        residents_count: parseInt(document.getElementById('residentsCount').value) || 1,
        apartment_area: parseFloat(document.getElementById('area').value) || 0
    };

    try {
        const response = await fetch('/api/users', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${token}`
            },
            body: JSON.stringify(data)
        });

        if (response.ok) {
            alert('Пользователь успешно создан!');
            loadUsers();
            e.target.reset();
        } else {
            const err = await response.json();
            alert('Ошибка: ' + (err.detail || 'Неизвестная ошибка'));
        }
    } catch (error) {
        alert('Ошибка сети');
    } finally {
        submitBtn.disabled = false;
        submitBtn.innerText = "Зарегистрировать пользователя";
    }
});

// --- 3. УПРАВЛЕНИЕ ТАРИФАМИ (TARIFFS) ---

async function loadTariffs() {
    try {
        const response = await fetch('/api/tariffs', {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (response.ok) {
            const t = await response.json();
            document.getElementById('t_main').value = t.maintenance_repair;
            document.getElementById('t_rent').value = t.social_rent;
            document.getElementById('t_heat').value = t.heating;
            document.getElementById('t_w_heat').value = t.water_heating;
            document.getElementById('t_w_sup').value = t.water_supply;
            document.getElementById('t_sew').value = t.sewage;
            document.getElementById('t_waste').value = t.waste_disposal;
            document.getElementById('t_el_sqm').value = t.electricity_per_sqm;
        }
    } catch (e) {
        console.error("Ошибка загрузки тарифов", e);
    }
}

document.getElementById('tariffsForm').addEventListener('submit', async (e) => {
    e.preventDefault();

    const submitBtn = e.target.querySelector('button[type="submit"]');
    const originalText = submitBtn.innerText;
    submitBtn.innerText = "Сохранение...";

    const data = {
        maintenance_repair: parseFloat(document.getElementById('t_main').value),
        social_rent: parseFloat(document.getElementById('t_rent').value),
        heating: parseFloat(document.getElementById('t_heat').value),
        water_heating: parseFloat(document.getElementById('t_w_heat').value),
        water_supply: parseFloat(document.getElementById('t_w_sup').value),
        sewage: parseFloat(document.getElementById('t_sew').value),
        waste_disposal: parseFloat(document.getElementById('t_waste').value),
        electricity_per_sqm: parseFloat(document.getElementById('t_el_sqm').value),
    };

    try {
        const response = await fetch('/api/tariffs', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${token}`
            },
            body: JSON.stringify(data)
        });

        if (response.ok) {
            alert("Тарифы успешно обновлены!");
        } else {
            alert("Ошибка обновления тарифов");
        }
    } catch (e) {
        alert("Ошибка сети");
    } finally {
        submitBtn.innerText = originalText;
    }
});

// --- Инициализация при загрузке страницы ---
// Открываем первую вкладку и загружаем данные для всех разделов
openTab('readings');
loadReadings();
loadUsers();
loadTariffs();