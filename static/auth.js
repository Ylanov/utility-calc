// Обработка логина
const loginForm = document.getElementById('loginForm');
if (loginForm) {
    loginForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const username = document.getElementById('username').value;
        const password = document.getElementById('password').value;

        // FastAPI ожидает form-data для OAuth2
        const formData = new URLSearchParams();
        formData.append('username', username);
        formData.append('password', password);

        const response = await fetch('/token', {
            method: 'POST',
            body: formData
        });

        if (response.ok) {
            const data = await response.json();
            localStorage.setItem('token', data.access_token);

            // Редирект в зависимости от роли
            if (data.role === 'accountant') {
                window.location.href = 'admin.html';
            } else {
                window.location.href = 'index.html';
            }
        } else {
            document.getElementById('errorMsg').innerText = "Ошибка входа";
        }
    });
}

function logout() {
    localStorage.removeItem('token');
    window.location.href = 'login.html';
}