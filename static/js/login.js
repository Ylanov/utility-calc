// static/js/login.js
import { api } from './core/api.js';
import { Auth } from './core/auth.js';
import { toast, setLoading } from './core/dom.js';

document.addEventListener('DOMContentLoaded', () => {

    // --- ЭЛЕМЕНТЫ ЛОГИНА ---
    const loginForm = document.getElementById('loginForm');
    const usernameInput = document.getElementById('username');
    const passwordInput = document.getElementById('password');
    const btnLogin = document.getElementById('btnLogin');

    // --- ЭЛЕМЕНТЫ СБРОСА ПАРОЛЯ ---
    const btnForgotPass = document.getElementById('btnForgotPass');
    const resetModal = document.getElementById('resetModal');
    const resetForm = document.getElementById('resetForm');
    const resetSuccess = document.getElementById('resetSuccess');
    const btnCancelReset = document.getElementById('btnCancelReset');
    const btnCloseIconReset = document.getElementById('btnCloseIconReset');
    const btnSubmitReset = document.getElementById('btnSubmitReset');
    const btnCloseSuccess = document.getElementById('btnCloseSuccess');

    // ==========================================
    // ЛОГИКА ВХОДА В СИСТЕМУ
    // ==========================================
    loginForm.addEventListener('submit', async (e) => {
        e.preventDefault();

        const username = usernameInput.value.trim();
        const password = passwordInput.value;

        if (!username || !password) {
            toast('Введите логин и пароль', 'warning');
            return;
        }

        setLoading(btnLogin, true, 'Вход...');

        try {
            // Формируем x-www-form-urlencoded данные для OAuth2
            const formData = new URLSearchParams();
            formData.append('username', username);
            formData.append('password', password);
            formData.append('grant_type', 'password');

            const response = await fetch('/api/token', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/x-www-form-urlencoded'
                },
                body: formData.toString()
            });

            if (!response.ok) {
                // Пытаемся вытащить сообщение об ошибке от FastAPI
                const errData = await response.json().catch(() => ({}));
                throw new Error(errData.detail || 'Неверный логин или пароль');
            }

            const data = await response.json();

            // Сохраняем сессию — токен хранится в sessionStorage этой вкладки,
            // что исключает смешивание сессий разных пользователей
            Auth.setSession(data.role, username, data.access_token);

            // Перенаправляем в зависимости от роли
            if (['admin', 'accountant', 'financier'].includes(data.role)) {
                window.location.replace('admin.html');
            } else {
                window.location.replace('index.html');
            }

        } catch (error) {
            toast(error.message, 'error');
            passwordInput.value = '';
            passwordInput.focus();
        } finally {
            setLoading(btnLogin, false, 'Войти');
        }
    });

    // ==========================================
    // ЛОГИКА СБРОСА ПАРОЛЯ
    // ==========================================

    const closeResetModal = () => {
        resetModal.classList.remove('open');
    };

    // 1. Открыть модалку
    btnForgotPass.addEventListener('click', (e) => {
        e.preventDefault();
        resetForm.classList.remove('hide');
        resetSuccess.classList.add('hide');
        resetForm.reset();

        // Автоматически подставляем логин, если пользователь уже начал его вводить
        document.getElementById('resetUsername').value = usernameInput.value.trim();

        resetModal.classList.add('open');
    });

    // 2. Закрыть модалку (Отмена и Крестик)
    btnCancelReset.addEventListener('click', closeResetModal);
    btnCloseIconReset.addEventListener('click', closeResetModal);

    // 3. Отправка запроса на сброс
    resetForm.addEventListener('submit', async (e) => {
        e.preventDefault();

        const resetUsername = document.getElementById('resetUsername').value.trim();
        const resetArea = parseFloat(document.getElementById('resetArea').value.replace(',', '.'));

        setLoading(btnSubmitReset, true, 'Проверка...');

        try {
            // Заявка на сброс пароля. Сервер больше не возвращает plaintext —
            // пароль генерирует админ через /api/admin/users/{id}/reset-password
            // и передаёт жильцу out-of-band. Здесь просто показываем
            // подтверждение, что заявка зарегистрирована (anti-enumeration —
            // одно и то же сообщение даже при невалидных данных).
            await api.post('/auth/reset-password', {
                username: resetUsername,
                apartment_area: resetArea
            });

            resetForm.classList.add('hide');
            resetSuccess.classList.remove('hide');

        } catch (error) {
            toast(error.message, 'error');
        } finally {
            setLoading(btnSubmitReset, false, 'Сбросить');
        }
    });

    // 4. Закрытие модалки после успеха.
    // Раньше здесь автозаполнялся пароль из tempPasswordDisplay в форму логина —
    // больше нет, потому что пароль вообще не отдаётся пользователю клиентом.
    btnCloseSuccess.addEventListener('click', () => {
        closeResetModal();
        usernameInput.value = document.getElementById('resetUsername').value.trim();
        usernameInput.focus();
    });

    // 5. Закрытие по клику вне модального окна
    resetModal.addEventListener('mousedown', (e) => {
        if (e.target === resetModal && !resetForm.classList.contains('hide')) {
            closeResetModal();
        }
    });
});