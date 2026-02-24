// static/js/client.js
import { Auth } from './core/auth.js';
import { ClientDashboard } from './modules/client-dashboard.js';
import { TotpSetup } from './core/totp.js'; // <-- Добавлен импорт логики 2FA

// --- 1. Глобальная проверка авторизации ---
// Если токена в памяти нет, сразу перенаправляем на вход.
// Используем replace, чтобы текущая страница не сохранялась в истории.
if (!Auth.isAuthenticated()) {
    window.location.replace('login.html');
}

// --- 2. Инициализация приложения ---
document.addEventListener('DOMContentLoaded', () => {
    console.log('Client App initialized');

    setupGlobalEvents();

    // Запускаем основной модуль личного кабинета
    ClientDashboard.init();

    // Инициализируем 2FA
    TotpSetup.init(); // <-- Запуск логики 2FA
});

function setupGlobalEvents() {
    // Находим кнопку выхода по ID (добавлен в новом index.html)
    const logoutBtn = document.getElementById('clientLogoutBtn');

    if (logoutBtn) {
        logoutBtn.addEventListener('click', (e) => {
            e.preventDefault();
            Auth.logout();
        });
    }
}