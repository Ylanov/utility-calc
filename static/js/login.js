// static/js/login.js
import { Auth } from './core/auth.js';
import { toast, setLoading } from './core/dom.js';

document.addEventListener('DOMContentLoaded', () => {

    // --- ЭЛЕМЕНТЫ ЛОГИНА ---
    const loginForm = document.getElementById('loginForm');
    const usernameInput = document.getElementById('username');
    const passwordInput = document.getElementById('password');
    const btnLogin = document.getElementById('btnLogin');

    // --- ЭЛЕМЕНТЫ ИНФО-МОДАЛКИ «ЗАБЫЛИ ПАРОЛЬ?» ---
    // Форма с площадью помещения удалена (may 2026): теперь только
    // показываем info-сообщение, никаких обращений к серверу.
    const btnForgotPass = document.getElementById('btnForgotPass');
    const resetModal = document.getElementById('resetModal');
    const btnCloseIconReset = document.getElementById('btnCloseIconReset');
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
    // «ЗАБЫЛИ ПАРОЛЬ?» — ПРОСТО ИНФО-МОДАЛКА
    // ==========================================
    const closeResetModal = () => resetModal.classList.remove('open');

    btnForgotPass.addEventListener('click', (e) => {
        e.preventDefault();
        resetModal.classList.add('open');
    });

    btnCloseIconReset.addEventListener('click', closeResetModal);
    btnCloseSuccess.addEventListener('click', closeResetModal);

    // Закрытие по клику вне модалки
    resetModal.addEventListener('mousedown', (e) => {
        if (e.target === resetModal) closeResetModal();
    });
});