/**
 * Юридический футер + cookie-баннер.
 *
 * Подключается через `<script src="/js/legal-footer.js" defer></script>`
 * в любую публичную страницу (login.html, portal.html).
 *
 * Что делает:
 *   1. Вставляет в конец `<body>` подвал с контактами оператора и ссылками
 *      на политику обработки ПД / правила использования.
 *   2. Показывает cookie-баннер один раз — пока пользователь не нажал «ОК».
 *      Согласие сохраняется в localStorage (имя/версия), при смене версии
 *      баннер появляется снова.
 *
 * Не требует backend — только статика.
 */
(function () {
    'use strict';

    const COOKIE_BANNER_KEY = 'cookie_banner_acked';
    const COOKIE_BANNER_VERSION = '1';
    const PRIVACY_VERSION = '1.0';

    // ─── Подвал ─────────────────────────────────────────────────────
    function injectFooter() {
        // Не дублируем — если футер уже есть на странице, ничего не делаем.
        if (document.getElementById('legal-footer')) return;

        // Определяем тип layout body:
        // - flex/grid-body (login.html) — обычный append «съезжает» вбок,
        //   потому что footer становится flex-item. Используем position:fixed.
        // - normal body (portal.html, privacy.html) — обычный append работает,
        //   footer естественно ложится в конец прокручиваемого контента.
        const bodyStyle = getComputedStyle(document.body);
        const isFlexBody = ['flex', 'inline-flex', 'grid', 'inline-grid'].includes(bodyStyle.display);
        const isDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;

        const footer = document.createElement('footer');
        footer.id = 'legal-footer';
        footer.setAttribute('role', 'contentinfo');
        if (isFlexBody) {
            // Sticky-к-дну для flex-страниц (login).
            footer.style.cssText = `
                position: fixed;
                bottom: 0;
                left: 0;
                right: 0;
                z-index: 50;
                padding: 10px 16px calc(10px + env(safe-area-inset-bottom, 0px));
                text-align: center;
                background: ${isDark ? 'rgba(15, 23, 42, 0.92)' : 'rgba(255, 255, 255, 0.92)'};
                backdrop-filter: blur(12px);
                -webkit-backdrop-filter: blur(12px);
                border-top: 1px solid ${isDark ? 'rgba(255, 255, 255, 0.08)' : 'rgba(0, 0, 0, 0.06)'};
                color: ${isDark ? '#94a3b8' : '#6b7280'};
                font-size: 11px;
                line-height: 1.5;
            `;
        } else {
            // Обычный inline-footer для длинных страниц.
            footer.style.cssText = `
                margin-top: 48px;
                padding: 24px 16px;
                text-align: center;
                border-top: 1px solid rgba(0,0,0,0.08);
                color: #6b7280;
                font-size: 12px;
                line-height: 1.6;
            `;
        }
        const year = new Date().getFullYear();
        // Стартовое содержимое — без реквизитов. После /api/settings/operator-info
        // подставим имя организации и email/телефон (см. fetchOperatorInfo ниже).
        // Ссылки на ДОКУМЕНТЫ (политика + отдельное согласие) держим вместе и
        // ОТДЕЛЬНО от email-контакта — иначе авто-сканеры РКН принимают mailto
        // за «ссылку на политику» и ругаются на битую ссылку.
        footer.innerHTML = `
            <div style="max-width: 760px; margin: 0 auto;">
                <div style="margin-bottom: 8px;">
                    <a href="/privacy.html" style="color: inherit; text-decoration: underline;">Политика обработки персональных данных</a>
                    <span style="margin: 0 8px; opacity: 0.4;">·</span>
                    <a href="/consent.html" style="color: inherit; text-decoration: underline;">Согласие на обработку персональных данных</a>
                </div>
                <div style="opacity: 0.75;">
                    © ${year} <span id="legalFooterOrgName">ЖКХ Лидер</span><span id="legalFooterReqs"></span>
                </div>
                <div style="opacity: 0.6; margin-top: 4px; font-size: 11px;">
                    Email для обращений по персональным данным:
                    <a id="legalFooterContactLink" href="mailto:privacy@asy-tk.ru" style="color: inherit; text-decoration: underline;">privacy@asy-tk.ru</a>
                </div>
            </div>
        `;
        document.body.appendChild(footer);

        // Подтягиваем актуальные реквизиты — публичный endpoint, без авторизации.
        // Один сетевой запрос на страницу, не критично если упадёт — футер
        // покажет дефолтные значения.
        fetch('/api/settings/operator-info', { credentials: 'omit' })
            .then((r) => r.ok ? r.json() : null)
            .then((info) => {
                if (!info) return;
                if (info.operator_name) {
                    const el = document.getElementById('legalFooterOrgName');
                    if (el) el.textContent = info.operator_name;
                }
                if (info.operator_email) {
                    const a = document.getElementById('legalFooterContactLink');
                    if (a) { a.href = 'mailto:' + info.operator_email; a.textContent = info.operator_email; }
                }
                // Реквизиты оператора в подвале (152-ФЗ / ПП-693): ИНН, ОГРН,
                // юр. адрес — чтобы посетитель знал, кому передаёт данные.
                const reqs = [];
                if (info.operator_inn) reqs.push('ИНН ' + info.operator_inn);
                if (info.operator_ogrn) reqs.push('ОГРН ' + info.operator_ogrn);
                if (info.operator_legal_address) reqs.push(info.operator_legal_address);
                const reqsEl = document.getElementById('legalFooterReqs');
                if (reqsEl && reqs.length) reqsEl.textContent = ' · ' + reqs.join(' · ');
            })
            .catch(() => {});
    }

    // ─── Cookie-баннер ──────────────────────────────────────────────
    function shouldShowBanner() {
        try {
            const acked = localStorage.getItem(COOKIE_BANNER_KEY);
            return acked !== COOKIE_BANNER_VERSION;
        } catch {
            return true;  // localStorage недоступен — показываем
        }
    }

    function ackBanner(choice) {
        try {
            localStorage.setItem(COOKIE_BANNER_KEY, COOKIE_BANNER_VERSION);
            if (choice) localStorage.setItem('cookie_banner_choice', choice);
        } catch {}
    }

    function injectBanner() {
        if (!shouldShowBanner()) return;
        if (document.getElementById('cookie-banner')) return;

        const banner = document.createElement('div');
        banner.id = 'cookie-banner';
        banner.setAttribute('role', 'dialog');
        banner.setAttribute('aria-label', 'Использование cookies');
        banner.style.cssText = `
            position: fixed;
            bottom: 16px;
            left: 16px;
            right: 16px;
            max-width: 560px;
            margin: 0 auto;
            background: #ffffff;
            color: #111827;
            border: 1px solid #e5e7eb;
            border-radius: 14px;
            padding: 16px 18px;
            box-shadow: 0 8px 24px rgba(0,0,0,0.12);
            z-index: 9999;
            font-size: 13px;
            line-height: 1.5;
            font-family: -apple-system, BlinkMacSystemFont, 'Inter', 'Segoe UI', Roboto, sans-serif;
            display: flex;
            flex-direction: column;
            gap: 10px;
        `;
        // На тёмной теме фон/текст инверсируем (если страница использует dark mode).
        const isDarkBanner = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
        if (isDarkBanner) {
            banner.style.background = '#1f1f1f';
            banner.style.color = '#f3f4f6';
            banner.style.borderColor = '#2a2a2a';
        }

        banner.innerHTML = `
            <div>
                Этот сайт использует cookies и аналогичные технологии
                (localStorage, sessionStorage) для работы личного кабинета и сохранения
                пользовательских настроек. Подробнее — в
                <a href="/privacy.html" style="color: #2563eb; font-weight: 600;">Политике обработки персональных данных</a>.
            </div>
            <div style="display: flex; gap: 8px; justify-content: flex-end; flex-wrap: wrap;">
                <button id="cookie-banner-reject" style="
                    padding: 8px 18px;
                    border-radius: 10px;
                    border: 1px solid ${isDarkBanner ? '#3f3f46' : '#d1d5db'};
                    background: transparent;
                    color: inherit;
                    font-weight: 600;
                    font-size: 13px;
                    cursor: pointer;
                ">Отклонить все</button>
                <button id="cookie-banner-ack" style="
                    padding: 8px 18px;
                    border-radius: 10px;
                    border: none;
                    background: #2563eb;
                    color: white;
                    font-weight: 600;
                    font-size: 13px;
                    cursor: pointer;
                ">Принять</button>
            </div>
        `;
        document.body.appendChild(banner);
        const closeBanner = (choice) => {
            ackBanner(choice);
            banner.style.transition = 'opacity 200ms ease';
            banner.style.opacity = '0';
            setTimeout(() => banner.remove(), 220);
        };
        const ackBtn = document.getElementById('cookie-banner-ack');
        const rejectBtn = document.getElementById('cookie-banner-reject');
        // Сайт не ставит сторонних/аналитических cookie — «Отклонить все» просто
        // фиксирует выбор; функциональное хранилище (вход в ЛК) остаётся.
        if (ackBtn) ackBtn.addEventListener('click', () => closeBanner('accepted'));
        if (rejectBtn) rejectBtn.addEventListener('click', () => closeBanner('rejected'));
    }

    // ─── Init ─────────────────────────────────────────────────────
    function init() {
        injectFooter();
        injectBanner();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    // Экспортируем для использования из PWA (там модульная архитектура).
    window.__legalFooter = {
        version: PRIVACY_VERSION,
        injectFooter,
        injectBanner,
    };
})();
