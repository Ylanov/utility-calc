// static/js/modules/client-app-download.js
//
// Карточка «Скачать приложение» в личном кабинете жильца.
//
// Запрашивает /api/app/latest при инициализации. Если есть опубликованная
// версия — показывает карточку: версию, release notes, кнопку скачать APK,
// QR-код со ссылкой (чтобы можно было отсканировать с телефона).
//
// Если опубликованных версий нет — карточка скрыта, ничего не делаем.

import { api } from '../core/api.js';

/**
 * QR через наш собственный endpoint /api/qr (Python qrcode библиотека).
 * Раньше использовался quickchart.io — он нарушал CSP (img-src 'self' data: blob:)
 * и создавал внешнюю зависимость. Теперь самодостаточно.
 */
function buildQrUrl(text, boxSize = 8) {
    const encoded = encodeURIComponent(text);
    return `/api/qr?text=${encoded}&box_size=${boxSize}&border=2`;
}

function fmtSize(bytes) {
    if (!bytes) return '';
    const mb = bytes / 1024 / 1024;
    return mb >= 1 ? `${mb.toFixed(1)} МБ` : `${(bytes / 1024).toFixed(0)} КБ`;
}

export const ClientAppDownload = {
    async init() {
        const card = document.getElementById('appDownloadCard');
        if (!card) return;

        // silentNotFound: для свежей установки сервера ещё не загружено ни одного APK,
        // /api/app/latest корректно отвечает 404 — это не ошибка, не пишем в консоль.
        // Карточка просто остаётся скрытой (display:none по умолчанию).
        let info;
        try {
            info = await api.get('/app/latest?platform=android', { silentNotFound: true });
        } catch (e) {
            // 5xx и прочие реальные ошибки — игнорируем тихо, портал должен работать.
            return;
        }
        if (!info || !info.download_url) return;

        // Полный URL — нужен для QR (телефон должен открыть его напрямую)
        const fullUrl = `${window.location.origin}${info.download_url}`;

        const versionEl = document.getElementById('appDownloadVersion');
        if (versionEl) {
            versionEl.innerHTML =
                `Версия: <b>${info.version}</b>` +
                (info.file_size ? ` · ${fmtSize(info.file_size)}` : '');
        }

        const notesEl = document.getElementById('appDownloadNotes');
        if (notesEl && info.release_notes) {
            notesEl.textContent = info.release_notes;
            notesEl.style.display = 'block';
        }

        const btn = document.getElementById('appDownloadBtn');
        if (btn) {
            btn.href = info.download_url;
            // download-атрибут хочет имя — берём из URL
            const fileName = info.download_url.split('/').pop();
            btn.setAttribute('download', fileName);
        }

        const qrContainer = document.getElementById('appDownloadQR');
        if (qrContainer) {
            // box_size=7 даёт результирующий PNG ~200x200px при 25-30 квадратах,
            // что хорошо смотрится в контейнере 160x160px (image-rendering crisp).
            const img = document.createElement('img');
            img.src = buildQrUrl(fullUrl, 7);
            img.alt = 'QR-код для скачивания APK';
            img.style.width = '100%';
            img.style.height = '100%';
            img.style.display = 'block';
            img.style.imageRendering = 'pixelated';  // QR должен быть резким
            img.loading = 'lazy';
            qrContainer.innerHTML = '';
            qrContainer.appendChild(img);
        }

        card.style.display = 'block';
    },
};
