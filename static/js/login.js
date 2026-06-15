// static/js/login.js — вход для СОТРУДНИКОВ (админка).
// ЛК жильцов вычищен (2026-06-10): жильцы в систему не входят, их роут —
// анонимный QR-портал квартиры (/qr.html#<токен>). Бэкенд жильцу вернёт 403.
import { Auth } from './core/auth.js';
import { toast, setLoading } from './core/dom.js';

document.addEventListener('DOMContentLoaded', () => {

    const loginForm = document.getElementById('loginForm');
    const usernameInput = document.getElementById('username');
    const passwordInput = document.getElementById('password');
    const btnLogin = document.getElementById('btnLogin');

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
                // (в т.ч. 403 «Вход для жильцов отключён — пользуйтесь QR-кодом»).
                const errData = await response.json().catch(() => ({}));
                throw new Error(errData.detail || 'Неверный логин или пароль');
            }

            const data = await response.json();

            // Сохраняем сессию — токен хранится в sessionStorage этой вкладки,
            // что исключает смешивание сессий разных пользователей
            Auth.setSession(data.role, username, data.access_token);

            // Сюда доходят только сотрудники (role=user бэкенд отшил 403 выше).
            window.location.replace('admin.html');

        } catch (error) {
            toast(error.message, 'error');
            passwordInput.value = '';
            passwordInput.focus();
        } finally {
            setLoading(btnLogin, false, 'Войти');
        }
    });
});
