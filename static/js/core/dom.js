// static/js/core/dom.js

/**
 * Создает HTML элемент.
 * Пример: el('div', { class: 'my-class', onclick: myFunc }, 'Текст внутри')
 */
export function el(tag, attributes = {}, ...children) {
    const element = document.createElement(tag);

    for (const [key, value] of Object.entries(attributes)) {
        if (key === 'class') {
            element.className = value;
        } else if (key.startsWith('on') && typeof value === 'function') {
            // Например: onclick, onchange
            const eventName = key.substring(2).toLowerCase();
            element.addEventListener(eventName, value);
        } else if (key === 'dataset' && typeof value === 'object') {
            // data-атрибуты
            Object.assign(element.dataset, value);
        } else if (key === 'style' && typeof value === 'object') {
            // Стили объектом
            Object.assign(element.style, value);
        } else {
            // Обычные атрибуты (type, href, value...)
            if (value !== null && value !== undefined) {
                element.setAttribute(key, value);
            }
        }
    }

    children.forEach(child => {
        if (child === null || child === undefined) return;

        if (typeof child === 'string' || typeof child === 'number') {
            element.appendChild(document.createTextNode(child));
        } else if (child instanceof Node) {
            element.appendChild(child);
        } else if (Array.isArray(child)) {
            // Если передан массив элементов
            child.forEach(c => element.appendChild(c));
        }
    });

    return element;
}

/**
 * Очищает содержимое элемента по ID и возвращает его.
 */
export function clear(elementId) {
    const element = document.getElementById(elementId);
    if (element) {
        element.innerHTML = '';
        return element;
    }
    console.warn(`Element with id "${elementId}" not found`);
    return null; // Возвращаем null, чтобы код падал с ошибкой, если ID неверен
}

/**
 * Вспомогательная функция для добавления спиннера в кнопку
 */
export function setLoading(btnElement, isLoading, loadingText = 'Загрузка...') {
    if (!btnElement) return;

    if (isLoading) {
        btnElement.dataset.originalText = btnElement.innerText;
        btnElement.disabled = true;
        btnElement.innerText = loadingText;
    } else {
        btnElement.disabled = false;
        btnElement.innerText = btnElement.dataset.originalText || 'OK';
    }
}

/**
 * Показывает красивое всплывающее уведомление (Toast)
 * @param {string} message - Текст сообщения
 * @param {string} type - 'success' (зеленый), 'error' (красный) или 'info' (синий)
 */
export function toast(message, type = 'success') {
    let container = document.getElementById('toast-container');

    // Если контейнера нет в HTML, создадим его динамически
    if (!container) {
        container = el('div', {
            id: 'toast-container',
            style: {
                position: 'fixed',
                bottom: '20px',
                right: '20px',
                zIndex: '9999',
                display: 'flex',
                flexDirection: 'column',
                gap: '10px'
            }
        });
        document.body.appendChild(container);
    }

    // Цвета для разных типов уведомлений
    const bgColors = {
        success: '#2ecc71', // Зеленый
        error: '#e74c3c',   // Красный
        info: '#3498db'     // Синий
    };

    const toastEl = el('div', {
        style: {
            backgroundColor: bgColors[type] || bgColors.success,
            color: '#fff',
            padding: '12px 20px',
            borderRadius: '5px',
            boxShadow: '0 4px 6px rgba(0,0,0,0.1)',
            opacity: '0',
            transform: 'translateY(20px)',
            transition: 'all 0.3s ease',
            minWidth: '250px',
            fontFamily: 'Segoe UI, sans-serif',
            fontSize: '14px',
            fontWeight: '500'
        }
    }, message);

    container.appendChild(toastEl);

    // Анимация появления
    requestAnimationFrame(() => {
        toastEl.style.opacity = '1';
        toastEl.style.transform = 'translateY(0)';
    });

    // Удаление через 3 секунды
    setTimeout(() => {
        toastEl.style.opacity = '0';
        toastEl.style.transform = 'translateY(20px)';
        toastEl.addEventListener('transitionend', () => toastEl.remove());
    }, 3000);
}