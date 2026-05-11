// admin-header.js — логика выпадающего меню аватара в compact header v2.
//
// Inline-скриптом не работает: nginx/main.py CSP для admin.html — strict,
// 'script-src' без 'unsafe-inline'. Поэтому файл external + грузим через
// <script src="js/admin-header.js"></script>.
//
// Что делает:
//  - читает инициал из sessionStorage('username') и подставляет в круглую
//    кнопку аватара (чтобы не было вспышки «A» → реального имени);
//  - вешает MutationObserver на #adminName — когда app.js заполнит имя
//    после /me, инициал автоматически синхронизируется;
//  - toggle dropdown по клику на аватар, закрытие по клику вне меню /
//    Escape / клику на пункт меню.
//
// Существующий app.js ловит #btnOpenAdminProfile и .logout-btn по тем же
// ID/классам, что были в старой шапке — поэтому JS-логика входа в профиль
// и выхода продолжает работать без правок.

(function () {
    const btn = document.getElementById('avatarBtn');
    const menu = document.getElementById('avatarMenu');
    const initial = document.getElementById('avatarInitial');

    // Инициал из sessionStorage — app.js обновит adminName позже,
    // но первая буква нужна сразу, без вспышки.
    try {
        const u = (sessionStorage.getItem('username') || '').trim();
        if (u && initial) initial.textContent = u.charAt(0).toUpperCase();
    } catch (e) {
        // sessionStorage недоступен (private mode / квота) — пропускаем
    }

    // Следить за изменением adminName (app.js его заполняет после /me),
    // синхронизировать инициал на аватаре.
    const nameEl = document.getElementById('adminName');
    if (nameEl && initial) {
        new MutationObserver(() => {
            const t = (nameEl.textContent || '').trim();
            if (t && t !== 'Загрузка…' && t !== 'Загрузка...') {
                initial.textContent = t.charAt(0).toUpperCase();
            }
        }).observe(nameEl, { childList: true, characterData: true, subtree: true });
    }

    if (!btn || !menu) return;

    function close() {
        menu.hidden = true;
        btn.setAttribute('aria-expanded', 'false');
    }
    function open() {
        menu.hidden = false;
        btn.setAttribute('aria-expanded', 'true');
    }

    btn.addEventListener('click', (e) => {
        e.stopPropagation();
        if (menu.hidden) {
            open();
        } else {
            close();
        }
    });

    document.addEventListener('click', (e) => {
        if (!menu.hidden && !menu.contains(e.target) && e.target !== btn) {
            close();
        }
    });

    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && !menu.hidden) {
            close();
            btn.focus();
        }
    });

    // Закрывать меню после клика на пункт — но через setTimeout, чтобы
    // внутренние обработчики (профиль/logout из app.js) успели сработать.
    menu.querySelectorAll('button').forEach((b) => {
        b.addEventListener('click', () => setTimeout(close, 0));
    });
})();
