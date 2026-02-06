// static/js/core/api.js
import { Auth } from './auth.js';

/**
 * ApiClient - это централизованный класс для всех запросов к серверу.
 * Он автоматически добавляет токен авторизации и базовый URL ко всем запросам,
 * а также обеспечивает единую обработку ошибок.
 */
class ApiClient {
    /**
     * @param {string} baseUrl - Префикс, который добавляется ко всем эндпоинтам.
     * КРИТИЧЕСКОЕ ИЗМЕНЕНИЕ: Устанавливаем '/api' по умолчанию.
     * Теперь все запросы (например, get('/users')) будут автоматически отправляться на '/api/users'.
     */
    constructor(baseUrl = '/api') {
        this.baseUrl = baseUrl;
    }

    // --- Приватный метод для выполнения запросов ---
    // Этот метод является "сердцем" клиента и вызывается всеми публичными методами (get, post и т.д.)
    async _request(endpoint, options = {}) {
        const token = Auth.getToken();

        const headers = {
            // Добавляем токен авторизации в каждый запрос
            'Authorization': `Bearer ${token}`,
            // Позволяем передавать дополнительные заголовки, если это необходимо
            ...options.headers
        };

        // Если мы отправляем данные (тело запроса) и это не FormData,
        // то автоматически преобразуем их в JSON и устанавливаем нужный заголовок.
        if (options.body && !(options.body instanceof FormData)) {
            headers['Content-Type'] = 'application/json';
            options.body = JSON.stringify(options.body);
        }

        const config = {
            ...options,
            headers
        };

        try {
            // Собираем полный URL и выполняем запрос
            const response = await fetch(`${this.baseUrl}${endpoint}`, config);

            // --- Централизованная обработка ошибок ---

            // Если сервер вернул 401 (Unauthorized), значит токен недействителен.
            // Выходим из системы и прерываем выполнение.
            if (response.status === 401) {
                Auth.logout();
                throw new Error('Сессия истекла. Пожалуйста, войдите снова.');
            }

            // --- Обработка ответа ---

            const contentType = response.headers.get("content-type");
            let data;

            // Если ответ содержит JSON, парсим его. В противном случае, читаем как текст.
            if (contentType && contentType.includes("application/json")) {
                data = await response.json();
            } else {
                data = await response.text();
            }

            // Если ответ не "ok" (статус 200-299), генерируем ошибку.
            if (!response.ok) {
                // Пытаемся извлечь понятное сообщение об ошибке из ответа FastAPI (поле "detail").
                // Если его нет, показываем общую ошибку.
                const errorMessage = (typeof data === 'object' && data.detail)
                    ? data.detail
                    : `Ошибка сервера (${response.status})`;
                throw new Error(errorMessage);
            }

            return data;

        } catch (error) {
            // Логируем ошибку в консоль для отладки и пробрасываем ее дальше,
            // чтобы модуль, вызвавший запрос, мог показать ее пользователю.
            console.error(`API Error on [${options.method || 'GET'} ${endpoint}]:`, error);
            throw error;
        }
    }

    // --- Публичные методы для удобства ---

    get(endpoint) {
        return this._request(endpoint, { method: 'GET' });
    }

    post(endpoint, body) {
        return this._request(endpoint, { method: 'POST', body });
    }

    put(endpoint, body) {
        return this._request(endpoint, { method: 'PUT', body });
    }

    delete(endpoint) {
        return this._request(endpoint, { method: 'DELETE' });
    }

    // --- Специальный метод для скачивания файлов ---
    // Он не использует _request, так как работает с бинарными данными (Blob), а не JSON.
    async download(endpoint, defaultFilename = 'file') {
        const token = Auth.getToken();
        try {
            const response = await fetch(`${this.baseUrl}${endpoint}`, {
                method: 'GET', // Скачивание всегда GET-запрос
                headers: { 'Authorization': `Bearer ${token}` }
            });

            if (response.status === 401) {
                Auth.logout();
                return;
            }

            if (!response.ok) {
                throw new Error('Не удалось загрузить файл');
            }

            // Пытаемся извлечь имя файла из заголовка Content-Disposition
            let filename = defaultFilename;
            const disposition = response.headers.get('content-disposition');
            if (disposition && disposition.includes('attachment')) {
                const filenameRegex = /filename[^;=\n]*=((['"]).*?\2|[^;\n]*)/;
                const matches = filenameRegex.exec(disposition);
                if (matches != null && matches[1]) {
                    filename = matches[1].replace(/['"]/g, '');
                }
            }

            // Создаем Blob из ответа и инициируем скачивание в браузере
            const blob = await response.blob();
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = filename;
            document.body.appendChild(a);
            a.click();
            a.remove(); // Очищаем DOM
            window.URL.revokeObjectURL(url); // Освобождаем память

        } catch (error) {
            console.error('Download error:', error);
            alert('Не удалось скачать файл: ' + error.message);
        }
    }
}

// Создаем и экспортируем единственный экземпляр клиента,
// чтобы все модули использовали одно и то же подключение.
export const api = new ApiClient();