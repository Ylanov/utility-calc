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
    const tempPasswordDisplay = document.getElementById('tempPasswordDisplay');

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

            // Если все ОК, сохраняем сессию
            Auth.setSession(data.role, username);

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
            // Запрос через api.js
            const result = await api.post('/auth/reset-password', {
                username: resetUsername,
                apartment_area: resetArea
            });

            // Скрываем форму, показываем блок с новым паролем
            resetForm.classList.add('hide');
            resetSuccess.classList.remove('hide');
            tempPasswordDisplay.textContent = result.temp_password;

        } catch (error) {
            toast(error.message, 'error');
        } finally {
            setLoading(btnSubmitReset, false, 'Сбросить');
        }
    });

    // 4. Закрытие модалки после успеха и подстановка данных для входа
    btnCloseSuccess.addEventListener('click', () => {
        closeResetModal();

        // Автоматически вставляем сгенерированные данные в форму логина
        usernameInput.value = document.getElementById('resetUsername').value.trim();
        passwordInput.value = tempPasswordDisplay.textContent;
        passwordInput.focus();
    });

    // 5. Закрытие по клику вне модального окна
    resetModal.addEventListener('mousedown', (e) => {
        if (e.target === resetModal && !resetForm.classList.contains('hide')) {
            closeResetModal();
        }
    });
});