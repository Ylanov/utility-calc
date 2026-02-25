// static/js/client.js
import { Auth } from './core/auth.js';
import { toast } from './core/dom.js';
import { ClientDashboard } from './modules/client-dashboard.js';
import { TotpSetup } from './core/totp.js';

// --- 1. Глобальная проверка авторизации ---
// Если токена в памяти нет, сразу перенаправляем на вход.
// Используем replace, чтобы текущая страница не сохранялась в истории браузера.
if (!Auth.isAuthenticated()) {
    window.location.replace('login.html');
}

// --- 2. Глобальный перехватчик ошибок (Error Boundary) ---
// Ловит ошибки в Promise (fetch, async/await)
window.addEventListener('unhandledrejection', (event) => {
    console.error('Unhandled Rejection:', event.reason);

    // Игнорируем ошибки отмены запросов (AbortController), это нормальное поведение
    if (event.reason && event.reason.name === 'AbortError') return;

    const msg = event.reason?.message || 'Неизвестная ошибка сервера';
    toast(`Системная ошибка: ${msg}`, 'error');
});

// Ловит обычные JS ошибки
window.addEventListener('error', (event) => {
    console.error('Global Error:', event.error);
    toast(`Ошибка приложения: ${event.message}`, 'error');
});

// --- 3. Инициализация приложения ---
document.addEventListener('DOMContentLoaded', () => {
    console.log('Client App initialized');

    setupGlobalEvents();

    // Запускаем основной модуль личного кабинета (вкладки, графики, отправка показаний)
    ClientDashboard.init();

    // Инициализируем логику 2FA (модальное окно и привязка)
    TotpSetup.init();
});

// --- 4. Настройка глобальных событий ---
function setupGlobalEvents() {
    // Находим кнопку выхода по ID
    const logoutBtn = document.getElementById('clientLogoutBtn');

    if (logoutBtn) {
        logoutBtn.addEventListener('click', (e) => {
            e.preventDefault();
            Auth.logout();
        });
    }
}