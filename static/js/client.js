// static/js/client.js
import { Auth } from './core/auth.js';
import { toast } from './core/dom.js';

// --- 1. Глобальная проверка авторизации ---
// Если токена в памяти нет, сразу перенаправляем на вход.
if (!Auth.isAuthenticated()) {
    window.location.replace('login.html');
}

// --- 2. Глобальный перехватчик ошибок (Error Boundary) ---
window.addEventListener('unhandledrejection', (event) => {
    console.error('Unhandled Rejection:', event.reason);

    // Игнорируем ошибки отмены запросов (AbortController)
    if (event.reason && event.reason.name === 'AbortError') return;

    const msg = event.reason?.message || 'Неизвестная ошибка сервера';
    toast(`Системная ошибка: ${msg}`, 'error');
});

window.addEventListener('error', (event) => {
    console.error('Global Error:', event.error);
    toast(`Ошибка приложения: ${event.message}`, 'error');
});

// --- 3. Инициализация приложения (Lazy Load) ---
document.addEventListener('DOMContentLoaded', async () => {
    console.log('Client App initialized (SPA Lazy Mode)');

    setupGlobalEvents();

    try {
        // Загружаем основные модули динамически, чтобы не блокировать рендер HTML
        const { ClientDashboard } = await import('./modules/client-dashboard.js');
        const { TotpSetup } = await import('./core/totp.js');
        const { ClientAppDownload } = await import('./modules/client-app-download.js');
        const { ClientCertificates } = await import('./modules/client-certificates.js');

        // Запускаем основной модуль личного кабинета
        ClientDashboard.init();

        // Инициализируем логику 2FA
        TotpSetup.init();

        // Подгружаем карточку «Скачать приложение» (если опубликован APK)
        ClientAppDownload.init();

        // Вкладка «Справки» + профильная форма с паспортом/семьёй.
        // Инициализация лёгкая — первый запрос идёт только когда жилец
        // реально переключится на вкладку (ленивая загрузка данных).
        ClientCertificates.init();

    } catch (error) {
        console.error('Ошибка загрузки модулей клиента:', error);
        toast('Ошибка загрузки интерфейса. Проверьте подключение к интернету.', 'error');
    }
});

// --- 4. Настройка глобальных событий ---
function setupGlobalEvents() {
    const logoutBtn = document.getElementById('clientLogoutBtn');

    if (logoutBtn) {
        logoutBtn.addEventListener('click', (e) => {
            e.preventDefault();
            Auth.logout();
        });
    }
}