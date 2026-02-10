// static/js/login.js
import { Auth } from './core/auth.js';
import { toast, setLoading } from './core/dom.js';

document.addEventListener('DOMContentLoaded', () => {
    // Если пользователь уже авторизован — сразу кидаем внутрь
    if (Auth.isAuthenticated()) {
        // Пытаемся угадать роль по прошлому заходу или просто кидаем на главную
        // (Бэкенд все равно проверит права при загрузке данных)
        window.location.href = 'index.html';
        return;
    }

    const loginForm = document.getElementById('loginForm');

    if (loginForm) {
        loginForm.addEventListener('submit', async (e) => {
            e.preventDefault();

            const usernameInput = document.getElementById('username');
            const passwordInput = document.getElementById('password');
            const btn = loginForm.querySelector('button');

            // Блокируем кнопку
            setLoading(btn, true, 'Вход...');

            // FastAPI OAuth2 требует данные в формате application/x-www-form-urlencoded
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

                if (response.ok) {
                    const data = await response.json();

                    if (data.access_token) {
                        Auth.setToken(data.access_token);
                        toast('Вход выполнен успешно!', 'success');

                        // Небольшая задержка перед редиректом для красоты
                        setTimeout(() => {
                            if (data.role === 'accountant' || data.role === 'admin') {
                                window.location.href = 'admin.html';
                            } else {
                                window.location.href = 'index.html';
                            }
                        }, 500);
                    } else {
                        throw new Error("Сервер не вернул токен");
                    }
                } else {
                    // Пытаемся прочитать текст ошибки от сервера
                    let errorText = "Неверный логин или пароль";
                    try {
                        const errData = await response.json();
                        if (errData.detail) errorText = errData.detail;
                    } catch (e) { /* ignore json parse error */ }

                    throw new Error(errorText);
                }

            } catch (e) {
                toast(e.message, 'error');
                // Сбрасываем пароль при ошибке, чтобы пользователь ввел заново
                passwordInput.value = '';
                passwordInput.focus();
            } finally {
                setLoading(btn, false, 'Войти');
            }
        });
    }
});