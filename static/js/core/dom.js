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