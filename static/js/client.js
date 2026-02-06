// static/js/client.js
import { Auth } from './core/auth.js';
import { ClientDashboard } from './modules/client-dashboard.js';
import { api } from './core/api.js'; // Чтобы подтянуть инстанс, если нужно

// Проверка авторизации
if (!Auth.isAuthenticated()) {
    window.location.href = 'login.html';
}

document.addEventListener('DOMContentLoaded', () => {
    console.log('Client App initialized');

    // Кнопка выхода
    const logoutBtn = document.querySelector('button[onclick="logout()"]');
    if (logoutBtn) {
        logoutBtn.removeAttribute('onclick');
        logoutBtn.addEventListener('click', () => Auth.logout());
    }

    // Загружаем данные профиля (если есть отдельный эндпоинт, используем его)
    // Сейчас мы загружаем всё внутри dashboard init
    ClientDashboard.init();

    // Попробуем загрузить данные пользователя отдельно, чтобы заполнить шапку
    loadUserProfile();
});

async function loadUserProfile() {
    // Если у тебя нет эндпоинта /api/users/me, данные останутся пустыми ("Загрузка...")
    // Можешь добавить этот эндпоинт в users.py (fastapi)
    /*
    try {
        const user = await api.get('/users/me');
        document.getElementById('pUser').textContent = user.username;
        document.getElementById('pAddress').textContent = user.dormitory;
        document.getElementById('pArea').textContent = user.apartment_area + ' м²';
        document.getElementById('pResidents').textContent = user.residents_count;
    } catch(e) {
        console.log('User profile info not available via API');
    }
    */
}