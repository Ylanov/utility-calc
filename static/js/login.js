import { Auth } from './core/auth.js';

document.addEventListener('DOMContentLoaded', () => {
    const loginForm = document.getElementById('loginForm');
    const errorMsg = document.getElementById('errorMsg');

    if (loginForm) {
        loginForm.addEventListener('submit', async (e) => {
            e.preventDefault();

            const username = document.getElementById('username').value;
            const password = document.getElementById('password').value;
            const btn = loginForm.querySelector('button');

            // Блокируем кнопку
            btn.disabled = true;
            btn.innerText = "Вход...";
            errorMsg.innerText = "";

            // FastAPI OAuth2 требует данные в формате application/x-www-form-urlencoded
            const formData = new URLSearchParams();
            formData.append('username', username);
            formData.append('password', password);

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
                    Auth.setToken(data.access_token);

                    // Редирект в зависимости от роли
                    if (data.role === 'accountant') {
                        window.location.href = 'admin.html';
                    } else {
                        window.location.href = 'index.html';
                    }
                } else {
                    errorMsg.innerText = "Неверный логин или пароль";
                    btn.disabled = false;
                    btn.innerText = "Войти";
                }
            } catch (e) {
                errorMsg.innerText = "Ошибка соединения с сервером";
                btn.disabled = false;
                btn.innerText = "Войти";
            }
        });
    }
});