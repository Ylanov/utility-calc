// static/js/login.js
import { Auth } from './core/auth.js';
import { toast, setLoading } from './core/dom.js';

document.addEventListener('DOMContentLoaded', () => {
    // Чистим старые данные при заходе на страницу логина
    sessionStorage.clear();
    localStorage.removeItem('token');

    const loginForm = document.getElementById('loginForm');
    if (loginForm) {
        loginForm.addEventListener('submit', async (e) => {
            e.preventDefault();

            const usernameInput = document.getElementById('username');
            const passwordInput = document.getElementById('password');
            const btn = loginForm.querySelector('button');

            setLoading(btn, true, 'Вход...');

            const formData = new URLSearchParams();
            formData.append('username', usernameInput.value.trim());
            formData.append('password', passwordInput.value);

            try {
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

                // Сохраняем только роль и имя пользователя.
                // Токен браузер сохранил автоматически в куках!
                Auth.setSession(data.role, data.username || usernameInput.value.trim());

                toast('Успешный вход!', 'success');
                passwordInput.value = '';

                // Редирект в зависимости от роли
                setTimeout(() => {
                    if (data.role === 'admin' || data.role === 'accountant') {
                        window.location.href = 'admin.html';
                    } else if (data.role === 'financier') {
                        window.location.href = 'financier.html';
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