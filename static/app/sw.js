/**
 * Service Worker — PWA жильца.
 *
 * Главная задача (Фаза 1): cache-first для статики (app-shell), чтобы
 * после первой загрузки портал открывался моментально и работал даже
 * без интернета (показывал что было закэшировано — баланс, история, и т.п.).
 *
 * Будущие фазы:
 *   - Push notifications handler (Фаза 3): период открыт, квитанция готова
 *   - Background sync для отложенной подачи показаний (если нет связи)
 *
 * Версионирование: при изменении статики поднимаем CACHE_VERSION, старый
 * кэш чистится в `activate`. Иначе пользователь будет видеть старую версию
 * до полной перерегистрации SW.
 */

const CACHE_VERSION = 'v1';
const CACHE_NAME = `jkh-lider-app-shell-${CACHE_VERSION}`;

// App-shell — критичные ресурсы для отрисовки UI. Грузятся при install.
// Версионирование сделаем на этапе деплоя (после Фазы 2, пока — простой список).
const APP_SHELL = [
    '/app/',
    '/app/index.html',
    '/app/css/main.css',
    '/app/css/components.css',
    '/app/js/main.js',
    '/app/js/api.js',
    '/app/js/auth.js',
    '/app/js/router.js',
    '/app/js/toast.js',
    '/app/js/screens/home.js',
    '/app/manifest.webmanifest',
    '/app/icons/icon.svg',
];

self.addEventListener('install', (event) => {
    // skipWaiting — новый SW активируется без ожидания закрытия всех табов.
    // Иначе пользователь будет видеть старую версию пока не закроет браузер.
    self.skipWaiting();
    event.waitUntil(
        caches.open(CACHE_NAME).then((cache) =>
            // addAll() атомарен — если хоть один файл не загрузится, install fails.
            // Это правильно: лучше остаться без SW чем с битым кэшем.
            cache.addAll(APP_SHELL).catch((err) => {
                console.warn('[SW] App-shell cache failed (продолжаем без кэша):', err);
            })
        )
    );
});

self.addEventListener('activate', (event) => {
    // clients.claim — берём контроль над всеми открытыми вкладками сразу,
    // без reload. Иначе SW начнёт работать только после следующей навигации.
    event.waitUntil(
        Promise.all([
            self.clients.claim(),
            // Чистим старые версии кэша (после bump CACHE_VERSION).
            caches.keys().then((keys) =>
                Promise.all(
                    keys
                        .filter((k) => k.startsWith('jkh-lider-app-shell-') && k !== CACHE_NAME)
                        .map((k) => caches.delete(k))
                )
            ),
        ])
    );
});

self.addEventListener('fetch', (event) => {
    const req = event.request;
    const url = new URL(req.url);

    // НЕ кэшируем:
    // - не-GET (POST/PUT/DELETE — это мутации, всегда онлайн)
    // - /api/* — данные жильца меняются часто, всегда свежие с сервера
    // - чужие домены (CDN, fonts) — пусть браузер сам решает
    if (
        req.method !== 'GET' ||
        url.origin !== self.location.origin ||
        url.pathname.startsWith('/api/')
    ) {
        return;  // дефолтное поведение (без SW)
    }

    // Только для /app/* — наш scope. Остальное (старый портал /, /admin/)
    // не трогаем чтобы не сломать существующее.
    if (!url.pathname.startsWith('/app/')) {
        return;
    }

    // Stale-while-revalidate: отдаём из кэша мгновенно, в фоне обновляем.
    // Идеально для статики которая редко меняется.
    event.respondWith(
        caches.open(CACHE_NAME).then(async (cache) => {
            const cached = await cache.match(req);
            const fetchPromise = fetch(req)
                .then((networkRes) => {
                    // Кэшируем только успешные ответы. 404/500 не сохраняем.
                    if (networkRes && networkRes.ok) {
                        cache.put(req, networkRes.clone()).catch(() => {});
                    }
                    return networkRes;
                })
                .catch(() => cached);  // оффлайн — отдаём кэш если есть
            // Если есть кэш — отдаём его мгновенно, а fetch продолжается в фоне.
            return cached || fetchPromise;
        })
    );
});

// =========================================================================
// PUSH NOTIFICATIONS — заготовка для Фазы 3.
// Сейчас НЕ активна (нет VAPID-ключей на сервере, нет subscription endpoint).
// Когда дойдём — раскомментировать + добавить handler на бэке.
// =========================================================================
self.addEventListener('push', (event) => {
    if (!event.data) return;
    let payload;
    try {
        payload = event.data.json();
    } catch {
        payload = { title: 'ЖКХ Лидер', body: event.data.text() };
    }
    const title = payload.title || 'ЖКХ Лидер';
    const options = {
        body: payload.body || '',
        icon: '/app/icons/icon-192.png',
        badge: '/app/icons/icon-192.png',
        data: payload.data || {},
        tag: payload.tag || 'jkh-notify',
        renotify: true,
    };
    event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
    event.notification.close();
    const targetUrl = event.notification.data?.url || '/app/';
    event.waitUntil(
        self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((wins) => {
            // Если есть уже открытая вкладка PWA — фокусируем её.
            const existing = wins.find((w) => w.url.includes('/app/'));
            if (existing) {
                existing.focus();
                if (event.notification.data?.url) existing.navigate(targetUrl);
                return;
            }
            return self.clients.openWindow(targetUrl);
        })
    );
});
