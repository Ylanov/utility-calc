// static/js/portal.js
//
// Раньше этот скрипт был inline в portal.html — мешал убрать
// 'unsafe-inline' из CSP script-src. Сейчас вынесен в external,
// логика без изменений.
//
// Назначение: при загрузке портала тянем метаданные о последней
// опубликованной версии Android-APK через /api/app/latest и
// рендерим блок «Скачать APK» с QR-кодом или показываем заглушку,
// если публикаций нет.

(async function () {
    const link = document.getElementById('portalAppDownloadLink');
    const label = document.getElementById('portalAppVersionLabel');
    const qrBlock = document.getElementById('portalAppQR');
    const block = document.getElementById('portalAppBlock');

    // Показываем заглушку «временно недоступно» вместо тихого молчания —
    // раньше юзер не знал, есть приложение или нет, а href="#" давал
    // скачивание мусорного 110-байтного JSON-404 с обычным браузером.
    const showUnavailable = (reason) => {
        if (label) label.textContent = 'Временно недоступно';
        if (link) {
            link.href = '#';
            link.removeAttribute('download');
            link.style.background = '#9aa0a6';
            link.style.cursor = 'not-allowed';
            link.addEventListener('click', (e) => e.preventDefault(), { once: true });
            link.title = reason || 'Админ ещё не опубликовал APK';
        }
        if (qrBlock) {
            // textContent — не innerHTML; CSP-чистая операция и нет XSS surface.
            qrBlock.textContent = '';
            const span = document.createElement('span');
            span.style.color = '#a0aec0';
            span.style.fontSize = '11px';
            span.textContent = 'QR недоступен';
            qrBlock.appendChild(span);
        }
        if (block) block.style.display = 'block';
    };

    try {
        const resp = await fetch('/api/app/latest?platform=android', { credentials: 'omit' });
        if (!resp.ok) {
            // /latest теперь фильтрует релизы, у которых файл пропал с диска.
            // 404 тут = «нет ни одной живой публикации» — показываем заглушку.
            showUnavailable('Публикаций с доступным файлом нет');
            return;
        }
        const info = await resp.json();
        if (!info || !info.download_url) {
            showUnavailable();
            return;
        }

        link.href = info.download_url;
        link.setAttribute('download', info.download_url.split('/').pop());
        label.textContent = 'Скачать APK · v' + info.version;

        // QR генерится нашим бэком — не нарушает CSP (img-src 'self')
        // и не зависит от внешних сервисов вроде quickchart.io.
        const fullUrl = window.location.origin + info.download_url;
        const qrSrc = '/api/qr?text=' + encodeURIComponent(fullUrl) + '&box_size=6&border=2';
        const img = document.createElement('img');
        img.src = qrSrc;
        img.alt = 'QR-код для скачивания APK';
        img.style.width = '100%';
        img.style.height = '100%';
        img.style.display = 'block';
        img.loading = 'lazy';
        qrBlock.textContent = '';  // убираем placeholder «QR-код»
        qrBlock.appendChild(img);

        block.style.display = 'block';
    } catch (e) {
        // Сетевая ошибка / парсинг JSON — тоже показываем заглушку,
        // чтобы не было «серой» кнопки с битой ссылкой.
        showUnavailable('Ошибка загрузки');
    }
})();
