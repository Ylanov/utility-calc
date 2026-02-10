// static/js/client.js
import { Auth } from './core/auth.js';
import { ClientDashboard } from './modules/client-dashboard.js';

// Проверка авторизации: если токена нет, редирект на вход
if (!Auth.isAuthenticated()) {
    window.location.href = 'login.html';
}

document.addEventListener('DOMContentLoaded', () => {
    console.log('Client App initialized');

    // Находим кнопку выхода и вешаем обработчик
    // (удаляем onclick атрибут из HTML, если он был, чтобы использовать модуль Auth)
    const logoutBtn = document.querySelector('button[onclick="logout()"]');
    if (logoutBtn) {
        logoutBtn.removeAttribute('onclick');
        logoutBtn.addEventListener('click', () => Auth.logout());
    }

    // Инициализируем основной модуль дашборда.
    // Он сам загрузит профиль, показания и историю.
    ClientDashboard.init();
});

// Глобальная функция logout на случай, если где-то в HTML остался вызов onclick="logout()"
window.logout = function() {
    Auth.logout();
};