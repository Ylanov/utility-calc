// static/js/login.js
import { Auth } from './core/auth.js';
import { toast, setLoading } from './core/dom.js';

document.addEventListener('DOMContentLoaded', () => {
    // Чистим старые данные при заходе на страницу логина
    localStorage.removeItem('token');

    const loginForm = document.getElementById('loginForm');
    if (loginForm) {
        loginForm.addEventListener('submit', async (e) => {
            e.preventDefault();

            const usernameInput = document.getElementById('username');
            const passwordInput = document.getElementById('password');
            const btn = loginForm.querySelector('button');

            setLoading(btn, true, 'Вход...');

            // Используем URLSearchParams для application/x-www-form-urlencoded
            const formData = new URLSearchParams();
            formData.append('username', usernameInput.value.trim());
            formData.append('password', passwordInput.value);

            try {
                // Используем fetch напрямую, так как базовый api.js настроен на /api,
                // а эндпоинт токена обычно лежит в корне /token
                const response = await fetch('/token', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/x-www-form-urlencoded'
                    },
                    body: formData
                });

                if (!response.ok) {
                    throw new Error('Неверный логин или пароль');
                }

                const data = await response.json();

                // Сохраняем токен в память
                // Бэкенд возвращает: access_token, token_type, role
                Auth.setSession(data.access_token, data.role, usernameInput.value);

                toast('Успешный вход!', 'success');

                // Очищаем пароль из DOM
                passwordInput.value = '';

                // Редирект в зависимости от роли
                setTimeout(() => {
                    if (data.role === 'accountant' || data.role === 'admin') {
                        window.location.href = 'admin.html';
                    } else {
                        window.location.href = 'index.html';
                    }
                }, 500);

            } catch (error) {
                toast(error.message, 'error');
                passwordInput.value = '';
                passwordInput.focus();
            } finally {
                setLoading(btn, false, 'Войти');
            }
        });
    }
});