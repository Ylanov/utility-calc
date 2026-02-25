// static/js/app.js
import { api } from './core/api.js';
import { Auth } from './core/auth.js';
import { toast, setLoading } from './core/dom.js';
import { TotpSetup } from './core/totp.js';

// Подключаем все модули бизнес-логики
import { ReadingsModule } from './modules/readings.js';
import { UsersModule } from './modules/users.js';
import { TariffsModule } from './modules/tariffs.js';
import { SummaryModule } from './modules/summary.js';

// --- 1. Глобальная проверка авторизации ---
// Если в памяти нет токена (роли), сразу выкидываем на страницу логина.
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

// --- 3. Инициализация приложения ---
document.addEventListener('DOMContentLoaded', async () => {
    console.log('Admin App Initialized');

    setupHeader();
    setupGlobalEvents();
    setupAdminProfile();
    setupRouting();

    // Проверяем, нужно ли принудительно сменить пароль
    await checkAdminSecurity();

    // Инициализируем 2FA логику
    TotpSetup.init();
});

function setupHeader() {
    // Выводим имя текущего админа в шапку
    const adminNameEl = document.getElementById('adminName');
    const username = Auth.getUsername();

    if (adminNameEl && username) {
        adminNameEl.textContent = `${username}`;
    }
}

function setupGlobalEvents() {
    // Обработка кнопок выхода
    const logoutBtns = document.querySelectorAll('.logout-btn');
    logoutBtns.forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.preventDefault();
            Auth.logout();
        });
    });

    // Делегирование событий для переключения табов
    const tabsContainer = document.querySelector('.tabs');
    if (tabsContainer) {
        tabsContainer.addEventListener('click', (e) => {
            const btn = e.target.closest('.tab-btn');
            if (!btn) return;

            const tabId = btn.dataset.tab;
            if (tabId) {
                window.location.hash = tabId;
            }
        });
    }
}

// --- ЛОГИКА ПРОФИЛЯ АДМИНИСТРАТОРА И БЕЗОПАСНОСТИ ---

async function checkAdminSecurity() {
    try {
        const user = await api.get('/users/me');
        // Если это первый вход, показываем принудительное окно
        if (user.is_initial_setup_done === false) {
            const fsModal = document.getElementById('firstSetupModal');
            if (fsModal) {
                fsModal.classList.add('open');
            }
        }
    } catch (e) {
        console.warn('Security check failed', e);
    }
}

function setupAdminProfile() {
    // 1. Форма первичной настройки (Обязательная)
    const fsForm = document.getElementById('firstSetupForm');
    if (fsForm) {
        fsForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            const btn = document.getElementById('btnFsSave');
            const newLogin = document.getElementById('fsNewLogin')?.value.trim();
            const newPass = document.getElementById('fsNewPassword').value;

            setLoading(btn, true, 'Сохранение...');
            try {
                const payload = { new_password: newPass };
                if (newLogin) payload.new_username = newLogin;

                await api.post('/users/me/setup', payload);

                alert('Данные безопасности успешно обновлены. Пожалуйста, войдите в систему с новыми данными.');
                Auth.logout(); // Выкидываем на логин для проверки
            } catch (err) {
                toast(err.message, 'error');
                setLoading(btn, false, 'Сохранить и войти');
            }
        });
    }

    // 2. Открытие профиля по кнопке из шапки
    const profileBtn = document.getElementById('btnOpenAdminProfile');
    const profileModal = document.getElementById('adminProfileModal');
    if (profileBtn && profileModal) {
        profileBtn.addEventListener('click', () => {
            profileModal.classList.add('open');
        });
    }

    // 3. Форма смены пароля/логина внутри профиля
    const changeCredsForm = document.getElementById('adminChangeCredsForm');
    if (changeCredsForm) {
        changeCredsForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            const btn = document.getElementById('btnSaveAdminCreds');

            const newLogin = document.getElementById('adminNewLogin').value.trim();
            const oldPass = document.getElementById('adminOldPass').value;
            const newPass = document.getElementById('adminNewPass').value;

            setLoading(btn, true, 'Сохранение...');
            try {
                const user = await api.get('/users/me');

                // Если введен новый пароль — меняем пароль
                if (newPass) {
                    await api.post('/users/me/change-password', {
                        old_password: oldPass,
                        new_password: newPass
                    });
                }

                // Если введен новый логин — меняем логин через обычный PUT (права админа позволяют)
                if (newLogin && newLogin !== user.username) {
                    // Используем метод обновления пользователя
                    await api.put(`/users/${user.id}`, {
                        username: newLogin
                    });
                    // Обновляем данные в сессии и шапке
                    Auth.setSession(user.role, newLogin);
                    const adminNameEl = document.getElementById('adminName');
                    if(adminNameEl) adminNameEl.textContent = newLogin;
                }

                toast('Профиль успешно обновлен!', 'success');

                // Очищаем форму
                document.getElementById('adminOldPass').value = '';
                document.getElementById('adminNewPass').value = '';
                document.getElementById('adminNewLogin').value = '';

                profileModal.classList.remove('open');

            } catch (err) {
                toast(err.message, 'error');
            } finally {
                setLoading(btn, false, 'Сохранить изменения');
            }
        });
    }
}

// --- РОУТИНГ (ПЕРЕКЛЮЧЕНИЕ ВКЛАДОК) ---

function setupRouting() {
    window.addEventListener('hashchange', handleRoute);
    handleRoute(); // Вызываем первый раз при загрузке
}

function handleRoute() {
    const hash = window.location.hash.substring(1);
    const defaultTab = 'readings';
    const tabToLoad = (hash && isValidTab(hash)) ? hash : defaultTab;
    switchTab(tabToLoad);
}

function isValidTab(tabId) {
    return !!document.querySelector(`.tab-btn[data-tab="${tabId}"]`);
}

function switchTab(tabId) {
    if (!tabId) return;

    // Скрываем контент и деактивируем кнопки
    document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));

    // Показываем нужный таб
    const content = document.getElementById(tabId);
    if (content) content.classList.add('active');

    const btn = document.querySelector(`.tab-btn[data-tab="${tabId}"]`);
    if (btn) btn.classList.add('active');

    // Инициализируем модуль
    initModule(tabId);
}

function initModule(tabId) {
    switch (tabId) {
        case 'readings':
            ReadingsModule.init();
            break;
        case 'users':
            UsersModule.init();
            break;
        case 'tariffs':
            TariffsModule.init();
            break;
        case 'accountant':
            SummaryModule.init();
            break;
        default:
            console.warn(`Модуль для вкладки "${tabId}" не найден.`);
    }
}