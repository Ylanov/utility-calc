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
                // 1. Первый этап: Отправка логина и пароля
                let response = await fetch('/token', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/x-www-form-urlencoded'
                    },
                    body: formData
                });

                // 2. Проверка на необходимость 2FA (Статус 202 Accepted)
                if (response.status === 202) {
                    const tempData = await response.json();

                    // Запрашиваем код у пользователя
                    // (Для продакшена можно заменить prompt на красивое модальное окно)
                    const code = prompt("🔐 Введите код из Яндекс.Ключа / Google Authenticator:");

                    if (!code) {
                        throw new Error("Вход отменен: код не введен");
                    }

                    // 3. Второй этап: Подтверждение кода 2FA
                    response = await fetch('/api/auth/verify-2fa', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json'
                        },
                        body: JSON.stringify({
                            temp_token: tempData.temp_token,
                            code: code.trim()
                        })
                    });
                }

                // 4. Обработка финального результата (от /token или от /verify-2fa)
                if (!response.ok) {
                    const errData = await response.json().catch(() => ({}));
                    throw new Error(errData.detail || 'Неверные данные для входа');
                }

                const data = await response.json();

                // Сохраняем роль и имя. Токен (access_token) браузер сохранил в HttpOnly Cookie.
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
                console.error(error);
                toast(error.message, 'error');
                passwordInput.value = '';
                // Если ошибка 2FA, возможно стоит очистить и поле логина, но это опционально
            } finally {
                setLoading(btn, false, 'Войти');
            }
        });
    }
});