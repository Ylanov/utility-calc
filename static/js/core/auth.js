// static/js/core/auth.js

export const Auth = {
    // Получить токен
    getToken() {
        return localStorage.getItem('token');
    },

    // Сохранить токен (используется при логине)
    setToken(token) {
        localStorage.setItem('token', token);
    },

    // Выход из системы
    logout() {
        console.log('Logging out...');
        localStorage.removeItem('token');
        window.location.href = 'login.html';
    },

    // Проверка, вошел ли пользователь
    isAuthenticated() {
        const token = this.getToken();
        // Здесь можно добавить проверку срока действия JWT, если нужно
        return !!token;
    }
};