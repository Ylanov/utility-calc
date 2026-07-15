// static/js/modules/debts.js
//
// Вкладка «Долги 1С» — единая панель для финансиста:
//   * KPI-панель сверху (суммарный долг/переплата, должники, ср. долг, последний импорт)
//   * Импорт Excel с живым прогрессом задачи
//   * История импортов с откатом и просмотром «не найденных» ФИО
//   * Таблица с фильтрами (должники/переплаты/общежитие/мин. долг), сортировкой по колонкам,
//     цветовыми чипами уровней долга
//   * Модалка корректировки (замена prompt-цепочки): счёт, сумма, шаблон причины + комментарий
//   * Экспорт текущей выборки в Excel

import { api } from '../core/api.js';
import { el, toast, setLoading, showConfirm } from '../core/dom.js';
import { formatRoomAddress } from '../core/format-address.js';

function esc(s) {
    if (s == null) return '';
    return String(s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function fmtMoney(v) {
    const n = Number(v || 0);
    return n.toLocaleString('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fmtDateTime(iso) {
    if (!iso) return '—';
    try { return new Date(iso).toLocaleString('ru-RU'); } catch { return iso; }
}

export const DebtsModule = {
    isInitialized: false,
    state: {
        page: 1, limit: 50, total: 0, search: '',
        mode: 'users',  // 'users' | 'rooms' — учёт по жильцам или по квартирам
        viewPeriodId: '',  // период ПРОСМОТРА долгов ('' = авто: активный/последний импорт)
        filterType: '', dormitory: '', minDebt: '',
        hideEmpty: true,  // Bug AB: по умолчанию скрываем пустые
        sortBy: 'room', sortDir: 'asc',
        importTaskId: null, pollTimer: null, isUploading: false, lastRequestId: 0,
        currentPollId: null,
    },

    init() {
        this.cacheDOM();
        // Bug AB: синхронизируем стартовое состояние чекбокса с state
        // (HTML может прийти с другим default'ом).
        if (this.dom.hideEmpty) {
            this.state.hideEmpty = !!this.dom.hideEmpty.checked;
        }
        if (!this.isInitialized) {
            this.bindEvents();
            this.isInitialized = true;
        }
        // Регистрируем глобальный хэндлер — аналитик из «Центр анализа»
        // («Сверка 1С») может позвать модалку «не найдены» по log_id.
        // Живёт на window, чтобы не плодить event-bus.
        window.__openDebtsNotFound = (logId) => {
            const tabBtn = document.querySelector('.tab-btn[data-tab="debts"]');
            tabBtn?.click();
            setTimeout(() => this.openNotFoundModal(logId), 120);
        };
        this.loadViewPeriods();
        this.loadStats();
        this.loadUnassigned();
        this.loadDormitories();
        this.loadDebtPeriods();
        this.loadUsers();
        this.loadImportHistory();
        this.loadGisgmpStatus();
        this.loadControl();
        this.loadOnecStatus();
        this.loadStagedStatus();
        // Авто-обновление статуса ГИС ГМП раз в 15с — «живой» статус (тикает возраст
        // опроса релея, прогресс актуализации). Только когда вкладка Долги видима.
        if (this._gisStatusTimer) clearInterval(this._gisStatusTimer);
        this._gisStatusTimer = setInterval(() => {
            const box = this.dom.gisgmpStatus;
            if (box && box.offsetParent !== null) {
                this.loadGisgmpStatus();
                this.loadOnecStatus();
                this.loadStagedStatus();
            }
        }, 15000);
    },

    // ─── Гейт «Выгрузить»: статус черновиков + публикация долгов ───────────
    async loadStagedStatus() {
        const box = this.dom.debtsStagedStatus;
        if (!box) return;
        try {
            const s = await api.get('/financier/debts/staged-status');
            const st = s.staged || {};
            if (!s.has_staged) {
                box.innerHTML = '<span style="color:#9ca3af;">Черновиков нет — загрузите Excel 1С, затем «Выгрузить».</span>';
                if (this.dom.btnPublishDebts) this.dom.btnPublishDebts.disabled = true;
                return;
            }
            const parts = [];
            for (const acc of ['209', '205']) {
                const d = st[acc];
                if (d) parts.push(`<b>${acc}</b>: ${d.residents} жильцов${d.not_found ? `, не найдено ${d.not_found}` : ''} <span style="color:#9ca3af;">(${d.at ? new Date(d.at).toLocaleString('ru-RU') : '—'})</span>`);
            }
            box.innerHTML = `<span style="color:#16a34a;">📥 Черновик готов к выгрузке:</span><br>${parts.join('<br>')}`;
            if (this.dom.btnPublishDebts) this.dom.btnPublishDebts.disabled = false;
        } catch (e) {
            box.innerHTML = '<span style="color:#9ca3af;">Статус черновиков недоступен.</span>';
        }
    },

    /** Пересопоставить «не найденных» в черновиках 1С с текущей базой (после
     *  добавления/заселения жильцов). Долг на лицевом счёте (ФИО): привязывает
     *  ВСЕХ, кто есть в базе — с комнатой ИЛИ без (комната подцепится позже). */
    async rematchBase() {
        try {
            const r = await api.post('/financier/debts/rematch-base', {});
            let msg = `Привязано долгов: ${r.attached}.`;
            if (r.in_base_no_room) msg += ` Из них ${r.in_base_no_room} пока без комнаты (подцепятся при заселении).`;
            if (r.still_not_found) msg += ` Совсем нет в базе: ${r.still_not_found}.`;
            toast(msg, r.attached ? 'success' : 'info');
            this.loadStagedStatus();
            this.reload();
        } catch (e) { toast('Ошибка пересопоставки: ' + (e?.message || e), 'error'); }
    },

    async publishDebts() {
        if (!await showConfirm('Выгрузить долги жильцам? Возьму последние черновики 1С (209/205) + активные ГИС-оверрайды и запишу долги в показания активного периода. Полная замена по выгружаемому счёту (кого нет в черновике → 0). Снимок до — для отката через историю.', { title: 'Выгрузить долги', confirmText: 'Выгрузить' })) return;
        try {
            const r = await api.post('/financier/debts/publish', {});
            toast(`Выгружено: счета ${(r.accounts || []).join('+')}, обновлено ${r.updated}, создано ${r.created}. Источник долгов — только 1С.`, 'success');
            this.loadStagedStatus();
            this.reload();
        } catch (e) { toast('Ошибка выгрузки: ' + (e?.message || e), 'error'); }
    },

    // ─── Авто-подгрузка ГИС ГМП (релей, управление отсюда) ─────────────────
    async loadGisgmpStatus() {
        const box = this.dom.gisgmpStatus;
        if (!box) return;
        try {
            const s = await api.get('/financier/gisgmp/status');
            if (!s.configured) {
                box.innerHTML = '<span style="color:#b91c1c;">⚠ Токен GISGMP_SYNC_TOKEN не задан в .env сервера ЖКХ — релей не примет данные.</span>';
                return;
            }
            const r = s.relay || {};
            if (this.dom.gisgmpEnabled) this.dom.gisgmpEnabled.checked = r.enabled !== false;
            if (this.dom.gisgmpMonths) {
                const mb = r.months_back ?? 999;
                this.dom.gisgmpMonths.value = mb >= 24 ? '999' : (mb >= 9 ? '12' : '6');
            }
            if (this.dom.gisgmpHour) this.dom.gisgmpHour.value = r.daily_hour ?? 22;

            const parts = [];
            // Онлайн + интервал опроса + версия релея (актуальна ли).
            const pollS = r.relay_poll_seconds || 120;
            let rl = r.online
                ? 'Релей: <b style="color:#047857;">🟢 онлайн</b>'
                : 'Релей: <b style="color:#b91c1c;">🔴 офлайн</b>';
            if (r.poll_age_seconds != null) rl += ` · опрос каждые ${pollS}с (последний ${Math.round(r.poll_age_seconds)}с назад)`;
            if (r.relay_version) {
                const up = r.relay_latest_version && r.relay_latest_version !== r.relay_version;
                rl += ` · версия <b>${esc(r.relay_version)}</b>`
                    + (up ? ` <span style="color:#d97706;">→ доступно обновление до ${esc(r.relay_latest_version)} (жми «Обновить релей»)</span>`
                          : (r.relay_latest_version ? ' <span style="color:#047857;">(актуальна)</span>' : ''));
            } else {
                rl += ' · <span style="color:#d97706;">версия неизвестна — обнови релей, чтобы он начал её сообщать</span>';
            }
            parts.push(rl);
            parts.push(r.last_run_at
                ? `Последний запуск релея: <b>${new Date(r.last_run_at).toLocaleString('ru-RU')}</b>`
                : 'Релей ещё ни разу не запускался (проверь, что он установлен на ВМ).');
            if (r.last_status) {
                const ok = r.last_status === 'ok';
                parts.push(`Результат: <b style="color:${ok ? '#047857' : '#b91c1c'}">${ok ? 'успех' : 'ошибка'}</b>`
                    + (r.last_message ? ` — ${esc(r.last_message)}` : ''));
            }
            const f = s.findings;
            if (f) {
                parts.push(`Найдено: начислений <b>${f.total_charges ?? 0}</b>, жильцов с долгом <b>${f.residents ?? 0}</b>, `
                    + `сопоставлено <b>${f.matched ?? 0}</b>, не найдено <b>${f.not_found ?? 0}</b>`
                    + (f.synced_at ? ` (${new Date(f.synced_at).toLocaleString('ru-RU')})` : ''));
            }
            if (r.passport_username) {
                parts.push(`Учётка реестра: <b>${esc(r.passport_username)}</b>`);
                if (this.dom.relayUser && !this.dom.relayUser.value) this.dom.relayUser.value = r.passport_username;
            }
            const pend = r.pending || {};
            const pl = [];
            if (pend.self_update) pl.push('обновление');
            if (pend.restart) pl.push('перезапуск');
            if (pend.credentials) pl.push('смена учётки');
            if (pl.length) parts.push(`<span style="color:#d97706;">⏳ В очереди для релея: ${pl.join(', ')} — применится на ближайшем опросе (~2 мин)</span>`);
            try {
                const act = await api.get('/financier/gisgmp/actualize-status');
                // Завершённый прогон старше суток не показываем — иначе старая
                // ошибка «висит» в карточке без возможности убрать.
                const finAgeH = act && act.finished && act.finished_at
                    ? (Date.now() - new Date(act.finished_at).getTime()) / 3600000 : 0;
                if (act && act.total && !(act.finished && finAgeH > 24)) {
                    const pct = Math.round((act.done || 0) / act.total * 100);
                    const err = act.finished && act.message && /ошибк/i.test(act.message);
                    const st = act.running ? '⏳ отправка'
                        : (act.finished ? (err ? `❌ ${act.message}` : '📨 отправлено — итог в «Истории актуализаций»') : 'в очереди');
                    parts.push(`<span style="color:${err ? '#dc2626' : '#2563eb'};">Актуализация: <b>${st}</b> — ${act.done || 0} из ${act.total} (${pct}%, ok ${act.ok || 0}, ошибок ${act.fail || 0})</span>`);
                }
            } catch (e) { /* нет очереди актуализации — норм */ }
            parts.push(`<span style="color:#9ca3af; font-size:11px;">🔄 обновлено ${new Date().toLocaleTimeString('ru-RU')} · авто-обновление каждые 15с</span>`);
            box.innerHTML = parts.join('<br>');
        } catch (e) {
            box.textContent = 'Не удалось загрузить статус ГИС ГМП.';
        }
    },

    // ─── Контроль 1С↔ГИС: светофор сверки ──────────────────────────────────
    // 1С — эталон долгов; ГИС подтягиваем к нему. Снапшот пересчитывают сбор
    // ГИС и выгрузка 1С; кнопка «Пересчитать» дёргает ?refresh=true.
    async loadControl(refresh = false) {
        const box = document.getElementById('gis1cControl');
        if (!box) return;
        try {
            const c = await api.get(`/financier/gisgmp/control${refresh ? '?refresh=true' : ''}`);
            if (!c || !c.matched) { box.innerHTML = ''; return; }
            const f = c.flags || {};
            const nOk = f.ok || 0;
            const nOver = (f.gis_more || 0) + (f.only_gis || 0);   // ГИС завышен → актуализация
            const nUnder = f.c1_more || 0;                          // ГИС занижен → дотянуть
            const nAbsent = f.only_1c || 0;                         // в ГИС нет вовсе
            const tile = (icon, num, label, color, hint) =>
                `<div title="${esc(hint)}" style="flex:1 1 120px; min-width:120px; text-align:center; padding:8px 6px; border-radius:8px; background:${color}14; border:1px solid ${color}44;">` +
                `<div style="font-size:20px; font-weight:700; color:${color};">${icon} ${num}</div>` +
                `<div style="font-size:11px; color:var(--text-secondary); margin-top:2px;">${label}</div></div>`;
            const topRows = (c.top || []).map(t =>
                `<tr><td style="padding:2px 8px 2px 0;">${esc(t.fio || '')}</td>` +
                `<td style="text-align:right; padding:2px 8px;">${fmtMoney(t.c1)}</td>` +
                `<td style="text-align:right; padding:2px 8px;">${fmtMoney(t.gis)}</td>` +
                `<td style="text-align:right; padding:2px 0; color:${(t.delta || 0) > 0 ? '#dc2626' : '#d97706'};">${(t.delta || 0) > 0 ? '+' : ''}${fmtMoney(t.delta)}</td></tr>`
            ).join('');
            const namesakes = c.namesakes || [];
            box.innerHTML =
                `<div style="border:1px solid var(--border-color,#e5e7eb); border-radius:10px; padding:10px 12px;">` +
                `<div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:6px; margin-bottom:8px;">` +
                `<b style="font-size:13px;">🚦 Контроль 1С↔ГИС</b>` +
                `<span style="font-size:11px; color:#9ca3af;">сверка от ${c.ts ? new Date(c.ts).toLocaleString('ru-RU') : '—'} · сопоставлено ${c.matched}` +
                ` · <a href="#" id="gis1cControlRefresh" style="color:#2563eb;">пересчитать</a></span></div>` +
                `<div style="display:flex; gap:8px; flex-wrap:wrap;">` +
                tile('✅', nOk, 'совпадает', '#16a34a', 'Суммы 1С и ГИС равны — всё правильно') +
                tile('🔺', nOver, 'ГИС завышен', '#dc2626', 'В ГИС начислено больше, чем в 1С — жми «Актуализация → Актуализировать расхождения»: реестр аннулирует лишнее') +
                tile('🔻', nUnder, 'ГИС занижен', '#d97706', 'В ГИС меньше, чем в 1С — «Актуализация → Дотянуть расхождения» (глубокий переопрос) либо 1С ещё не довыгрузил в ГИС') +
                tile('⬜', nAbsent, 'нет в ГИС', '#6b7280', 'Человек есть в 1С, а в реестре ГИС его начислений нет — выгрузка 1С→ГИС не прошла') +
                tile('👻', c.orphans || 0, 'нет в базе', '#7c3aed', 'Есть в 1С/ГИС, но нет в базе жильцов — «Сверки → Создать отсутствующих в базе»') +
                `</div>` +
                `<div style="font-size:12px; color:var(--text-secondary); margin-top:8px;">` +
                `Итого: 1С <b>${fmtMoney(c.sum_1c)} ₽</b> · ГИС <b>${fmtMoney(c.sum_gis)} ₽</b> · разница ` +
                `<b style="color:${Math.abs(c.delta || 0) < 1 ? '#16a34a' : ((c.delta || 0) > 0 ? '#dc2626' : '#d97706')};">${(c.delta || 0) > 0 ? '+' : ''}${fmtMoney(c.delta)} ₽</b>` +
                `${(c.delta || 0) < -1 ? ' (ГИС недовыгружен — эталон всё равно 1С)' : ''}</div>` +
                ((nOver || nUnder) ?
                    `<div style="margin-top:8px; display:flex; gap:8px; flex-wrap:wrap;">` +
                    (nOver ? `<button id="gis1cBtnActualize" class="action-btn secondary-btn" style="font-size:12px; color:#dc2626; border-color:#fecaca;">🔺 Актуализировать завышенных (${nOver})</button>` : '') +
                    (nUnder ? `<button id="gis1cBtnRecheck" class="action-btn secondary-btn" style="font-size:12px; color:#d97706; border-color:#fde68a;">🔻 Дотянуть заниженных (${nUnder})</button>` : '') +
                    `</div>` : '') +
                (topRows ?
                    `<details style="margin-top:8px;"><summary style="cursor:pointer; font-size:12px; color:#2563eb;">Топ расхождений (${(c.top || []).length})</summary>` +
                    `<table style="font-size:12px; margin-top:6px; border-collapse:collapse;">` +
                    `<tr style="color:#9ca3af;"><td style="padding:2px 8px 2px 0;">ФИО</td><td style="text-align:right; padding:2px 8px;">1С</td><td style="text-align:right; padding:2px 8px;">ГИС</td><td style="text-align:right;">Δ (ГИС−1С)</td></tr>` +
                    topRows + `</table></details>` : '') +
                (namesakes.length ?
                    `<details style="margin-top:6px;"><summary style="cursor:pointer; font-size:12px; color:#b45309;">⚠ Тёзки: ${namesakes.length} ФИО под несколькими лицевыми счетами — проверьте дубли базы</summary>` +
                    `<div style="font-size:12px; margin-top:6px; color:var(--text-secondary);">` +
                    namesakes.map(n => `${esc(n.fio)} ×${n.count}`).join(' · ') +
                    `</div></details>` : '') +
                `</div>`;
            document.getElementById('gis1cControlRefresh')?.addEventListener('click', async (e) => {
                e.preventDefault();
                e.target.textContent = 'пересчитываю…';
                try { await this.loadControl(true); } catch { }
            });
            document.getElementById('gis1cBtnActualize')?.addEventListener('click', () => this.actualizeGisgmp());
            document.getElementById('gis1cBtnRecheck')?.addEventListener('click', () => this.recheckGisgmp());
        } catch (e) {
            box.innerHTML = ''; // вспомогательная карточка — тихо
        }
    },

    async saveGisgmpRelay() {
        try {
            const dh = parseInt(this.dom.gisgmpHour?.value, 10);
            await api.put('/financier/gisgmp/relay-config', {
                enabled: !!this.dom.gisgmpEnabled?.checked,
                months_back: parseInt(this.dom.gisgmpMonths?.value, 10) || 999,
                daily_hour: Number.isNaN(dh) ? 22 : dh,
            });
            toast('Настройки релея сохранены', 'info');
            this.loadGisgmpStatus();
        } catch (e) {
            toast('Ошибка сохранения: ' + (e?.message || e), 'error');
        }
    },

    async runGisgmpNow() {
        try {
            await api.post('/financier/gisgmp/run-now', {});
            toast('Команда отправлена — релей запустится в течение пары минут', 'info');
        } catch (e) {
            toast('Ошибка: ' + (e?.message || e), 'error');
        }
    },

    // ─── 1С (БГУ): авто-подгрузка через релей (браузер) ────────────────────
    async loadOnecStatus() {
        const box = this.dom.onecStatus;
        if (!box) return;
        try {
            const s = await api.get('/financier/onec/status');
            if (this.dom.onecEnabled) this.dom.onecEnabled.checked = !!s.enabled;
            if (this.dom.onecAccNaem && !this.dom.onecAccNaem.value) this.dom.onecAccNaem.value = s.account_naem || '';
            if (this.dom.onecAccComm && !this.dom.onecAccComm.value) this.dom.onecAccComm.value = s.account_comm || '';
            if (this.dom.onecHour && !this.dom.onecHour.value) this.dom.onecHour.value = s.daily_hour ?? 6;
            if (this.dom.onecBaseUrl && !this.dom.onecBaseUrl.value) this.dom.onecBaseUrl.value = s.base_url || '';
            if (this.dom.onecInfobase && !this.dom.onecInfobase.value) this.dom.onecInfobase.value = s.infobase_path || '';
            if (this.dom.onecLogin && !this.dom.onecLogin.value && s.login) this.dom.onecLogin.value = s.login;

            const parts = [];
            parts.push(s.online
                ? 'Релей: <b style="color:#047857;">🟢 онлайн</b>'
                : 'Релей: <b style="color:#b91c1c;">🔴 офлайн</b> (поднимется при опросе)');
            parts.push(`Учётка 1С: <b>${s.has_password ? (esc(s.login || '') + ' ✓') : '<span style=\"color:#b91c1c;\">не задана</span>'}</b>`);
            if (s.creds_pending) parts.push('<span style="color:#d97706;">⏳ учётка ждёт выдачи релею (~2 мин)</span>');
            parts.push(s.last_run_at
                ? `Последний запуск: <b>${fmtDateTime(s.last_run_at)}</b>`
                : 'Ещё не запускался.');
            if (s.last_status) {
                const ok = s.last_status === 'ok' || s.last_status === 'probe';
                parts.push(`Результат: <b style="color:${ok ? '#047857' : '#b91c1c'}">${esc(s.last_status)}</b>`
                    + (s.last_message ? ` — ${esc(s.last_message)}` : ''));
            }
            if (s.last_count_205 || s.last_count_209) {
                parts.push(`Собрано ОСВ: наём(205) <b>${s.last_count_205 || 0}</b>, коммуналка(209) <b>${s.last_count_209 || 0}</b> → авто-выгрузка жильцам (статус ниже).`);
            }
            const a = s.last_autopublish;
            if (a && a.status === 'published') {
                parts.push(`Авто-выгрузка жильцам: <b style="color:#047857;">✓ ${fmtDateTime(a.at)}</b> — обновлено ${a.updated || 0}, создано ${a.created || 0}.`);
            } else if (a && a.status === 'guard_tripped') {
                parts.push(`<b style="color:#b91c1c;">⚠ Авто-выгрузка ОСТАНОВЛЕНА предохранителем (${fmtDateTime(a.at)}):</b> сбор обнулил бы ${a.would_zero}/${a.prev_nonzero} ненулевых долгов — похоже на сбой парсинга. Черновик НЕ выгружен. Проверь и, если данные верны, нажми «Выгрузить» вручную.`);
            } else if (a && a.status === 'no_active_period') {
                parts.push(`Авто-выгрузка: <span style="color:#d97706;">нет активного периода</span> (${fmtDateTime(a.at)}).`);
            }
            box.innerHTML = parts.join('<br>');
        } catch (e) {
            box.textContent = 'Не удалось загрузить статус 1С.';
        }
    },

    async saveOnecConfig() {
        try {
            // Шлём только заполненные поля — чтобы клик/чекбокс до прогрузки статуса
            // не затёр настройки сервера пустыми строками (enabled — всегда).
            const body = { enabled: !!this.dom.onecEnabled?.checked };
            const naem = (this.dom.onecAccNaem?.value || '').trim();
            const comm = (this.dom.onecAccComm?.value || '').trim();
            const base = (this.dom.onecBaseUrl?.value || '').trim();
            const ib = (this.dom.onecInfobase?.value || '').trim();
            const dh = parseInt(this.dom.onecHour?.value, 10);
            if (naem) body.account_naem = naem;
            if (comm) body.account_comm = comm;
            if (base) body.base_url = base;
            if (ib) body.infobase_path = ib;
            if (!Number.isNaN(dh)) body.daily_hour = dh;
            await api.put('/financier/onec/config', body);
            toast('Настройки 1С сохранены', 'info');
            this.loadOnecStatus();
        } catch (e) {
            toast('Ошибка сохранения: ' + (e?.message || e), 'error');
        }
    },

    async saveOnecCreds() {
        const u = (this.dom.onecLogin?.value || '').trim();
        const p = this.dom.onecPass?.value || '';
        if (!u || !p) { toast('Укажи логин и пароль 1С', 'warning'); return; }
        if (!await showConfirm('Сохранить учётку 1С для релея? Пароль шифруется (Fernet) и уйдёт релею один раз на ближайшем опросе (~2 мин).')) return;
        try {
            await api.post('/financier/onec/credentials', { login: u, password: p });
            if (this.dom.onecPass) this.dom.onecPass.value = '';
            toast('Учётка 1С сохранена (зашифровано). Релей заберёт на опросе (~2 мин).', 'info');
            this.loadOnecStatus();
        } catch (e) { toast('Ошибка: ' + (e?.message || e), 'error'); }
    },

    async runOnec(probe) {
        const msg = probe
            ? 'Запустить РАЗВЕДКУ 1С? Релей залогинится и снимет скрины/DOM (без сбора долгов) в /opt/gisgmp-relay/onec_probe — пришли их мне, я донастрою селекторы.'
            : 'Запустить сбор ОСВ из 1С сейчас? Релей сформирует ведомости за период с начала года и зальёт черновиком (~2 мин до старта).';
        if (!await showConfirm(msg)) return;
        try {
            await api.post(`/financier/onec/run-now?probe=${probe ? 'true' : 'false'}`, {});
            toast(probe ? 'Разведка поставлена — релей выполнит на ближайшем опросе (~2 мин).'
                        : 'Сбор поставлен — релей выполнит на ближайшем опросе (~2 мин).', 'info');
            this.loadOnecStatus();
        } catch (e) { toast('Ошибка: ' + (e?.message || e), 'error'); }
    },

    // «Что нашёл релей» — последний авто-сбор 1С: ФИО + долги/переплаты 209/205.
    async loadOnecFound() {
        const box = this.dom.onecFoundBody;
        if (!box) return;
        if (box.style.display === 'block') { box.style.display = 'none'; return; }
        box.style.display = 'block';
        box.innerHTML = '<div style="padding:10px; color:var(--text-secondary);">Загрузка…</div>';
        try {
            const d = await api.get('/financier/onec/last-found');
            const it = d.items || [];
            const t = d.totals || {};
            if (!it.length) {
                box.innerHTML = '<div style="padding:10px; color:#9ca3af;">Релей ещё ничего не собрал из 1С (нет авто-импортов). Запусти «Сбор сейчас».</div>';
                return;
            }
            const m209 = d.meta && d.meta['209'];
            const m205 = d.meta && d.meta['205'];
            const fmt = (v) => Number(v || 0).toLocaleString('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
            const esc = (s) => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
            const rows = it.map(x => `<tr style="${x.matched ? '' : 'background:#fff7ed;'}">
                <td>${esc(x.fio)}</td>
                <td>${x.matched ? '<span style="color:#166534;">в базе</span>' : '<span style="color:#b45309;">не найден</span>'}</td>
                <td style="text-align:right;">${x.debt_209 ? fmt(x.debt_209) : '—'}</td>
                <td style="text-align:right;">${x.debt_205 ? fmt(x.debt_205) : '—'}</td>
                <td style="text-align:right; color:#16a34a;">${(x.over_209 + x.over_205) ? fmt(x.over_209 + x.over_205) : '—'}</td>
            </tr>`).join('');
            box.innerHTML = `
                <div style="font-size:12px; color:var(--text-secondary); margin-bottom:6px;">
                    Источник: ${esc((m209 && m209.file) || (m205 && m205.file) || '—')}
                    ${m209 && m209.at ? '· ' + fmtDateTime(m209.at) : ''} ·
                    людей <b>${t.people}</b> (в базе ${t.matched}, не найдено ${t.not_found}) ·
                    долг 209 <b>${fmt(t.debt_209)}</b> · долг 205 <b>${fmt(t.debt_205)}</b> ·
                    переплат <b>${fmt(t.over_209 + t.over_205)}</b>
                </div>
                <div style="max-height:420px; overflow-y:auto;">
                <table style="width:100%; border-collapse:collapse; font-size:13px;">
                    <thead><tr style="position:sticky; top:0; background:var(--bg-card,#fff);">
                        <th style="text-align:left; padding:5px 8px; border-bottom:1px solid var(--border-color,#e5e7eb);">ФИО</th>
                        <th style="text-align:left; padding:5px 8px; border-bottom:1px solid var(--border-color,#e5e7eb);">Сопоставление</th>
                        <th style="text-align:right; padding:5px 8px; border-bottom:1px solid var(--border-color,#e5e7eb);">Долг 209</th>
                        <th style="text-align:right; padding:5px 8px; border-bottom:1px solid var(--border-color,#e5e7eb);">Долг 205</th>
                        <th style="text-align:right; padding:5px 8px; border-bottom:1px solid var(--border-color,#e5e7eb);">Переплата</th>
                    </tr></thead>
                    <tbody>${rows}</tbody>
                </table></div>`;
        } catch (e) {
            box.innerHTML = `<div style="padding:10px; color:#b91c1c;">Ошибка: ${(e?.message || e)}</div>`;
        }
    },

    // Раздельное окно отладки: что нашёл ГИС ГМП (в долги пока не пишется).
    // Поиск по фамилии — мгновенный, по сырым начислениям из последнего синка.
    async openGisgmpFindings() {
        const body = this.dom.gisgmpFindingsBody;
        if (!body) return;
        if (body.style.display !== 'none') { body.style.display = 'none'; return; }
        body.style.display = 'block';
        body.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Загрузка…';
        try {
            const f = await api.get('/financier/gisgmp/findings');
            if (!f || f.empty) {
                body.innerHTML = '<span style="color:var(--text-secondary)">Пока ничего не найдено — релей ещё не отработал в раздельном режиме. Нажми «Запустить сейчас».</span>';
                return;
            }
            this._gisgmpFindings = f;
            const d = f.diag || {};
            body.innerHTML =
                `<div style="font-size:12px; color:var(--text-secondary); margin-bottom:8px;">`
                + `Синхронизация: <b>${f.synced_at ? new Date(f.synced_at).toLocaleString('ru-RU') : '—'}</b> · `
                + `начислений: ${f.total_charges ?? 0} · в долг: ${d.counted ?? '—'} · оплачено: ${d.paid ?? '—'} · аннулировано: ${d.annulled ?? '—'} · `
                + `жильцов с долгом: <b>${f.residents ?? 0}</b> (сопоставлено ${f.matched ?? 0}, не найдено ${f.not_found ?? 0})</div>`
                + `<input type="text" id="gisgmpSearch" placeholder="Фамилия — разбор по начислениям…" style="width:300px; padding:6px 8px; margin-bottom:10px;">`
                + `<div id="gisgmpFindingsResult"></div>`;
            document.getElementById('gisgmpSearch')?.addEventListener('input', () => this.renderGisgmpFindings());
            this.renderGisgmpFindings();
        } catch (e) {
            body.innerHTML = 'Ошибка загрузки находок: ' + esc(e?.message || String(e));
        }
    },

    renderGisgmpFindings() {
        const f = this._gisgmpFindings;
        const res = document.getElementById('gisgmpFindingsResult');
        if (!f || !res) return;
        const q = (document.getElementById('gisgmpSearch')?.value || '').trim().toLowerCase();
        const fmt = (v) => (Number(v) || 0).toLocaleString('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        const acct = (p) => {
            p = (p || '').toLowerCase();
            if (p.includes('наем') || p.includes('найм') || p.includes('наём')) return '205';
            if (p.includes('комус') || p.includes('коммунал')) return '209';
            return '?';
        };

        if (!q) {
            // Без поиска — сводка по жильцам (Σ долга), отсортировано.
            const rows = (f.summary || []).map(r => {
                const m = r.matched_username
                    ? `<span style="color:#047857">${esc(r.matched_username)}</span>`
                    : '<span style="color:#b91c1c">не найден</span>';
                return `<tr><td>${esc(r.fio)}</td><td>${m}</td>`
                    + `<td class="text-right">${fmt(r.debt_209)}</td><td class="text-right">${fmt(r.debt_205)}</td>`
                    + `<td class="text-right"><b>${fmt(r.total)}</b></td></tr>`;
            }).join('');
            res.innerHTML = `<div class="table-responsive" style="max-height:65vh;overflow:auto;"><table class="sticky-header-table" style="font-size:13px;">`
                + `<thead><tr><th>ФИО (реестр)</th><th>Жилец в базе</th>`
                + `<th class="text-right">Долг 209</th><th class="text-right">Долг 205</th><th class="text-right">Σ долг</th></tr></thead>`
                + `<tbody>${rows || '<tr><td colspan="5" class="text-center">пусто</td></tr>'}</tbody></table></div>`;
            return;
        }

        // Поиск по фамилии — разбор по начислениям (аннулированные не считаются).
        const list = (f.charges || []).filter(c => (c.payer_name || '').toLowerCase().includes(q));
        let t205 = 0, t209 = 0;
        const rows = list.map(c => {
            const a = acct(c.purpose);
            const unpaid = (c.ack_status || '').toLowerCase().includes('не сквитировано');
            const annul = (c.change_status || '').trim().toLowerCase() === 'аннулирование';
            const counted = unpaid && !annul && (a === '205' || a === '209');
            if (counted) { if (a === '205') t205 += Number(c.amount) || 0; else t209 += Number(c.amount) || 0; }
            const flag = annul ? '<span style="color:#9ca3af">аннулир.</span>'
                : (counted ? '<span style="color:#b91c1c">В ДОЛГ</span>' : '<span style="color:#6b7280">оплачено</span>');
            return `<tr style="${annul ? 'opacity:.5' : ''}"><td>${esc(c.payer_name || '')}</td><td>сч ${a}</td>`
                + `<td class="text-right">${fmt(c.amount)}</td><td>${esc(c.bill_date || '')}</td>`
                + `<td>${esc(c.ack_status || '')}</td><td>${esc(c.change_status || '')}</td>`
                + `<td>${flag}</td><td>${esc((c.purpose || '').slice(0, 42))}</td></tr>`;
        }).join('');
        res.innerHTML = `<div style="font-size:13px; margin-bottom:6px;">Строк: <b>${list.length}</b> · `
            + `ДОЛГ 205 (наём): <b>${fmt(t205)}</b> · ДОЛГ 209 (комуслуги): <b>${fmt(t209)}</b> `
            + `<span style="color:#9ca3af">(аннулированные не считаются)</span></div>`
            + `<div class="table-responsive" style="max-height:65vh;overflow:auto;"><table class="sticky-header-table" style="font-size:12px;">`
            + `<thead><tr><th>Плательщик</th><th>Счёт</th><th class="text-right">Сумма</th><th>Дата</th>`
            + `<th>Квитирование</th><th>Изменение</th><th>Статус</th><th>Назначение</th></tr></thead>`
            + `<tbody>${rows || '<tr><td colspan="8" class="text-center">по этой фамилии ничего</td></tr>'}</tbody></table></div>`;
    },

    // Дотянуть расхождения: ставит проблемных (ГИС занижен) в очередь точечного добора.
    async recheckGisgmp() {
        const ok = await showConfirm('Поставить в очередь дотягивания жильцов, где ГИС занижен («1С > ГИС» и «нет в ГИС»)? Релей точечно доберёт их полную историю (36 мес) за пару минут. Сошедшихся и «ГИС > 1С» не трогаем.');
        if (!ok) return;
        try {
            const r = await api.post('/financier/gisgmp/recheck-build', {});
            toast(`В очередь: ${r.queued} фамилий. Релей дотянет в течение ~2 мин — потом обнови «Сверку с 1С».`, 'info');
        } catch (e) {
            toast('Ошибка: ' + (e?.message || e), 'error');
        }
    },

    // Массовая актуализация: демон дёргает «Актуализировать из ГИС ГМП» по каждому
    // неоплаченному счёту жильцов с расхождением. Долго → фон + прогресс в статусе.
    async actualizeGisgmp() {
        if (!await showConfirm('Запустить массовую актуализацию? В очередь попадут неоплаченные счета ТОЛЬКО тех жильцов, у кого ГИС > 1С («ошибка ГИС ГМП» — реестр завышает долг). Демон в фоне по каждому счёту дёрнет «Актуализировать из ГИС ГМП». Это ДОЛГО (сервер реестра медленный) — можно закрыть страницу, прогресс в «Статусе». Каждый прогон пишется в «Историю актуализаций» (до/после).')) return;
        try {
            const r = await api.post('/financier/gisgmp/actualize-build', {});
            if (!r.queued) { toast('Сейчас нет жильцов с ГИС > 1С — актуализировать нечего. Сначала обнови «Сверку с 1С».', 'info'); return; }
            toast(`В очередь: ${r.queued} счетов по ${r.residents} жильцам (только ГИС > 1С). Прогресс — в статусе, итог «до/после» — в «Истории актуализаций».`, 'info');
            this.loadGisgmpStatus();
        } catch (e) { toast('Ошибка: ' + (e?.message || e), 'error'); }
    },

    // Актуализация ЗА ВСЕХ: в очередь попадают ВСЕ несквитированные начисления
    // всех людей (не только ГИС>1С). Демон идёт по одному счёту с паузой — «по
    // чуть-чуть». Эквивалент «нажать актуализацию за каждого» сразу для всех.
    async actualizeAllGisgmp() {
        if (!await showConfirm('Актуализировать ВСЕХ? В очередь попадут ВСЕ несквитированные начисления всех людей из ГИС ГМП (не только расхождения). Демон в фоне дёрнет «Актуализировать из ГИС ГМП» по каждому счёту — по одному с паузой, чтобы не перегружать реестр. Это ДОЛГО — можно закрыть страницу, прогресс в «Статусе», итог «до/после» — в «Истории актуализаций».')) return;
        try {
            const r = await api.post('/financier/gisgmp/actualize-all', {});
            if (!r.queued) { toast(r.reason || 'Нечего актуализировать (пустой кэш ГИС ГМП).', 'info'); return; }
            toast(`В очередь: ${r.queued} счетов по ${r.residents} людям (ВСЕ). Демон обработает по одному с паузой. Прогресс — в «Статусе».`, 'info');
            this.loadGisgmpStatus();
        } catch (e) { toast('Ошибка: ' + (e?.message || e), 'error'); }
    },

    // Выпадающие меню в карточке ГИС ГМП (компактнее, чем 8 кнопок в ряд).
    _initGisDropdowns() {
        const hideAll = () => document.querySelectorAll('.gis-dd-menu').forEach(m => { m.style.display = 'none'; });
        document.querySelectorAll('.gis-dd-trigger').forEach(t => {
            if (t._ddWired) return;
            t._ddWired = true;
            t.addEventListener('click', (e) => {
                e.stopPropagation();
                const menu = t.parentElement.querySelector('.gis-dd-menu');
                if (!menu) return;
                const wasOpen = menu.style.display === 'block';
                hideAll();
                menu.style.display = wasOpen ? 'none' : 'block';
            });
        });
        document.querySelectorAll('.gis-dd-item').forEach(it => {
            if (it._ddWired) return;
            it._ddWired = true;
            it.addEventListener('click', () => setTimeout(hideAll, 0));
        });
        if (!document._gisDdDocClose) {
            document._gisDdDocClose = true;
            document.addEventListener('click', hideAll);
        }
    },

    async purgeGisgmp() {
        if (!await showConfirm('Очистить ВСЕ рабочие данные ГИС ГМП (кэш начислений, находки, курсор, очередь актуализации)? Долги жильцов это НЕ трогает — они из 1С. После очистки запусти сбор заново («Актуализация → Запустить сбор»).', { title: 'Очистить ГИС ГМП', confirmText: 'Очистить' })) return;
        try {
            const r = await api.post('/financier/gisgmp/purge', {});
            toast(`Данные ГИС ГМП очищены (ключей: ${r.cleared}). Запусти сбор заново.`, 'success');
            this.loadGisgmpStatus();
        } catch (e) { toast('Ошибка: ' + (e?.message || e), 'error'); }
    },

    // 3-сторонняя сверка ФИО: 1С ↔ ГИС ↔ база («где кого нету»).
    /** Завести личные кабинеты (только ФИО) для всех, кто есть в 1С/ГИС, но
     *  нет в базе. Двухстадийно: предпросмотр (dry-run) → подтверждение → apply.
     *  Мусор (организации/цифры/одно слово) и дубли отсеиваются на бэке. */
    async createMissingResidents() {
        let p;
        try {
            p = await api.post('/financier/gisgmp/create-missing-residents?dry_run=true');
        } catch (e) { toast('Ошибка предпросмотра: ' + (e?.message || e), 'error'); return; }
        const n = p.to_create_count || 0;
        const skNot = (p.skip_not_fio || []).length;
        const skDup = (p.skip_in_db || []).length + (p.skip_similar || []).length;
        if (!n) {
            toast(`Новых ФИО для создания нет. Отсеяно: не-ФИО/организаций ${skNot}, дублей ${skDup}.`, 'info');
            return;
        }
        const sample = (p.to_create || []).slice(0, 20).join('\n  • ');
        const more = n > 20 ? `\n  …и ещё ${n - 20}` : '';
        const ok = await showConfirm(
            `Создать ${n} жильцов — ТОЛЬКО ФИО (без комнаты, адреса и прочего)?\n\n` +
            `Будут заведены:\n  • ${sample}${more}\n\n` +
            `Пропущено: не-ФИО/организаций — ${skNot}, дублей с базой — ${skDup}.\n\n` +
            `Пароль, комнату и данные заполнишь потом сам.`,
            { title: 'Создать отсутствующих в базе', confirmText: `Создать ${n}` }
        );
        if (!ok) return;
        try {
            const r = await api.post('/financier/gisgmp/create-missing-residents?dry_run=false');
            toast(`Создано жильцов: ${r.created_count}. Пропущено: не-ФИО ${r.skip_not_fio}, дублей ${r.skip_in_db + r.skip_similar}.`, 'success');
            this.openReconcileFio();  // перерисовать союз — сирот станет меньше
        } catch (e) { toast('Ошибка создания: ' + (e?.message || e), 'error'); }
    },

    async openReconcileFio() {
        const body = this.dom.gisgmpReconcileFioBody;
        if (!body) return;
        if (body.style.display !== 'none') { body.style.display = 'none'; return; }
        body.style.display = 'block';
        body.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Сверяю 1С / ГИС / базу…';
        try {
            this._reconFio = await api.get('/financier/gisgmp/reconcile-fio');
            this._reconFioFilter = 'problem';
            this._reconFioQuery = '';
            this.renderReconcileFio();
        } catch (e) {
            body.innerHTML = 'Ошибка: ' + esc(e?.message || String(e));
        }
    },

    /** Принудительно перезапросить «Сверку ФИО» с сервера (без тоггла видимости).
     *  База проверяется живьём — добавленные/заселённые жильцы тут отразятся. */
    async refreshReconcileFio() {
        const body = this.dom.gisgmpReconcileFioBody;
        if (!body) return;
        body.style.display = 'block';
        const f = this._reconFioFilter, q = this._reconFioQuery;
        try {
            this._reconFio = await api.get('/financier/gisgmp/reconcile-fio');
            this._reconFioFilter = f || 'problem';
            this._reconFioQuery = q || '';
            this.renderReconcileFio();
            toast('Сверка обновлена', 'info');
        } catch (e) { body.innerHTML = 'Ошибка: ' + esc(e?.message || String(e)); }
    },

    renderReconcileFio() {
        const body = this.dom.gisgmpReconcileFioBody;
        const d = this._reconFio;
        if (!body || !d) return;
        const s = d.summary || {};
        const fmt = (v) => (Number(v) || 0).toLocaleString('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        const tile = (label, val, color) => `<div style="flex:1; min-width:92px; background:var(--bg-page); border-radius:8px; padding:8px 10px; text-align:center;"><div style="font-size:18px; font-weight:700; color:${color};">${val}</div><div style="font-size:10px; color:var(--text-secondary);">${label}</div></div>`;
        const flt = this._reconFioFilter || 'problem';
        const q = (this._reconFioQuery || '').trim().toLowerCase();
        let list = d.rows || [];
        if (flt === 'no_1c') list = list.filter(r => !r.in_1c);
        else if (flt === 'no_gis') list = list.filter(r => !r.in_gis);
        else if (flt === 'no_db') list = list.filter(r => !r.in_db);
        else if (flt === 'over') list = list.filter(r => ((r.o209_1c || 0) + (r.o205_1c || 0)) > 0.005);
        else if (flt === 'problem') list = list.filter(r => !(r.in_1c && r.in_gis && r.in_db));
        if (q) list = list.filter(r => (r.fio || '').toLowerCase().includes(q));
        const mark = (on) => on ? '<span style="color:#047857; font-weight:700;">✓</span>' : '<span style="color:#b91c1c; font-weight:700;">✗</span>';
        const trs = list.slice(0, 1500).map(r => {
            const t1c = (r.d209_1c || 0) + (r.d205_1c || 0);
            const tgis = (r.d209_gis || 0) + (r.d205_gis || 0);
            const o1c = (r.o209_1c || 0) + (r.o205_1c || 0);
            const d1c = (r.d209_1c || r.d205_1c) ? fmt(t1c) : '<span style="color:#d1d5db;">—</span>';
            const dgis = (r.d209_gis || r.d205_gis) ? fmt(tgis) : '<span style="color:#d1d5db;">—</span>';
            const op1c = o1c > 0.005 ? `<span style="color:#047857; font-weight:600;">${fmt(o1c)}</span>` : '<span style="color:#d1d5db;">—</span>';
            // Δ = ГИС − 1С (то же сравнение, что в «Сверке с 1С», но тут по ФИО,
            // без гейта по базе). Красный = ГИС больше, синий = 1С больше.
            const dv = tgis - t1c;
            const dCol = Math.abs(dv) < 0.005 ? '<span style="color:#9ca3af;">0</span>'
                : `<span style="color:${dv > 0 ? '#b91c1c' : '#0ea5e9'}; font-weight:700;">${dv > 0 ? '+' : '−'}${fmt(Math.abs(dv))}</span>`;
            const bad = !(r.in_1c && r.in_gis && r.in_db);
            // «Привязать» — опционально (связать сироту с жильцом базы для выгрузки).
            const action = (!r.in_db && (r.in_1c || r.in_gis))
                ? `<button class="action-btn secondary-btn" style="font-size:10px; padding:2px 7px;" data-link-fio="${esc(r.fio)}"><i class="fa-solid fa-link"></i> Привязать</button>`
                : '';
            return `<tr style="${bad ? 'background:rgba(254,226,226,.35);' : ''}"><td><span class="recon-fio-link" data-person-fio="${esc(r.fio)}" style="cursor:pointer; color:#2563eb; text-decoration:underline dotted;" title="Открыть начисления ГИС по этому ФИО">${esc(r.fio)}</span></td>`
                + `<td class="text-center">${mark(r.in_1c)}</td><td class="text-right">${d1c}</td><td class="text-right">${op1c}</td>`
                + `<td class="text-center">${mark(r.in_gis)}</td><td class="text-right">${dgis}</td>`
                + `<td class="text-right">${dCol}</td>`
                + `<td class="text-center">${mark(r.in_db)}</td><td class="text-center">${action}</td></tr>`;
        }).join('');
        const fbtn = (key, label) => `<button class="action-btn ${flt === key ? 'primary-btn' : 'secondary-btn'}" style="font-size:11px; padding:3px 10px;" data-rf="${key}">${label}</button>`;

        body.innerHTML =
            `<div style="display:flex; gap:8px; flex-wrap:wrap; margin-bottom:10px;">
                ${tile('Всего ФИО', s.total ?? 0, '#2563eb')}
                ${tile('Везде (1С+ГИС+база)', s.all_three ?? 0, '#047857')}
                ${tile('Нет в 1С', s.not_in_1c ?? 0, '#b91c1c')}
                ${tile('Нет в ГИС', s.not_in_gis ?? 0, '#b91c1c')}
                ${tile('Нет в базе', s.not_in_db ?? 0, '#d97706')}
                ${tile('С переплатой 1С', s.with_overpay ?? 0, '#0ea5e9')}
            </div>
            <div style="display:flex; gap:6px; flex-wrap:wrap; align-items:center; margin-bottom:8px;">
                ${fbtn('problem', 'Проблемные')}${fbtn('no_1c', 'Нет в 1С')}${fbtn('no_gis', 'Нет в ГИС')}${fbtn('no_db', 'Нет в базе')}${fbtn('over', 'С переплатой')}${fbtn('all', 'Все')}
                <button class="action-btn secondary-btn" data-recon-refresh style="font-size:11px; padding:3px 10px;"><i class="fa-solid fa-rotate-right"></i> Обновить</button>
                <input type="text" id="reconFioSearch" placeholder="Поиск по ФИО…" value="${esc(this._reconFioQuery || '')}" style="margin-left:auto; padding:4px 8px; font-size:12px; min-width:200px;">
            </div>
            <div style="font-size:11px; color:var(--text-secondary); margin-bottom:6px;">Показано: <b>${list.length}</b>. База — живьём; ГИС от: <b>${d.gis_synced_at ? new Date(d.gis_synced_at).toLocaleString('ru-RU') : '—'}</b>; 1С — последний импорт. Союз по ТОЧНОМУ ФИО.</div>
            <div class="table-responsive" style="max-height:55vh; overflow:auto;"><table class="sticky-header-table" style="font-size:12px;">
                <thead><tr><th>ФИО</th><th class="text-center">1С</th><th class="text-right">Долг 1С</th><th class="text-right">Перепл 1С</th><th class="text-center">ГИС</th><th class="text-right">Долг ГИС</th><th class="text-right">Δ ГИС−1С</th><th class="text-center">База</th><th class="text-center">Действие</th></tr></thead>
                <tbody>${trs || '<tr><td colspan="9" class="text-center" style="color:#9ca3af;">нет строк под фильтр</td></tr>'}</tbody>
            </table></div>`;
        body.querySelectorAll('[data-rf]').forEach(b => b.addEventListener('click', () => { this._reconFioFilter = b.getAttribute('data-rf'); this.renderReconcileFio(); }));
        body.querySelector('[data-recon-refresh]')?.addEventListener('click', () => this.refreshReconcileFio());
        body.querySelectorAll('[data-link-fio]').forEach(b => b.addEventListener('click', () => this.linkFioPrompt(b.getAttribute('data-link-fio'))));
        body.querySelectorAll('[data-person-fio]').forEach(b => b.addEventListener('click', () => this.openPersonCharges(b.getAttribute('data-person-fio'))));
        const si = document.getElementById('reconFioSearch');
        if (si) {
            si.addEventListener('input', () => { this._reconFioQuery = si.value; this.renderReconcileFio(); });
            if (this._reconFioQuery) { si.focus(); si.setSelectionRange(si.value.length, si.value.length); }
        }
    },

    // Проваливание в ФИО: модалка со всеми начислениями ГИС человека + действия.
    async openPersonCharges(fio) {
        let data;
        try {
            data = await api.get(`/financier/gisgmp/person-charges?fio=${encodeURIComponent(fio)}`);
        } catch (e) { toast('Ошибка загрузки начислений: ' + (e?.message || e), 'error'); return; }
        const charges = data.charges || [];
        const s = data.summary || {};
        const fmt = (v) => (Number(v) || 0).toLocaleString('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        const ov = document.createElement('div');
        ov.className = 'modal-overlay open';
        ov.style.zIndex = '9999';
        const rowsHtml = charges.length ? charges.map(c => {
            const st = c.annulled ? '<span style="color:#6b7280;">аннулировано</span>'
                : (c.unpaid ? '<span style="color:#b91c1c; font-weight:600;">не сквитировано</span>' : '<span style="color:#047857;">сквитировано</span>');
            return `<tr style="${c.unpaid && !c.annulled ? 'background:rgba(254,226,226,.3);' : ''}">`
                + `<td style="font-family:monospace; font-size:11px;">${esc(c.uin || '—')}</td>`
                + `<td class="text-center">${esc(c.account || '?')}</td>`
                + `<td class="text-right">${fmt(c.amount)}</td>`
                + `<td class="text-center" style="font-size:11px;">${esc(c.bill_date || '—')}</td>`
                + `<td>${st}</td></tr>`;
        }).join('') : '<tr><td colspan="5" class="text-center" style="color:#9ca3af;">нет начислений в кэше ГИС по этому ФИО</td></tr>';
        ov.innerHTML = `<div class="modal-window" style="width:740px; max-width:96vw;">
            <div class="modal-header"><h3 style="font-size:15px;">Начисления ГИС ГМП — ${esc(fio)}</h3><button class="close-btn" data-close>&times;</button></div>
            <div class="modal-form" style="padding:14px 16px; max-height:72vh; overflow:auto;">
                <div style="font-size:12px; color:var(--text-secondary); margin-bottom:8px;">Всего: <b>${s.total || 0}</b> · не сквитировано: <b style="color:#b91c1c;">${s.revocable || 0}</b> (${fmt(s.sum_revocable)} ₽) · аннулировано: <b>${s.annulled || 0}</b></div>
                <div style="display:flex; gap:8px; margin-bottom:10px; flex-wrap:wrap; align-items:center;">
                    <button class="action-btn primary-btn" style="font-size:13px;" data-act-person ${s.revocable ? '' : 'disabled'}><i class="fa-solid fa-rotate"></i> Актуализировать (${s.revocable || 0})</button>
                    <button class="action-btn" style="font-size:13px; background:#b91c1c; color:#fff; border-color:#b91c1c;" data-annul-person ${s.revocable ? '' : 'disabled'} title="Аннулировать в ГИС все несквитированные начисления (только админ, обратимо де-аннулированием)"><i class="fa-solid fa-ban"></i> Аннулировать несквитированное (${s.revocable || 0})</button>
                </div>
                <div class="table-responsive" style="max-height:50vh; overflow:auto;"><table class="sticky-header-table" style="font-size:12px;">
                    <thead><tr><th>УИН</th><th class="text-center">Счёт</th><th class="text-right">Сумма</th><th class="text-center">Дата</th><th>Статус</th></tr></thead>
                    <tbody>${rowsHtml}</tbody></table></div>
            </div></div>`;
        document.body.appendChild(ov);
        const close = () => ov.remove();
        ov.querySelector('[data-close]')?.addEventListener('click', close);
        ov.addEventListener('click', (e) => { if (e.target === ov) close(); });
        ov.querySelector('[data-act-person]')?.addEventListener('click', async () => { close(); await this.actualizePerson(fio); });
        ov.querySelector('[data-annul-person]')?.addEventListener('click', () => this.annulPerson(fio, s));
    },

    // Аннулировать ВСЕ несквитированные начисления человека (только админ, слово-подтв).
    async annulPerson(fio, s) {
        const fmt = (v) => (Number(v) || 0).toLocaleString('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        const ov = document.createElement('div');
        ov.className = 'modal-overlay open';
        ov.style.zIndex = '10000';
        ov.innerHTML = `<div class="modal-window" style="width:500px; max-width:94vw;">
            <div class="modal-header" style="background:#fef2f2;"><h3 style="font-size:15px; color:#b91c1c;">⚠ Аннулировать начисления</h3><button class="close-btn" data-close>&times;</button></div>
            <div class="modal-form" style="padding:16px;">
                <p style="font-size:13px; margin:0 0 8px;">Аннулировать в ГИС ГМП <b>ВСЕ ${s.revocable || 0}</b> несквитированных начислений на сумму <b>${fmt(s.sum_revocable)} ₽</b> у <b>${esc(fio)}</b>.</p>
                <p style="font-size:12px; color:#92400e; background:#fffbeb; padding:8px 10px; border-radius:6px; margin:0 0 12px;">Релей дёрнет «Аннулировать» по каждому счёту. Уже аннулированные/сквитированные не трогаются. В ГИС это <b>обратимо</b> («Де-аннулировать»). Действие только для админа.</p>
                <label style="font-size:12px; display:block; margin-bottom:4px;">Впишите слово <b style="color:#b91c1c;">АННУЛИРОВАТЬ</b> для подтверждения:</label>
                <input type="text" id="annulConfirmInput" placeholder="АННУЛИРОВАТЬ" autocomplete="off" style="width:100%; padding:7px 10px; font-size:13px; margin-bottom:14px; box-sizing:border-box;">
                <div style="display:flex; gap:8px; justify-content:flex-end;">
                    <button class="action-btn secondary-btn" data-close>Отмена</button>
                    <button class="action-btn" style="background:#b91c1c; color:#fff; border-color:#b91c1c;" data-annul-go><i class="fa-solid fa-ban"></i> Аннулировать ${s.revocable || 0}</button>
                </div>
            </div></div>`;
        document.body.appendChild(ov);
        const close = () => ov.remove();
        ov.querySelectorAll('[data-close]').forEach(b => b.addEventListener('click', close));
        ov.addEventListener('click', (e) => { if (e.target === ov) close(); });
        const inp = document.getElementById('annulConfirmInput');
        inp?.focus();
        ov.querySelector('[data-annul-go]')?.addEventListener('click', async () => {
            const confirm = (inp?.value || '').trim();
            if (confirm.toUpperCase() !== 'АННУЛИРОВАТЬ') { toast('Впишите слово АННУЛИРОВАТЬ', 'warning'); inp?.focus(); return; }
            try {
                const r = await api.post('/financier/gisgmp/annul-person', { fio, confirm });
                if (!r.queued) { toast(r.reason || 'Нечего аннулировать', 'info'); close(); return; }
                toast(`Поставлено в аннулирование: ${r.queued} счетов (${fmt(r.sum)} ₽) по «${fio}». Релей аннулирует за ~1-2 мин.`, 'success');
                close();
                this.loadGisgmpStatus?.();
            } catch (e) {
                const m = e?.message || String(e);
                toast(m.includes('403') ? 'Аннулирование — только администратор' : ('Ошибка: ' + m), 'error');
            }
        });
    },

    async actualizePerson(fio) {
        if (!await showConfirm(`Поставить актуализацию всех несквитированных начислений «${fio}»? Релей дёрнет ГИС по каждому; результат — отложенной перепроверкой (как у массовой).`)) return;
        try {
            const r = await api.post('/financier/gisgmp/actualize-person', { fio });
            if (!r.queued) { toast(r.reason || 'Нечего актуализировать', 'info'); return; }
            toast(`Поставлено в актуализацию: ${r.queued} начислений по «${fio}». Прогресс — в статусе, итог — в «Истории актуализаций».`, 'success');
            this.loadGisgmpStatus?.();
        } catch (e) { toast('Ошибка актуализации: ' + (e?.message || e), 'error'); }
    },

    // Привязать «сироту» 1С/ГИС к жильцу базы: кандидаты по фамилии → выбор → алиас + переименование.
    async linkFioPrompt(fio) {
        let cands = [];
        try {
            const r = await api.get(`/financier/gisgmp/link-candidates?fio=${encodeURIComponent(fio)}`);
            cands = r.candidates || [];
        } catch (e) { toast('Ошибка загрузки кандидатов: ' + (e?.message || e), 'error'); return; }
        const ov = document.createElement('div');
        ov.className = 'modal-overlay open';
        ov.style.zIndex = '9999';
        const candHtml = cands.length
            ? cands.map(c => `<button class="action-btn secondary-btn" style="display:block; width:100%; text-align:left; margin-bottom:6px; font-size:13px;" data-uid="${c.id}">${esc(c.username)} <span style="color:var(--text-secondary); font-size:11px;">— ${esc(c.address)}</span></button>`).join('')
            : '<div style="color:var(--text-secondary); padding:8px;">Нет жильцов с такой фамилией в базе. Возможно, жильца нет — заведи его во вкладке «Жильцы», потом привяжи.</div>';
        ov.innerHTML = `<div class="modal-window" style="width:540px; max-width:94vw;">
            <div class="modal-header"><h3 style="font-size:15px;">Привязать «${esc(fio)}» к жильцу</h3><button class="close-btn" data-close>&times;</button></div>
            <div class="modal-form" style="padding:14px 16px; max-height:60vh; overflow:auto;">
                <p style="font-size:12px; color:var(--text-secondary); margin:0 0 10px;">Кандидаты по фамилии. Выбери жильца — создастся алиас (долг привяжется при выгрузке), а имя в базе обновится на «${esc(fio)}» (приоритет 1С/ГИС).</p>
                ${candHtml}
            </div>
        </div>`;
        document.body.appendChild(ov);
        const close = () => ov.remove();
        ov.querySelectorAll('[data-close]').forEach(b => b.addEventListener('click', close));
        ov.addEventListener('click', (e) => { if (e.target === ov) close(); });
        ov.querySelectorAll('[data-uid]').forEach(b => b.addEventListener('click', async () => {
            const uid = Number(b.getAttribute('data-uid'));
            try {
                const res = await api.post('/financier/gisgmp/link-fio', { fio, user_id: uid, rename: true });
                toast(res.warning || `Привязано → ${res.username}`, res.warning ? 'warning' : 'success');
                close();
                // Обновляем открытые виды: союз «Сверка ФИО» и «Сверка с 1С».
                if (this._reconFio) { try { this._reconFio = await api.get('/financier/gisgmp/reconcile-fio'); this.renderReconcileFio(); } catch (er) { /* норм */ } }
                this.reloadReconcile();
            } catch (e) { toast('Ошибка привязки: ' + (e?.message || e), 'error'); }
        }));
    },

    // История/аудит массовых актуализаций: что актуализировали и что изменилось (до→после).
    async openActualizeLog() {
        const body = this.dom.gisgmpActualizeLogBody;
        if (!body) return;
        if (body.style.display !== 'none') { body.style.display = 'none'; return; }
        body.style.display = 'block';
        body.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Загрузка истории…';
        try {
            const r = await api.get('/financier/gisgmp/actualize-log');
            this._actualizeRuns = r.runs || [];
            this.renderActualizeLog();
        } catch (e) {
            body.innerHTML = 'Ошибка загрузки истории: ' + esc(e?.message || String(e));
        }
    },

    renderActualizeLog() {
        const body = this.dom.gisgmpActualizeLogBody;
        if (!body) return;
        const runs = this._actualizeRuns || [];
        const fmt = (v) => (Number(v) || 0).toLocaleString('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        const dt = (s) => s ? new Date(s).toLocaleString('ru-RU') : '—';
        const STAT = {
            running: ['⏳ отправка запросов в ГИС…', '#2563eb'],
            checking: ['🔄 цикл: проверяю «Сквитировано» (~каждые 2 мин)…', '#2563eb'],
            sent: ['📨 отправлено — ждём перепроверку', '#d97706'],
            rechecking: ['🔄 перепроверяем результат в ГИС…', '#2563eb'],
            actualized: ['📨 отправлено — ждём перепроверку', '#d97706'],
            done: ['✅ готово (есть «после»)', '#047857'],
        };
        const LRES = {
            all_paid: ['✅ всё сквитировано (оплачено)', '#047857'],
            unpaid_left: ['⚠ часть не оплачена (после 2 попыток — реальный долг)', '#b91c1c'],
            timeout: ['⏱ финал по таймауту (90 мин)', '#d97706'],
        };
        const RES = {
            annulled: ['аннулировано', '#047857'], reduced: ['уменьшилось', '#0ea5e9'],
            unchanged: ['без изменений', '#6b7280'], increased: ['выросло', '#b91c1c'], unknown: ['—', '#6b7280'],
        };
        if (!runs.length) {
            body.innerHTML = '<span style="color:var(--text-secondary)">История пуста — массовая актуализация ещё не запускалась.</span>';
            return;
        }
        const hasPending = runs.length > 0;  // «Проверить результат» доступна всегда — ГИС мог дообработать позже
        const head = `<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px; gap:8px; flex-wrap:wrap;">`
            + `<span style="font-size:12px; color:var(--text-secondary)">Прогонов в истории: <b>${runs.length}</b> (хранится до 50, старьё чистится)</span>`
            + `<span style="display:flex; gap:6px;">`
            + (hasPending ? `<button class="action-btn primary-btn" style="font-size:12px;" data-act-recheck title="Переопросить ГИС по ФИО последнего прогона и снять снимок «после». ГИС обрабатывает актуализацию асинхронно — жми, когда прошло время (или дождись авто через ~2ч)."><i class="fa-solid fa-rotate-right"></i> Проверить результат</button>` : '')
            + `<button class="action-btn secondary-btn" style="font-size:12px;" data-act-prune="all"><i class="fa-solid fa-trash"></i> Очистить всё</button>`
            + `</span></div>`;
        const blocks = runs.map((run, idx) => {
            const [stTxt, stCol] = STAT[run.status] || STAT.running;
            const rows = (run.residents || []).map(p => {
                const b = p.before || {}, a = p.after || {};
                const [rTxt, rCol] = RES[p.result] || RES.unknown;
                const afterCells = (run.status === 'done' || run.status === 'checking')
                    ? `<td class="text-right">${fmt(a.gis)}</td><td class="text-center"><b style="color:${rCol}">${rTxt}</b></td>`
                    : `<td class="text-right" style="color:#9ca3af">ждём…</td><td class="text-center" style="color:#9ca3af">—</td>`;
                return `<tr><td>${esc(p.fio || p.username || p.user_id)}</td>`
                    + `<td class="text-right">${fmt(b.gis)}</td><td class="text-right">${fmt(b.c1)}</td>`
                    + afterCells
                    + `<td class="text-center">${(p.charges || []).length}</td></tr>`;
            }).join('');
            return `<details ${idx === 0 ? 'open' : ''} style="margin-bottom:10px; border:1px solid var(--border-color,#e5e7eb); border-radius:6px; padding:8px;">`
                + `<summary style="cursor:pointer; font-size:13px;">`
                + `<b>${dt(run.queued_at)}</b> · ${esc(run.by || '—')} · `
                + `<b>${run.residents_count || 0}</b> жильцов / <b>${run.total_charges || 0}</b> счетов · `
                + `<span style="color:${stCol}">${stTxt}</span>`
                + (run.status === 'checking' ? ` · попытка ${run.attempt || 1}/2` : '')
                + (run.loop_result && LRES[run.loop_result] ? ` · <b style="color:${LRES[run.loop_result][1]}">${LRES[run.loop_result][0]}</b>` : '')
                + (run.status !== 'running' ? ` · ok ${run.ok || 0}, ошибок ${run.fail || 0}` : '')
                + ` <button class="action-btn secondary-btn" style="font-size:11px; padding:2px 8px; margin-left:8px;" data-act-prune="${esc(run.id)}" title="Удалить прогон"><i class="fa-solid fa-xmark"></i></button>`
                + `</summary>`
                + `<div style="font-size:11px; color:var(--text-secondary); margin:4px 0;">Цель: ${esc(run.targeting || '')} · снимок «после»: ${run.after_at ? dt(run.after_at) : '—'}</div>`
                + `<div class="table-responsive" style="max-height:45vh; overflow:auto;"><table class="sticky-header-table" style="font-size:12px;">`
                + `<thead><tr><th>ФИО</th><th class="text-right">ГИС до</th><th class="text-right">1С</th>`
                + `<th class="text-right">ГИС после</th><th class="text-center">Результат</th><th class="text-center">Счетов</th></tr></thead>`
                + `<tbody>${rows || '<tr><td colspan="6" class="text-center">пусто</td></tr>'}</tbody></table></div>`
                + `</details>`;
        }).join('');
        body.innerHTML = head + blocks;
        body.querySelectorAll('[data-act-prune]').forEach(btn => {
            btn.addEventListener('click', (e) => { e.preventDefault(); e.stopPropagation(); this.pruneActualizeLog(btn.getAttribute('data-act-prune')); });
        });
        body.querySelector('[data-act-recheck]')?.addEventListener('click', () => this.recheckActualize());
    },

    // Проверить результат актуализации: переопрос ГИС по ФИО последнего прогона
    // (ГИС обрабатывает «Отправлен в ГИС ГМП» асинхронно — снимок сразу был бы пуст).
    async recheckActualize() {
        try {
            const r = await api.post('/financier/gisgmp/actualize-recheck', {});
            if (!r.queued) { toast(r.reason || 'Нет активных циклов', 'info'); return; }
            toast(`Запущен сбор для ${r.runs || r.queued} активных циклов — результат подтянется за ~2 мин.`, 'info');
            this.loadGisgmpStatus?.();
            setTimeout(async () => {
                try { const x = await api.get('/financier/gisgmp/actualize-log'); this._actualizeRuns = x.runs || []; this.renderActualizeLog(); } catch (e) { /* ignore */ }
            }, 2000);
        } catch (e) { toast('Ошибка перепроверки: ' + (e?.message || e), 'error'); }
    },

    async pruneActualizeLog(which) {
        const isAll = which === 'all';
        if (!await showConfirm(isAll ? 'Очистить ВСЮ историю актуализаций?' : 'Удалить этот прогон из истории?')) return;
        try {
            await api.post('/financier/gisgmp/actualize-log/prune', isAll ? { clear_all: true } : { delete_id: which });
            const r = await api.get('/financier/gisgmp/actualize-log');
            this._actualizeRuns = r.runs || [];
            this.renderActualizeLog();
            toast('История обновлена', 'info');
        } catch (e) { toast('Ошибка: ' + (e?.message || e), 'error'); }
    },

    // Управление демоном релея из UI (применяется на ближайшем опросе ~2 мин).
    async relayUpdate() {
        if (!await showConfirm('Обновить код релея до последней версии и перезапустить его? Релей подтянет свежий relay.py с сервера на ближайшем опросе (~2 мин).')) return;
        try {
            await api.post('/financier/gisgmp/relay-update', {});
            toast('Команда обновления поставлена. Релей применит и перезапустится (~2 мин).', 'info');
            this.loadGisgmpStatus();
        } catch (e) { toast('Ошибка: ' + (e?.message || e), 'error'); }
    },

    async relayRestart() {
        if (!await showConfirm('Перезапустить демон релея?')) return;
        try {
            await api.post('/financier/gisgmp/relay-restart', {});
            toast('Команда перезапуска поставлена (~2 мин).', 'info');
            this.loadGisgmpStatus();
        } catch (e) { toast('Ошибка: ' + (e?.message || e), 'error'); }
    },

    async relaySaveCreds() {
        const u = (this.dom.relayUser?.value || '').trim();
        const p = this.dom.relayPass?.value || '';
        if (!u || !p) { toast('Укажи логин и пароль', 'warning'); return; }
        if (!await showConfirm('Сменить учётку входа в реестр (passport) для релея? Релей запишет её в свой relay.env и перезапустится (~2 мин).')) return;
        try {
            await api.post('/financier/gisgmp/relay-credentials', { username: u, password: p });
            if (this.dom.relayPass) this.dom.relayPass.value = '';
            toast('Учётка сохранена (зашифровано). Релей применит на ближайшем опросе (~2 мин).', 'info');
            this.loadGisgmpStatus();
        } catch (e) { toast('Ошибка: ' + (e?.message || e), 'error'); }
    },

    // Сверка ГИС ГМП ↔ долги 1С: жилец = строка + авто-флаги проблем + фильтры.
    _RECON_LAB: {
        ok: ['ок', '#047857'],
        only_1c: ['нет в ГИС · дотянуть', '#2563eb'],
        only_gis: ['нет в 1С', '#d97706'],
        gis_more: ['ошибка ГИС ГМП', '#b91c1c'],
        c1_more: ['ГИС < 1С · дотянуть', '#7c3aed'],
    },

    async openGisgmpReconcile() {
        const body = this.dom.gisgmpReconcileBody;
        if (!body) return;
        if (body.style.display !== 'none') { body.style.display = 'none'; return; }
        body.style.display = 'block';
        body.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Сверяю…';
        try {
            const d = await api.get('/financier/gisgmp/reconcile');
            if (!d.has_findings) {
                body.innerHTML = '<span style="color:#92400e;">Нет находок ГИС ГМП — сначала «Запустить сейчас».</span>';
                return;
            }
            this._recon = d; this._reconFlag = '';
            const fmt = (v) => (Number(v) || 0).toLocaleString('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
            const a9 = d.accounts['209'] || {}, a5 = d.accounts['205'] || {};
            const src = (s) => s ? `${esc(s.file || '?')}${s.at ? ' от ' + new Date(s.at).toLocaleString('ru-RU') : ''}` : '<span style="color:#b91c1c">нет импорта</span>';
            const LAB = this._RECON_LAB, pr = d.problems || {};
            let chips = `<button class="action-btn primary-btn" data-rflag="" style="font-size:12px;padding:4px 10px;">Все</button> `;
            for (const k of ['gis_more', 'c1_more', 'only_1c', 'only_gis']) {
                const x = pr[k]; if (!x) continue;
                chips += `<button class="action-btn secondary-btn" data-rflag="${k}" style="font-size:12px;padding:4px 10px;border-left:3px solid ${LAB[k][1]};">`
                    + `${LAB[k][0]}: ${x.count} (${fmt(x.sum_abs)}₽${x.high ? ', крит ' + x.high : ''})</button> `;
            }
            body.innerHTML =
                `<div style="font-size:12px;color:var(--text-secondary);margin-bottom:6px;">`
                + `Источник 1С: <b>209</b> — ${src(d.source_1c && d.source_1c['209'])}; <b>205</b> — ${src(d.source_1c && d.source_1c['205'])}. `
                + `Синк ГИС: ${d.findings_at ? new Date(d.findings_at).toLocaleString('ru-RU') : '—'}.</div>`
                + `<div style="font-size:12px;margin-bottom:8px;"><b>209:</b> ГИС ${fmt(a9.sum_gisgmp)} / 1С ${fmt(a9.sum_1c)} (Δ ${fmt(a9.delta_total)}) &nbsp;·&nbsp; `
                + `<b>205:</b> ГИС ${fmt(a5.sum_gisgmp)} / 1С ${fmt(a5.sum_1c)} (Δ ${fmt(a5.delta_total)}) &nbsp;·&nbsp; совпало: ${d.matched_count || 0}</div>`
                + `<div style="margin-bottom:8px;display:flex;flex-wrap:wrap;gap:6px;align-items:center;">${chips}<button class="action-btn secondary-btn" id="reconPrint" style="font-size:12px;padding:4px 10px;"><i class="fa-solid fa-print"></i> Печать</button></div>`
                + `<input type="text" id="reconSearch" placeholder="Фильтр по фамилии…" style="width:240px;padding:6px 8px;margin-bottom:8px;">`
                + `<div id="reconResult"></div>`;
            body.querySelectorAll('[data-rflag]').forEach(b => b.addEventListener('click', () => {
                this._reconFlag = b.getAttribute('data-rflag');
                body.querySelectorAll('[data-rflag]').forEach(x => {
                    const on = x.getAttribute('data-rflag') === this._reconFlag;
                    x.classList.toggle('primary-btn', on);
                    x.classList.toggle('secondary-btn', !on);
                });
                this.renderGisgmpReconcile();
            }));
            document.getElementById('reconPrint')?.addEventListener('click', () => this.printGisgmpReconcile());
            document.getElementById('reconSearch')?.addEventListener('input', () => this.renderGisgmpReconcile());
            this.renderGisgmpReconcile();
        } catch (e) {
            body.innerHTML = 'Ошибка сверки: ' + esc(e?.message || String(e));
        }
    },

    renderGisgmpReconcile() {
        const d = this._recon, res = document.getElementById('reconResult');
        if (!d || !res) return;
        const q = (document.getElementById('reconSearch')?.value || '').trim().toLowerCase();
        const flt = this._reconFlag || '';
        const LAB = this._RECON_LAB;
        const fmt = (v) => { const n = Number(v) || 0; return n ? n.toLocaleString('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : '—'; };
        const dcell = (v) => {
            const n = Number(v) || 0;
            if (Math.abs(n) < 0.01) return '<td class="text-right" style="color:#047857">0</td>';
            const c = n > 0 ? '#b91c1c' : '#2563eb';
            return `<td class="text-right" style="color:${c}">${n > 0 ? '+' : ''}${fmt(n)}</td>`;
        };
        let list = d.residents || [];
        if (flt) list = list.filter(r => r.flag === flt);
        if (q) list = list.filter(r => (r.username || '').toLowerCase().includes(q));
        const rows = list.map(r => {
            const L = LAB[r.flag] || ['', '#666'];
            return `<tr><td><a href="#" class="recon-payer" data-fio="${esc(r.username)}" data-uid="${r.user_id}" style="color:#2563eb;cursor:pointer;">${esc(r.username)}</a></td>`
                + `<td class="text-right">${fmt(r.g209)}</td><td class="text-right">${fmt(r.c209)}</td>${dcell(r.d209)}`
                + `<td class="text-right">${fmt(r.g205)}</td><td class="text-right">${fmt(r.c205)}</td>${dcell(r.d205)}`
                + `<td class="text-right"><b>${fmt(r.delta)}</b></td>`
                + `<td style="text-align:center;font-size:11px;">${r.gis_months || 0}${r.need_pull ? ' <span style="color:#2563eb;" title="ГИС занижен — стоит дотянуть">⤓</span>' : ''}</td>`
                + `<td><span style="color:${L[1]};font-size:11px;">${L[0]}${r.severity === 'high' ? ' ⚠' : ''}</span></td></tr>`;
        }).join('');
        // Несопоставленные (1С/ГИС есть, жильца в базе нет) — блок внизу, чтобы НЕ ТЕРЯТЬ людей.
        let orph = d.orphans || [];
        if (q) orph = orph.filter(o => (o.fio || '').toLowerCase().includes(q));
        const orphRows = orph.map(o => {
            const where = [];
            if (o.gis_209 || o.gis_205) where.push('ГИС');
            if (o.c1_209 || o.c1_205) where.push('1С');
            return `<tr style="background:#fffbeb;"><td>${esc(o.fio)}</td>`
                + `<td class="text-right">${fmt(o.gis_209)}</td><td class="text-right">${fmt(o.gis_205)}</td>`
                + `<td class="text-right">${fmt(o.c1_209)}</td><td class="text-right">${fmt(o.c1_205)}</td>`
                + `<td style="font-size:11px;color:#92400e;">в ${where.join('+') || '—'}, нет в базе</td>`
                + `<td><button class="recon-linkorph" data-fio="${esc(o.fio)}" style="font-size:11px;color:#fff;background:#0ea5e9;border:none;border-radius:4px;padding:2px 7px;cursor:pointer;">Привязать</button></td></tr>`;
        }).join('');
        res.innerHTML = `<div style="font-size:12px;color:var(--text-secondary);margin-bottom:4px;">Показано: ${list.length}. ⚠ = крупное расхождение (≥20k). Красный Δ = ГИС больше, синий = 1С больше.</div>`
            + `<div class="table-responsive" style="max-height:55vh;overflow:auto;"><table class="sticky-header-table" style="font-size:12px;">`
            + `<thead><tr><th>Жилец</th><th class="text-right">209 ГИС</th><th class="text-right">209 1С</th><th class="text-right">Δ209</th>`
            + `<th class="text-right">205 ГИС</th><th class="text-right">205 1С</th><th class="text-right">Δ205</th><th class="text-right">Σ Δ</th><th title="За сколько разных месяцев долг в ГИС; ⤓ = стоит дотянуть">ГИС, мес</th><th>Флаг</th></tr></thead>`
            + `<tbody>${rows || '<tr><td colspan="10" class="text-center">нет</td></tr>'}</tbody></table></div>`
            + (orph.length ? `<div style="font-size:13px;font-weight:600;color:#92400e;margin:14px 0 4px;">🔶 Не найдены в базе — ${orph.length} (есть в 1С/ГИС, жильца нет). Не теряем — их долги ниже. «Привязать» свяжет с жильцом.</div>`
                + `<div class="table-responsive" style="max-height:40vh;overflow:auto;"><table class="sticky-header-table" style="font-size:12px;">`
                + `<thead><tr><th>ФИО (1С/ГИС)</th><th class="text-right">209 ГИС</th><th class="text-right">205 ГИС</th><th class="text-right">209 1С</th><th class="text-right">205 1С</th><th>Где</th><th>Действие</th></tr></thead>`
                + `<tbody>${orphRows}</tbody></table></div>` : '');
        res.querySelectorAll('.recon-payer').forEach(a => a.addEventListener('click', (e) => {
            e.preventDefault();
            this.openPayerCharges(a.getAttribute('data-fio'), a.getAttribute('data-uid'));
        }));
        res.querySelectorAll('.recon-linkorph').forEach(b => b.addEventListener('click', () => this.linkFioPrompt(b.getAttribute('data-fio'))));
    },

    // Печатный отчёт сверки — чистый документ в новом окне → печать/PDF из браузера.
    printGisgmpReconcile() {
        const d = this._recon;
        if (!d) { toast('Сначала открой «Сверка с 1С»', 'info'); return; }
        const LAB = this._RECON_LAB;
        const fmt = (v) => (Number(v) || 0).toLocaleString('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        const dlt = (v) => { const n = Number(v) || 0; return n ? (n > 0 ? '+' : '') + fmt(n) : '0'; };
        const src = (s) => s ? `${s.file || '?'}${s.at ? ' от ' + new Date(s.at).toLocaleString('ru-RU') : ''}` : 'нет импорта';
        const a9 = d.accounts['209'] || {}, a5 = d.accounts['205'] || {}, pr = d.problems || {};
        const now = new Date().toLocaleString('ru-RU');
        const probRows = ['gis_more', 'c1_more', 'only_1c', 'only_gis'].filter(k => pr[k])
            .map(k => `<tr><td>${LAB[k][0]}</td><td class=r>${pr[k].count}</td><td class=r>${fmt(pr[k].sum_abs)}</td><td class=r>${pr[k].high || 0}</td></tr>`).join('');
        const rows = (d.residents || []).map(r => {
            const L = LAB[r.flag] || ['', ''];
            return `<tr><td>${esc(r.username)}</td><td class=r>${fmt(r.g209)}</td><td class=r>${fmt(r.c209)}</td><td class=r>${dlt(r.d209)}</td>`
                + `<td class=r>${fmt(r.g205)}</td><td class=r>${fmt(r.c205)}</td><td class=r>${dlt(r.d205)}</td><td class=r><b>${dlt(r.delta)}</b></td>`
                + `<td class=r>${r.gis_months || 0}${r.need_pull ? ' (дотянуть)' : ''}</td><td>${L[0]}</td></tr>`;
        }).join('');
        const orphPrint = (d.orphans || []).map(o => `<tr><td>${esc(o.fio)}</td><td class=r>${fmt(o.gis_209)}</td><td class=r>${fmt(o.gis_205)}</td><td class=r>${fmt(o.c1_209)}</td><td class=r>${fmt(o.c1_205)}</td></tr>`).join('');
        const html = `<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Сверка ГИС ГМП — 1С</title>
<style>
body{font-family:Arial,Helvetica,sans-serif;font-size:11px;color:#111;margin:16px;}
h1{font-size:17px;margin:0 0 2px;} h2{font-size:13px;margin:14px 0 4px;}
.sub{color:#555;margin-bottom:8px;line-height:1.4;}
table{border-collapse:collapse;width:100%;margin-bottom:8px;}
th,td{border:1px solid #bbb;padding:3px 5px;} th{background:#eee;text-align:left;}
td.r,th.r{text-align:right;white-space:nowrap;} tr:nth-child(even){background:#fafafa;}
@media print{body{margin:8px;} tr{page-break-inside:avoid;} thead{display:table-header-group;}}
</style></head><body>
<h1>Сверка ГИС ГМП ↔ долги 1С (жильцы)</h1>
<div class="sub">Сформировано: ${now} · Синк ГИС: ${d.findings_at ? new Date(d.findings_at).toLocaleString('ru-RU') : '—'}<br>
Источник 1С: <b>209</b> — ${esc(src(d.source_1c && d.source_1c['209']))}; <b>205</b> — ${esc(src(d.source_1c && d.source_1c['205']))}</div>
<h2>Итоги по счетам</h2>
<table><tr><th>Счёт</th><th class=r>ГИС ГМП</th><th class=r>1С (Excel)</th><th class=r>Разница</th><th class=r>Совпало</th></tr>
<tr><td>209 — коммуслуги</td><td class=r>${fmt(a9.sum_gisgmp)}</td><td class=r>${fmt(a9.sum_1c)}</td><td class=r>${dlt(a9.delta_total)}</td><td class=r>${a9.matched || 0}</td></tr>
<tr><td>205 — наём</td><td class=r>${fmt(a5.sum_gisgmp)}</td><td class=r>${fmt(a5.sum_1c)}</td><td class=r>${dlt(a5.delta_total)}</td><td class=r>${a5.matched || 0}</td></tr></table>
<h2>Проблемы (авто-флаги)</h2>
<table><tr><th>Категория</th><th class=r>Жильцов</th><th class=r>Сумма Δ, ₽</th><th class=r>Крупных (≥20k)</th></tr>${probRows || '<tr><td colspan=4>расхождений нет</td></tr>'}</table>
<h2>Разбор по жильцам — ${(d.residents || []).length} (сортировка по |разнице|)</h2>
<table><thead><tr><th>Жилец</th><th class=r>209 ГИС</th><th class=r>209 1С</th><th class=r>Δ209</th><th class=r>205 ГИС</th><th class=r>205 1С</th><th class=r>Δ205</th><th class=r>Σ Δ</th><th class=r>ГИС, мес</th><th>Флаг</th></tr></thead><tbody>${rows}</tbody></table>
${(d.orphans || []).length ? `<h2>Не найдены в базе (1С/ГИС есть, жильца нет) — ${(d.orphans || []).length}</h2>
<table><thead><tr><th>ФИО</th><th class=r>209 ГИС</th><th class=r>205 ГИС</th><th class=r>209 1С</th><th class=r>205 1С</th></tr></thead><tbody>${orphPrint}</tbody></table>` : ''}
<script>window.onload=function(){setTimeout(function(){window.print();},250);};</script>
</body></html>`;
        const w = window.open('', '_blank');
        if (!w) { toast('Разреши всплывающие окна для печати', 'warning'); return; }
        w.document.write(html);
        w.document.close();
    },

    // Клик по ФИО → модал со ВСЕМИ начислениями + предложение аннулирования.
    async openPayerCharges(fio, uid) {
        let modal = document.getElementById('payerChargesModal');
        if (!modal) {
            modal = document.createElement('div');
            modal.id = 'payerChargesModal';
            modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:9999;display:flex;align-items:flex-start;justify-content:center;padding:36px 16px;overflow:auto;';
            modal.addEventListener('click', (e) => { if (e.target === modal) modal.remove(); });
            document.body.appendChild(modal);
        }
        modal.innerHTML = `<div style="background:var(--bg-primary,#fff);color:var(--text-primary,#111);border-radius:10px;max-width:940px;width:100%;padding:18px 20px;box-shadow:0 12px 48px rgba(0,0,0,.35);">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
              <div style="font-size:15px;font-weight:600;">Начисления ГИС ГМП: ${esc(fio)}</div>
              <button id="payerClose" class="action-btn secondary-btn" style="font-size:13px;">✕ Закрыть</button>
            </div>
            <div id="payerBody"><i class="fa-solid fa-spinner fa-spin"></i> Загружаю…</div>
          </div>`;
        modal.querySelector('#payerClose').addEventListener('click', () => modal.remove());
        try {
            const resident = uid ? (this._recon?.residents || []).find(r => String(r.user_id) === String(uid)) : null;
            const d = await api.get('/financier/gisgmp/payer-charges?q=' + encodeURIComponent(fio));
            this.renderPayerCharges(modal.querySelector('#payerBody'), d, resident);
        } catch (e) {
            const b = modal.querySelector('#payerBody');
            if (b) b.innerHTML = 'Ошибка: ' + esc(e?.message || String(e));
        }
    },

    renderPayerCharges(box, d, resident) {
        if (!box) return;
        const fmt = (v) => (Number(v) || 0).toLocaleString('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        if (!d.count) {
            box.innerHTML = '<span style="color:#92400e;">В кэше ГИС ГМП начислений по этой фамилии нет — возможно, не дотянуто (нажми «Дотянуть расхождения»).</span>';
            return;
        }
        const t = d.totals || {};
        const ST = {
            unpaid: ['Не сквитировано (долг)', '#b91c1c'],
            paid: ['Сквитировано', '#047857'],
            annulled: ['Аннулировано', '#6b7280'],
        };
        // Предложение к аннулированию (ошибка ГИС ГМП): когда ГИС > 1С — берём
        // СТАРЕЙШИЕ неоплаченные счета на сумму превышения по каждому счёту →
        // их аннулировать в реестре, итог сравняется с 1С («старый хвост, что
        // 1С уже закрыл»).
        const annul = new Set();
        let banner = '';
        if (resident) {
            const pick = (acc, delta) => {
                if (!(delta > 0.01)) return null;
                const list = (d.charges || []).filter(c => c.account_type === acc && c.status === 'unpaid')
                    .sort((a, b) => this._billTs(a.bill_date) - this._billTs(b.bill_date));
                let s = 0, n = 0;
                for (const c of list) { annul.add(c.uin); s += Number(c.amount) || 0; n++; if (s >= delta - 0.01) break; }
                return n ? `<b>${acc}</b>: ${n} счёт(ов) на <b>${fmt(s)}₽</b>` : null;
            };
            const parts = [pick('209', resident.d209), pick('205', resident.d205)].filter(Boolean);
            if (parts.length) {
                banner = `<div style="background:#fef2f2;border:1px solid #fca5a5;border-radius:8px;padding:10px 12px;margin-bottom:10px;font-size:12px;line-height:1.5;">
                    <b style="color:#b91c1c;">⚠ Ошибка ГИС ГМП — предложение аннулировать (сравнять с 1С):</b><br>
                    ${parts.join(' &nbsp;·&nbsp; ')}.<br>
                    <span style="color:#666;">Эти счета (подсвечены ниже 🔻) аннулируй вручную в реестре ГИС ГМП по их УИН — итог долга сравняется с 1С. Подобраны старейшие неоплаченные = «хвост», который 1С уже закрыл.</span></div>`;
            }
        }
        const rows = (d.charges || []).map(c => {
            const s = ST[c.status] || ['—', '#666'];
            const hl = annul.has(c.uin);
            return `<tr style="${hl ? 'background:#fee2e2;' : ''}">
                <td>${esc(c.bill_date || '—')}</td>
                <td style="text-align:center;">${c.account_type || '—'}</td>
                <td class="text-right">${fmt(c.amount)}</td>
                <td><span style="color:${s[1]};font-weight:600;">${s[0]}</span>${hl ? ' <span title="Предложено аннулировать" style="color:#b91c1c;">🔻</span>' : ''}</td>
                <td style="font-size:11px;color:var(--text-secondary,#666);">${esc(c.purpose || '')}</td>
                <td style="font-size:10px;color:#999;font-family:monospace;">${esc(c.uin || '')}</td>
            </tr>`;
        }).join('');
        box.innerHTML = banner + `<div style="font-size:13px;margin-bottom:10px;line-height:1.6;">
                Долг <b>209</b> (комуслуги): <b style="color:#b91c1c">${fmt(t.debt_209)}</b> &nbsp;·&nbsp;
                Долг <b>205</b> (наём): <b style="color:#b91c1c">${fmt(t.debt_205)}</b><br>
                Оплачено (сквитировано): ${fmt(t.paid)} &nbsp;·&nbsp; аннулировано строк: ${t.annulled || 0} &nbsp;·&nbsp; всего строк: ${t.count || 0}</div>
            <div class="table-responsive" style="max-height:60vh;overflow:auto;"><table class="sticky-header-table" style="font-size:12px;width:100%;">
                <thead><tr><th>Период (нач.)</th><th>Счёт</th><th class="text-right">Сумма</th><th>Статус</th><th>Назначение</th><th>УИН</th></tr></thead>
                <tbody>${rows}</tbody></table></div>`;
    },

    _billTs(s) {
        const m = String(s || '').match(/(\d{2})\.(\d{2})\.(\d{4})/);
        return m ? new Date(+m[3], +m[2] - 1, +m[1]).getTime() : 0;
    },

    // Перечитать сверку без сворачивания.
    async reloadReconcile() {
        if (!this._recon) return;
        try {
            const d = await api.get('/financier/gisgmp/reconcile');
            if (d && d.has_findings) { this._recon = d; this.renderGisgmpReconcile(); }
        } catch (e) { /* тихо */ }
    },

    cacheDOM() {
        this.dom = {
            // Таблица
            tableBody: document.getElementById('debtsTableBody'),
            btnRefresh: document.getElementById('btnRefreshDebts'),
            btnExport: document.getElementById('btnExportDebts'),
            btnZombieCheck: document.getElementById('btnZombieCheck'),
            btnIntegrityCheck: document.getElementById('btnIntegrityCheck'),
            btnPrev: document.getElementById('btnPrevDebts'),
            btnNext: document.getElementById('btnNextDebts'),
            pageInfo: document.getElementById('debtsPageInfo'),
            searchInput: document.getElementById('debtsSearchInput'),
            // Фильтры
            filterType: document.getElementById('debtsFilterType'),
            filterDorm: document.getElementById('debtsFilterDormitory'),
            minDebt: document.getElementById('debtsMinDebt'),
            hideEmpty: document.getElementById('debtsHideEmpty'),
            // Импорт
            btnUpload: document.getElementById('btnUploadDebts'),
            // Парный импорт v2 — два отдельных file-input. Старый
            // #debtFile1C оставлен fallback'ом если HTML где-то ещё
            // содержит legacy-шаблон, но в актуальном tab_debts.html
            // его нет.
            inputUpload209: document.getElementById('debtFile209'),
            inputUpload205: document.getElementById('debtFile205'),
            inputUpload: document.getElementById('debtFile1C'),
            uploadResult: document.getElementById('uploadResult'),
            periodSelect: document.getElementById('debtPeriodSelect'),
            viewPeriod: document.getElementById('debtsViewPeriod'),
            unassignedCard: document.getElementById('debtsUnassignedCard'),
            unassignedMeta: document.getElementById('debtsUnassignedMeta'),
            unassignedBody: document.getElementById('debtsUnassignedBody'),
            btnToggleUnassigned: document.getElementById('btnToggleUnassigned'),
            // KPI
            stats: document.getElementById('debtsStats'),
            // История
            importHistoryList: document.getElementById('importHistoryList'),
            btnRefreshImportHistory: document.getElementById('btnRefreshImportHistory'),
            // Авто-подгрузка ГИС ГМП (серверный релей)
            gisgmpStatus: document.getElementById('gisgmpStatus'),
            btnPublishDebts: document.getElementById('btnPublishDebts'),
            btnRematchBase: document.getElementById('btnRematchBase'),
            debtsStagedStatus: document.getElementById('debtsStagedStatus'),
            gisgmpEnabled: document.getElementById('gisgmpEnabled'),
            gisgmpMonths: document.getElementById('gisgmpMonths'),
            gisgmpHour: document.getElementById('gisgmpHour'),
            btnGisgmpSave: document.getElementById('btnGisgmpSave'),
            btnGisgmpRunNow: document.getElementById('btnGisgmpRunNow'),
            btnGisgmpFindings: document.getElementById('btnGisgmpFindings'),
            gisgmpFindingsBody: document.getElementById('gisgmpFindingsBody'),
            btnGisgmpReconcile: document.getElementById('btnGisgmpReconcile'),
            gisgmpReconcileBody: document.getElementById('gisgmpReconcileBody'),
            btnGisgmpReconcileFio: document.getElementById('btnGisgmpReconcileFio'),
            btnGisgmpCreateMissing: document.getElementById('btnGisgmpCreateMissing'),
            btnGisgmpPurge: document.getElementById('btnGisgmpPurge'),
            gisgmpReconcileFioBody: document.getElementById('gisgmpReconcileFioBody'),
            btnGisgmpRecheck: document.getElementById('btnGisgmpRecheck'),
            btnGisgmpActualize: document.getElementById('btnGisgmpActualize'),
            btnGisgmpActualizeAll: document.getElementById('btnGisgmpActualizeAll'),
            btnGisgmpActualizeLog: document.getElementById('btnGisgmpActualizeLog'),
            gisgmpActualizeLogBody: document.getElementById('gisgmpActualizeLogBody'),
            btnRelayUpdate: document.getElementById('btnRelayUpdate'),
            btnRelayRestart: document.getElementById('btnRelayRestart'),
            btnRelayCreds: document.getElementById('btnRelayCreds'),
            relayUser: document.getElementById('relayUser'),
            relayPass: document.getElementById('relayPass'),
            // 1С (БГУ) авто-подгрузка через релей
            onecEnabled: document.getElementById('onecEnabled'),
            onecAccNaem: document.getElementById('onecAccNaem'),
            onecAccComm: document.getElementById('onecAccComm'),
            onecHour: document.getElementById('onecHour'),
            onecBaseUrl: document.getElementById('onecBaseUrl'),
            onecInfobase: document.getElementById('onecInfobase'),
            onecLogin: document.getElementById('onecLogin'),
            onecPass: document.getElementById('onecPass'),
            btnOnecSave: document.getElementById('btnOnecSave'),
            btnOnecCreds: document.getElementById('btnOnecCreds'),
            btnOnecRunNow: document.getElementById('btnOnecRunNow'),
            btnOnecProbe: document.getElementById('btnOnecProbe'),
            btnOnecFound: document.getElementById('btnOnecFound'),
            onecFoundBody: document.getElementById('onecFoundBody'),
            onecStatus: document.getElementById('onecStatus'),
            // Модалка корректировки
            adjustModal: document.getElementById('debtAdjustModal'),
            adjustForm: document.getElementById('debtAdjustForm'),
            adjustUserId: document.getElementById('adjustUserId'),
            adjustUserName: document.getElementById('adjustUserName'),
            adjustAccount: document.getElementById('adjustAccount'),
            adjustAmount: document.getElementById('adjustAmount'),
            adjustTemplate: document.getElementById('adjustTemplate'),
            adjustDescription: document.getElementById('adjustDescription'),
            // Модалка «не найдено»
            notFoundModal: document.getElementById('notFoundModal'),
            notFoundList: document.getElementById('notFoundList'),
            notFoundLogMeta: document.getElementById('notFoundLogMeta'),
        };
    },

    bindEvents() {
        // Переключатель режима учёта: жильцы (ФИО) / квартиры (адрес).
        document.getElementById('debtsModeUsers')?.addEventListener('click', () => this.setMode('users'));
        document.getElementById('debtsModeRooms')?.addEventListener('click', () => this.setMode('rooms'));
        // Период просмотра долгов (когда активного нет — выбрать май/апрель).
        this.dom.viewPeriod?.addEventListener('change', () => {
            this.state.viewPeriodId = this.dom.viewPeriod.value || '';
            this.state.page = 1;
            this.loadStats();
            this.loadUsers();
            this.loadUnassigned();
        });
        this.dom.btnToggleUnassigned?.addEventListener('click', () => {
            const b = this.dom.unassignedBody;
            if (!b) return;
            const show = b.style.display === 'none';
            b.style.display = show ? 'block' : 'none';
            this.dom.btnToggleUnassigned.textContent = show ? 'Скрыть список' : 'Показать список';
        });
        this.dom.btnRefresh?.addEventListener('click', () => this.reload());
        this.dom.btnExport?.addEventListener('click', () => this.exportExcel());
        this.dom.btnZombieCheck?.addEventListener('click', () => this.openZombieModal());
        this.dom.btnIntegrityCheck?.addEventListener('click', () => this.openIntegrityModal());
        this.dom.btnGisgmpSave?.addEventListener('click', () => this.saveGisgmpRelay());
        this.dom.btnGisgmpRunNow?.addEventListener('click', () => this.runGisgmpNow());
        this.dom.gisgmpEnabled?.addEventListener('change', () => this.saveGisgmpRelay());
        this.dom.btnGisgmpFindings?.addEventListener('click', () => this.openGisgmpFindings());
        this.dom.btnGisgmpReconcile?.addEventListener('click', () => this.openGisgmpReconcile());
        this.dom.btnGisgmpReconcileFio?.addEventListener('click', () => this.openReconcileFio());
        this.dom.btnGisgmpCreateMissing?.addEventListener('click', () => this.createMissingResidents());
        this.dom.btnGisgmpPurge?.addEventListener('click', () => this.purgeGisgmp());
        this._initGisDropdowns();
        this.dom.btnGisgmpRecheck?.addEventListener('click', () => this.recheckGisgmp());
        this.dom.btnGisgmpActualize?.addEventListener('click', () => this.actualizeGisgmp());
        this.dom.btnGisgmpActualizeAll?.addEventListener('click', () => this.actualizeAllGisgmp());
        this.dom.btnGisgmpActualizeLog?.addEventListener('click', () => this.openActualizeLog());
        this.dom.btnRelayUpdate?.addEventListener('click', () => this.relayUpdate());
        this.dom.btnRelayRestart?.addEventListener('click', () => this.relayRestart());
        this.dom.btnRelayCreds?.addEventListener('click', () => this.relaySaveCreds());
        // 1С (БГУ) авто-подгрузка
        this.dom.btnOnecSave?.addEventListener('click', () => this.saveOnecConfig());
        this.dom.onecEnabled?.addEventListener('change', () => this.saveOnecConfig());
        this.dom.btnOnecCreds?.addEventListener('click', () => this.saveOnecCreds());
        this.dom.btnOnecRunNow?.addEventListener('click', () => this.runOnec(false));
        this.dom.btnOnecProbe?.addEventListener('click', () => this.runOnec(true));
        this.dom.btnOnecFound?.addEventListener('click', () => this.loadOnecFound());
        this.dom.btnUpload?.addEventListener('click', () => this.handleUpload());
        this.dom.btnPublishDebts?.addEventListener('click', () => this.publishDebts());
        this.dom.btnRematchBase?.addEventListener('click', () => this.rematchBase());

        // Авто-предпросмотр при выборе файла (Bug T)
        this.dom.inputUpload209?.addEventListener('change', () => this.previewFile('209'));
        this.dom.inputUpload205?.addEventListener('change', () => this.previewFile('205'));
        // Сохраним признак дубликата для блокировки upload.
        this._lastPreview209 = null;
        this._lastPreview205 = null;
        this.dom.btnPrev?.addEventListener('click', () => this.changePage(-1));
        this.dom.btnNext?.addEventListener('click', () => this.changePage(1));

        const refilter = () => {
            this.state.filterType = this.dom.filterType?.value || '';
            this.state.dormitory = this.dom.filterDorm?.value || '';
            this.state.minDebt = this.dom.minDebt?.value || '';
            this.state.page = 1;
            this.loadUsers();
        };
        this.dom.filterType?.addEventListener('change', refilter);
        this.dom.filterDorm?.addEventListener('change', refilter);

        let minDebtTimer;
        this.dom.minDebt?.addEventListener('input', () => {
            clearTimeout(minDebtTimer);
            minDebtTimer = setTimeout(refilter, 400);
        });

        // Bug AB: «Скрыть пустых» — пользователи без данных из 1С
        this.dom.hideEmpty?.addEventListener('change', (e) => {
            this.state.hideEmpty = e.target.checked;
            this.state.page = 1;
            this.loadUsers();
        });

        let searchTimer;
        this.dom.searchInput?.addEventListener('input', (e) => {
            clearTimeout(searchTimer);
            searchTimer = setTimeout(() => {
                this.state.search = e.target.value || '';
                this.state.page = 1;
                this.loadUsers();
            }, 400);
        });

        // Сортировка по клику на заголовок
        document.querySelectorAll('[data-debt-sort]').forEach(th => {
            th.addEventListener('click', () => {
                const field = th.dataset.debtSort;
                if (this.state.sortBy === field) {
                    this.state.sortDir = this.state.sortDir === 'asc' ? 'desc' : 'asc';
                } else {
                    this.state.sortBy = field;
                    this.state.sortDir = field === 'debt' ? 'desc' : 'asc';
                }
                this.updateSortIcons();
                this.state.page = 1;
                this.loadUsers();
            });
        });

        // Модалка корректировки
        this.dom.adjustModal?.addEventListener('click', (e) => {
            if (e.target.closest('[data-adjust-close]')) this.closeAdjustModal();
        });
        this.dom.adjustForm?.addEventListener('submit', (e) => this.submitAdjust(e));
        this.dom.adjustTemplate?.addEventListener('change', (e) => {
            const val = e.target.value;
            if (val && this.dom.adjustDescription && !this.dom.adjustDescription.value.trim()) {
                this.dom.adjustDescription.value = val;
            }
        });

        // Модалка «не найдено»
        this.dom.notFoundModal?.addEventListener('click', (e) => {
            if (e.target.closest('[data-nf-close]')) this.closeNotFoundModal();
        });

        // История импортов
        this.dom.btnRefreshImportHistory?.addEventListener('click', () => this.loadImportHistory());
        this.dom.importHistoryList?.addEventListener('click', (e) => {
            const btn = e.target.closest('button[data-history-action]');
            if (!btn) return;
            const action = btn.dataset.historyAction;
            const logId = Number(btn.dataset.logId);
            if (action === 'view-not-found') this.openNotFoundModal(logId);
            else if (action === 'undo') this.undoImport(logId);
            else if (action === 'diff') this.openDiffModal(logId);
            else if (action === 'diagnose') this.openDiagnoseModal(logId);
            else if (action === 'reparse') this.reparseImport(logId);  // Bug AE
            else if (action === 'delete') this.deleteImportHistory(logId);
            else if (action === 'cleanup') this.cleanupImportHistory();
        });
    },

    reload() { this.state.page = 1; this.loadUsers(); this.loadStats(); this.loadUnassigned(); },

    changePage(delta) {
        const newPage = this.state.page + delta;
        if (newPage < 1) return;
        this.state.page = newPage;
        this.loadUsers();
    },

    updateSortIcons() {
        document.querySelectorAll('[data-debt-sort]').forEach(th => {
            th.classList.remove('sort-asc', 'sort-desc');
            if (th.dataset.debtSort === this.state.sortBy) {
                th.classList.add(`sort-${this.state.sortDir}`);
            }
        });
    },

    clearPoll() {
        if (this.state.pollTimer) { clearTimeout(this.state.pollTimer); this.state.pollTimer = null; }
        this.state.currentPollId = null;
    },

    // Список периодов для дропдауна «Период просмотра» (шапка списка долгов).
    async loadViewPeriods() {
        const sel = this.dom.viewPeriod;
        if (!sel) return;
        try {
            const periods = await api.get('/admin/periods/history');
            const prev = sel.value;
            sel.innerHTML = '';
            (periods || []).forEach(p => {
                const opt = document.createElement('option');
                opt.value = String(p.id);
                opt.textContent = p.name + (p.is_active ? ' (активный)' : '');
                sel.appendChild(opt);
            });
            if (prev) sel.value = prev;
        } catch { /* молча — останется заглушка «Период…» */ }
    },

    // Сводка неразнесённых долгов (ФИО из 1С, не привязанные к жильцу/комнате).
    async loadUnassigned() {
        const card = this.dom.unassignedCard;
        if (!card) return;
        try {
            const qs = this.state.viewPeriodId ? `?period_id=${this.state.viewPeriodId}` : '';
            const d = await api.get(`/financier/debts/unassigned${qs}`);
            if (!d.count) { card.style.display = 'none'; return; }
            card.style.display = '';
            if (this.dom.unassignedMeta) {
                this.dom.unassignedMeta.textContent = `— ${fmtMoney(d.total_debt)} ₽ · ${d.count} ФИО`;
            }
            const rows = (d.items || []).map(it => `
                <tr style="border-bottom:1px solid var(--border-color);">
                    <td style="padding:6px 10px;">${esc(it.fio)}</td>
                    <td style="padding:6px 10px; text-align:center; color:var(--text-secondary); font-size:11px;">${esc((it.accounts || []).join(', '))}</td>
                    <td style="padding:6px 10px; text-align:right; color:#991b1b; font-weight:600;">${it.debt > 0 ? fmtMoney(it.debt) + ' ₽' : '—'}</td>
                    <td style="padding:6px 10px; text-align:right; color:#15803d;">${it.overpayment > 0 ? fmtMoney(it.overpayment) + ' ₽' : '—'}</td>
                </tr>`).join('');
            if (this.dom.unassignedBody) {
                this.dom.unassignedBody.innerHTML = `
                    <div style="overflow-x:auto; border:1px solid var(--border-color); border-radius:8px;">
                        <table style="width:100%; border-collapse:collapse; font-size:13px; min-width:480px;">
                            <thead style="background:var(--bg-page); font-size:11px; color:var(--text-secondary); text-transform:uppercase;">
                                <tr>
                                    <th style="text-align:left; padding:6px 10px;">ФИО из 1С</th>
                                    <th style="text-align:center; padding:6px 10px;">Счета</th>
                                    <th style="text-align:right; padding:6px 10px;">Долг</th>
                                    <th style="text-align:right; padding:6px 10px;">Переплата</th>
                                </tr>
                            </thead>
                            <tbody>${rows}</tbody>
                        </table>
                    </div>`;
            }
        } catch { /* молча — карточка просто не покажется */ }
    },

    // ==========================================================================
    // KPI
    // ==========================================================================
    async loadStats() {
        if (!this.dom.stats) return;
        try {
            const qs = this.state.viewPeriodId ? `?period_id=${this.state.viewPeriodId}` : '';
            const s = await api.get(`/financier/debts/stats${qs}`);
            // «Авто»-режим: подтягиваем дропдаун к периоду, который выбрал бэк
            // (активный → последний импорт → свежий), чтобы было видно, что
            // показываем именно май, а не пустой активный период.
            if (!this.state.viewPeriodId && s.period_id) {
                this.state.viewPeriodId = String(s.period_id);
                if (this.dom.viewPeriod) this.dom.viewPeriod.value = String(s.period_id);
            }
            this.renderStats(s);
        } catch (e) {
            this.dom.stats.innerHTML = `<div style="padding:14px; color:var(--danger-color); grid-column:1/-1;">Ошибка аналитики: ${esc(e.message)}</div>`;
        }
    },

    renderStats(s) {
        const card = (bg, color, icon, value, label, hint) => `
            <div style="background:${bg}; border-radius:10px; padding:14px 12px; border:1px solid ${color}33;">
                <div style="display:flex; align-items:center; gap:8px; color:${color}; font-size:12px; margin-bottom:4px;">
                    <span style="font-size:16px;">${icon}</span>${esc(label)}
                </div>
                <div style="font-size:20px; font-weight:700; color:#111827;">${value}</div>
                ${hint ? `<div style="font-size:11px; color:var(--text-secondary); margin-top:2px;">${esc(hint)}</div>` : ''}
            </div>
        `;

        const lastImp = s.last_import;
        const lastHtml = lastImp
            ? `счёт ${lastImp.account_type}, ${fmtDateTime(lastImp.started_at)} · ${lastImp.started_by || '—'}`
            : 'импортов ещё не было';

        // Режим «Квартиры»: считаем помещения, а не людей.
        const rooms = this.state.mode === 'rooms';

        this.dom.stats.innerHTML = [
            card('#f5f3ff', '#7c3aed', '📅', s.period_name || '—', 'Период (просмотр)',
                rooms ? `всего квартир: ${s.total_rooms ?? '—'}` : `всего жильцов: ${s.total_users}`),
            rooms
                ? card('#fef2f2', '#dc2626', '🏠', s.rooms_with_debt_count ?? 0, 'Квартир с долгом',
                    `средний долг: ${fmtMoney(s.avg_debt_per_room)} ₽ · жильцов-должников: ${s.debtors_count}`)
                : card('#fef2f2', '#dc2626', '🔴', s.debtors_count, 'Должников',
                    `средний долг: ${fmtMoney(s.avg_debt_per_debtor)} ₽`),
            rooms
                ? card('#ecfdf5', '#10b981', '🟢', s.rooms_overpaying_count ?? 0, 'Квартир с переплатой', `сумма: ${fmtMoney(s.total_overpay)} ₽`)
                : card('#ecfdf5', '#10b981', '🟢', s.overpayers_count, 'С переплатами', `сумма: ${fmtMoney(s.total_overpay)} ₽`),
            card('#fff7ed', '#ea580c', '💰', `${fmtMoney(s.total_debt)} ₽`, 'Суммарный долг', `209: ${fmtMoney(s.total_debt_209)} · 205: ${fmtMoney(s.total_debt_205)}`),
            card('#eff6ff', '#2563eb', '📊', s.readings_count, 'Показаний в периоде', ''),
            card('#f9fafb', '#6b7280', '⏱️', lastImp ? lastImp.status : '—', 'Последний импорт', lastHtml),
        ].join('');
    },

    // ==========================================================================
    // Список общежитий в фильтр
    // ==========================================================================
    async loadDormitories() {
        if (!this.dom.filterDorm) return;
        try {
            const dorms = await api.get('/financier/debts/dormitories');
            const prev = this.dom.filterDorm.value;
            this.dom.filterDorm.innerHTML = '<option value="">Все общежития</option>';
            dorms.forEach(d => {
                const opt = document.createElement('option');
                opt.value = d;
                opt.textContent = d;
                this.dom.filterDorm.appendChild(opt);
            });
            if (prev) this.dom.filterDorm.value = prev;
        } catch { /* молча */ }
    },

    // ==========================================================================
    // ИМПОРТ
    // ==========================================================================
    /** Bug T: при выборе файла авто-парсит его и показывает сводку под
     *  input'ом. Проверяет дубликат по SHA256 и предупреждает если файл
     *  уже импортировали. */
    async previewFile(accountType) {
        const input = accountType === '209' ? this.dom.inputUpload209 : this.dom.inputUpload205;
        const file = input?.files?.[0] || null;
        // Контейнер для preview-сводки: создаём рядом с input если ещё нет.
        const previewId = `debtPreview${accountType}`;
        let preview = document.getElementById(previewId);
        if (!preview && input) {
            preview = document.createElement('div');
            preview.id = previewId;
            preview.style.cssText = 'margin-top:6px; padding:6px 9px; border-radius:4px; font-size:10.5px; line-height:1.35; max-height:80px; overflow:hidden;';
            // Вставляем плашку ПОСЛЕ контейнера input'а, не внутрь — иначе
            // она «наезжает» на input при длинном hash/sample_fio.
            const wrapper = input.closest('.upload-row') || input.parentElement;
            wrapper?.parentElement?.insertBefore(preview, wrapper.nextSibling) || wrapper?.appendChild(preview);
        }
        if (!file) {
            if (preview) preview.innerHTML = '';
            if (accountType === '209') this._lastPreview209 = null;
            else this._lastPreview205 = null;
            return;
        }
        if (preview) preview.innerHTML = '<span style="color:var(--text-secondary);"><i class="fa-solid fa-spinner fa-spin"></i> Анализируем файл…</span>';

        const fd = new FormData();
        fd.append('account_type', accountType);
        fd.append('file', file);
        try {
            const res = await api.post('/financier/debts/preview-file', fd);
            if (accountType === '209') this._lastPreview209 = res;
            else this._lastPreview205 = res;

            const sizeKb = (res.size_bytes / 1024).toFixed(1);
            let bg, color, statusLine;
            if (res.duplicate_of) {
                bg = '#fef3c7'; color = '#92400e';
                const d = res.duplicate_of;
                const date = d.started_at?.split('T')[0] || '—';
                statusLine = `⚠ <b>Дубликат</b> №${d.log_id} (${date}, ${d.status})`;
            } else if (res.rows_with_fio === 0) {
                bg = '#fee2e2'; color = '#991b1b';
                statusLine = '❌ <b>ФИО не найдено</b> — не ОСВ 1С?';
            } else {
                bg = '#dcfce7'; color = '#166534';
                statusLine = `✓ <b>${res.rows_with_fio}</b> строк с ФИО`;
            }
            const sampleText = res.sample_fio?.length
                ? ` · ${res.sample_fio.slice(0, 2).map(s => esc(s.length > 24 ? s.slice(0, 22) + '…' : s)).join(', ')}`
                : '';

            if (preview) {
                preview.style.background = bg;
                preview.style.color = color;
                preview.innerHTML = `
                    ${statusLine}
                    <span style="color:var(--text-secondary); margin-left:6px;">${esc(res.file_name.length > 28 ? res.file_name.slice(0, 26) + '…' : res.file_name)} · ${sizeKb}KB${sampleText}</span>
                `;
            }
        } catch (e) {
            if (preview) {
                preview.style.background = '#fee2e2';
                preview.style.color = '#991b1b';
                preview.innerHTML = `<b>Ошибка анализа:</b> ${esc(e.message)}`;
            }
        }
    },

    // Список периодов для выбора «за какой месяц грузим». По умолчанию активный.
    async loadDebtPeriods() {
        const sel = this.dom.periodSelect;
        if (!sel) return;
        try {
            const periods = await api.get('/admin/periods/history');
            const prev = sel.value;
            sel.innerHTML = '';
            (periods || []).forEach(p => {
                const opt = document.createElement('option');
                opt.value = String(p.id);
                opt.textContent = p.name + (p.is_active ? ' (активный)' : '');
                sel.appendChild(opt);
            });
            const active = (periods || []).find(p => p.is_active);
            sel.value = prev
                || (active ? String(active.id) : (periods && periods[0] ? String(periods[0].id) : ''));
        } catch {
            sel.innerHTML = '<option value="">— активный период —</option>';
        }
    },

    async handleUpload() {
        if (this.state.isUploading) return toast('Импорт уже выполняется', 'info');

        const file209 = this.dom.inputUpload209?.files[0] || null;
        const file205 = this.dom.inputUpload205?.files[0] || null;
        const periodId = this.dom.periodSelect?.value || '';
        const periodLabel = this.dom.periodSelect?.selectedOptions?.[0]?.textContent || 'активный период';
        // Legacy: если только старая разметка (#debtFile1C + radio) — старая логика.
        if (!file209 && !file205) {
            const legacyFile = this.dom.inputUpload?.files[0];
            if (legacyFile) {
                return this._handleLegacyUpload(legacyFile);
            }
            return toast('Выберите хотя бы один файл .xlsx', 'error');
        }

        // Bug T: подсветить дубликаты в confirm-диалоге.
        const dupNotes = [];
        if (file209 && this._lastPreview209?.duplicate_of) {
            const d = this._lastPreview209.duplicate_of;
            dupNotes.push(`⚠ 209-файл уже импортирован: №${d.log_id} (${d.started_at?.split('T')[0] || '—'}, status=${d.status})`);
        }
        if (file205 && this._lastPreview205?.duplicate_of) {
            const d = this._lastPreview205.duplicate_of;
            dupNotes.push(`⚠ 205-файл уже импортирован: №${d.log_id} (${d.started_at?.split('T')[0] || '—'}, status=${d.status})`);
        }

        const summary = [
            `Период: ${periodLabel}`,
            file209 ? `209: ${file209.name}${this._lastPreview209 ? ` · ФИО найдено: ${this._lastPreview209.rows_with_fio}` : ''}` : null,
            file205 ? `205: ${file205.name}${this._lastPreview205 ? ` · ФИО найдено: ${this._lastPreview205.rows_with_fio}` : ''}` : null,
            ...dupNotes,
        ].filter(Boolean).join('\n');
        const confirmMsg = dupNotes.length
            ? `Загрузить файлы?\n${summary}\n\nЭти файлы уже загружались. Точно повторить?`
            : `Загрузить файлы?\n${summary}`;
        if (!await showConfirm(confirmMsg, { title: 'Загрузка файлов', confirmText: 'Загрузить' })) return;

        this.state.isUploading = true;
        if (this.dom.uploadResult) {
            this.dom.uploadResult.style.display = 'none';
            this.dom.uploadResult.innerHTML = '';
        }
        setLoading(this.dom.btnUpload, true, 'Загрузка...');

        const formData = new FormData();
        if (file209) formData.append('file_209', file209);
        if (file205) formData.append('file_205', file205);
        if (periodId) formData.append('period_id', periodId);

        try {
            const res = await api.post('/financier/import-debts-pair', formData);
            // Очищаем inputs чтобы случайно не нажать «загрузить» ещё раз.
            if (this.dom.inputUpload209) this.dom.inputUpload209.value = '';
            if (this.dom.inputUpload205) this.dom.inputUpload205.value = '';
            // Чистим preview-блоки.
            document.getElementById('debtPreview209')?.remove();
            document.getElementById('debtPreview205')?.remove();
            this._lastPreview209 = null;
            this._lastPreview205 = null;

            toast(`Файлы приняты (${res.tasks?.length || 0}). Обработка…`, 'info');
            // Polling по последнему таску — обычно у нас 1-2 и они идут
            // параллельно, общая длительность определяется самым медленным.
            // Для простоты UI ждём один из тасков; loadImportHistory всё равно
            // покажет обе записи в любом случае.
            const lastTask = res.tasks?.[res.tasks.length - 1];
            if (lastTask?.task_id) {
                this.pollTask(lastTask.task_id);
            } else {
                this.state.isUploading = false;
                setLoading(this.dom.btnUpload, false, '⬆ Загрузить выбранные');
                this.loadImportHistory();
            }
        } catch (e) {
            toast(`Ошибка: ${e.message}`, 'error');
            this.state.isUploading = false;
            setLoading(this.dom.btnUpload, false, '⬆ Загрузить выбранные');
        }
    },

    async _handleLegacyUpload(file) {
        // Старая разметка (только #debtFile1C + radio) — отдельный код-пас
        // для обратной совместимости. После полного удаления tab_debts.html
        // v1 этот метод можно убрать.
        const radio = document.querySelector('input[name="accountType"]:checked');
        const accountType = radio?.value || '209';
        if (!await showConfirm(`Загрузить долги для счёта ${accountType}?`, { title: 'Загрузка', confirmText: 'Загрузить' })) return;

        this.state.isUploading = true;
        if (this.dom.uploadResult) {
            this.dom.uploadResult.style.display = 'none';
            this.dom.uploadResult.innerHTML = '';
        }
        setLoading(this.dom.btnUpload, true, 'Загрузка...');

        const formData = new FormData();
        formData.append('file', file);
        formData.append('account_type', accountType);
        const legacyPeriod = this.dom.periodSelect?.value || '';
        if (legacyPeriod) formData.append('period_id', legacyPeriod);

        try {
            const res = await api.post('/financier/import-debts', formData);
            this.dom.inputUpload.value = '';
            toast(`Файл принят (Счёт ${accountType}). Обработка…`, 'info');
            this.pollTask(res.task_id);
        } catch (e) {
            toast(`Ошибка: ${e.message}`, 'error');
            this.state.isUploading = false;
            setLoading(this.dom.btnUpload, false, '⬆ Загрузить выбранные');
        }
    },

    async pollTask(taskId) {
        this.clearPoll();
        this.state.currentPollId = taskId;
        let attempts = 0;
        const maxAttempts = 150;

        const check = async () => {
            if (this.state.currentPollId !== taskId) return;
            attempts++;
            if (attempts > maxAttempts) {
                toast('Превышено время ожидания сервера.', 'warning');
                this.state.isUploading = false;
                setLoading(this.dom.btnUpload, false, '⬆ Загрузить выбранные');
                return;
            }
            try {
                const res = await api.get(`/admin/tasks/${taskId}`);
                if (this.state.currentPollId !== taskId) return;

                // Celery task проходит состояния:
                //   PENDING (в очереди) → STARTED (worker взял) → RETRY (autoretry)
                //   → SUCCESS | FAILURE
                // Раньше STARTED/RETRY валились в «Неизвестный статус» —
                // считаем их как «продолжаем polling».
                const inProgress = ['PENDING', 'STARTED', 'RETRY', 'RECEIVED'];
                if (inProgress.includes(res.state) || res.status === 'processing') {
                    this.state.pollTimer = setTimeout(check, 2000);
                    return;
                }
                if (res.status === 'done' || res.state === 'SUCCESS') {
                    this.renderUploadResult(res.result || res);
                    toast('Импорт завершён!', 'success');
                    this.reload();
                    this.loadImportHistory();
                    this.state.isUploading = false;
                    setLoading(this.dom.btnUpload, false, '⬆ Загрузить выбранные');
                    return;
                }
                if (res.state === 'FAILURE' || res.state === 'REVOKED') {
                    throw new Error(res.error || 'Ошибка воркера');
                }
                // На всякий случай: неизвестное состояние — повторяем polling,
                // а не сразу падаем с ошибкой. maxAttempts ограничит сверху.
                this.state.pollTimer = setTimeout(check, 3000);
            } catch (e) {
                if (this.state.currentPollId !== taskId) return;
                toast('Ошибка задачи: ' + e.message, 'error');
                if (this.dom.uploadResult) {
                    this.dom.uploadResult.style.display = 'block';
                    this.dom.uploadResult.innerHTML = '';
                    this.dom.uploadResult.appendChild(
                        el('div', { style: { color: 'red' } }, `Сбой: ${e.message}`)
                    );
                }
                this.state.isUploading = false;
                setLoading(this.dom.btnUpload, false, '⬆ Загрузить выбранные');
            }
        };
        check();
    },

    renderUploadResult(res) {
        if (!this.dom.uploadResult || !res) return;
        this.dom.uploadResult.innerHTML = '';
        this.dom.uploadResult.style.display = 'block';

        const success = el('div', {
            style: { padding: '15px', background: '#e8f5e9', color: '#2e7d32', borderRadius: '6px', border: '1px solid #c8e6c9' }
        },
            el('h4', { style: { margin: '0 0 10px 0' } }, `✅ Импорт завершён (Счёт ${res.account || '?'})`),
            el('ul', { style: { margin: 0, paddingLeft: '20px' } },
                el('li', {}, 'Обработано: ', el('strong', {}, String(res.processed))),
                el('li', {}, 'Обновлено: ', el('strong', {}, String(res.updated))),
                el('li', {}, 'Создано: ', el('strong', {}, String(res.created))),
                res.log_id ? el('li', {}, 'Запись в истории: ', el('strong', {}, `#${res.log_id}`)) : ''
            )
        );
        this.dom.uploadResult.appendChild(success);

        if (res.not_found_users && res.not_found_users.length) {
            const errorBox = el('div', {
                style: { marginTop: '15px', padding: '15px', background: '#ffebee', color: '#c62828', borderRadius: '6px', border: '1px solid #ffcdd2' }
            },
                el('h4', { style: { margin: '0 0 10px 0' } }, `⚠️ Не найдены (${res.not_found_users.length})`)
            );
            const scrollBox = el('div', {
                style: { maxHeight: '100px', overflow: 'auto', fontSize: '13px', background: 'rgba(255,255,255,.5)', padding: '5px' }
            });
            res.not_found_users.forEach(u => scrollBox.appendChild(el('div', {}, String(u))));
            errorBox.appendChild(scrollBox);
            if (res.log_id) {
                errorBox.appendChild(el('button', {
                    class: 'action-btn secondary-btn',
                    style: { marginTop: '10px', fontSize: '12px' },
                    onclick: () => this.openNotFoundModal(res.log_id)
                }, 'Привязать вручную'));
            }
            this.dom.uploadResult.appendChild(errorBox);
        }
    },

    // ==========================================================================
    // ТАБЛИЦА
    // ==========================================================================
    async loadUsers() {
        if (!this.dom.tableBody) return;
        if (this.state.mode === 'rooms') return this.loadRooms();
        const requestId = ++this.state.lastRequestId;
        this.dom.tableBody.innerHTML = '<tr><td colspan="10" style="text-align:center;padding:20px">Загрузка…</td></tr>';

        const params = new URLSearchParams({
            page: this.state.page,
            limit: this.state.limit,
            sort_by: this.state.sortBy,
            sort_dir: this.state.sortDir,
        });
        if (this.state.search) params.set('search', this.state.search);
        if (this.state.filterType === 'debtors') params.set('only_debtors', 'true');
        if (this.state.filterType === 'overpaid') params.set('only_overpaid', 'true');
        if (this.state.dormitory) params.set('dormitory', this.state.dormitory);
        if (this.state.minDebt) params.set('min_debt', this.state.minDebt);
        if (this.state.hideEmpty) params.set('has_data', 'true');
        if (this.state.viewPeriodId) params.set('period_id', this.state.viewPeriodId);

        try {
            const data = await api.get(`/financier/users-status?${params}`);
            if (requestId !== this.state.lastRequestId) return;
            this.state.total = data.total;
            this.renderUsers(data.items);
            this.updatePagination();
        } catch (e) {
            if (requestId !== this.state.lastRequestId) return;
            this.dom.tableBody.innerHTML = '';
            this.dom.tableBody.appendChild(
                el('tr', {}, el('td', { colspan: '10', style: { color: 'red', textAlign: 'center', padding: '20px' } }, e.message))
            );
        }
    },

    // ── РЕЖИМ КВАРТИР: зеркало loadUsers, агрегация по помещению, без ФИО ──
    async loadRooms() {
        const requestId = ++this.state.lastRequestId;
        this.dom.tableBody.innerHTML = '<tr><td colspan="10" style="text-align:center;padding:20px">Загрузка…</td></tr>';
        const sortBy = ['debt', 'overpay', 'total'].includes(this.state.sortBy)
            ? this.state.sortBy : 'room';
        const params = new URLSearchParams({
            page: this.state.page, limit: this.state.limit,
            sort_by: sortBy, sort_dir: this.state.sortDir,
        });
        if (this.state.search) params.set('search', this.state.search);
        if (this.state.filterType === 'debtors') params.set('only_debtors', 'true');
        if (this.state.filterType === 'overpaid') params.set('only_overpaid', 'true');
        if (this.state.dormitory) params.set('dormitory', this.state.dormitory);
        if (this.state.minDebt) params.set('min_debt', this.state.minDebt);
        if (this.state.hideEmpty) params.set('has_data', 'true');
        if (this.state.viewPeriodId) params.set('period_id', this.state.viewPeriodId);
        try {
            const data = await api.get(`/financier/rooms-status?${params}`);
            if (requestId !== this.state.lastRequestId) return;
            this.state.total = data.total;
            this.renderRooms(data.items);
            this.updatePagination();
        } catch (e) {
            if (requestId !== this.state.lastRequestId) return;
            this.dom.tableBody.innerHTML =
                `<tr><td colspan="10" style="color:red;text-align:center;padding:20px">${e.message}</td></tr>`;
        }
    },

    renderRooms(rooms) {
        if (!rooms || !rooms.length) {
            this.dom.tableBody.innerHTML = '<tr><td colspan="10" style="text-align:center;padding:20px; color:var(--text-secondary);">Нет данных для текущих фильтров</td></tr>';
            return;
        }
        const f = (v) => {
            const a = Math.abs(Number(v || 0));
            if (a < 0.005) return '0';
            return (a >= 10000 ? a.toFixed(0) : a.toFixed(2)).replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
        };
        const esc = (s) => String(s ?? '').replace(/[&<>"]/g,
            (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
        this.dom.tableBody.innerHTML = rooms.map((r) => {
            const d209 = Number(r.debt_209 || 0), o209 = Number(r.overpayment_209 || 0);
            const d205 = Number(r.debt_205 || 0), o205 = Number(r.overpayment_205 || 0);
            const totalDebt = d209 + d205, total = Number(r.current_total_cost || 0);
            const bg = totalDebt >= 10000 ? 'background:#fef2f2;'
                : totalDebt >= 1000 ? 'background:#fffbeb;'
                : (o209 + o205) > 0 ? 'background:#f0fdf4;' : '';
            return `<tr style="${bg}">
                <td style="color:var(--text-secondary);">#${r.room_id}</td>
                <td colspan="2"><b>${esc(r.address)}</b> <span style="color:var(--text-secondary);font-size:12px;">· 👤 ${r.residents_count}</span></td>
                <td style="text-align:right; color:#991b1b;">${d209 > 0 ? f(d209) : '—'}</td>
                <td style="text-align:right; color:#15803d;">${o209 > 0 ? f(o209) : '—'}</td>
                <td style="text-align:right; color:#d97706;">${d205 > 0 ? f(d205) : '—'}</td>
                <td style="text-align:right; color:#15803d;">${o205 > 0 ? f(o205) : '—'}</td>
                <td style="text-align:right; font-weight:700;">${totalDebt > 0 ? f(totalDebt) : '—'}</td>
                <td style="text-align:right;">${f(total)}</td>
                <td style="text-align:right;"><button class="icon-btn" data-room-residents="${r.room_id}" title="Кто живёт в квартире"><i class="fa-solid fa-users"></i></button></td>
            </tr>`;
        }).join('');
        this.dom.tableBody.querySelectorAll('[data-room-residents]').forEach((btn) => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                this.showRoomResidents(Number(btn.dataset.roomResidents));
            });
        });
    },

    async showRoomResidents(roomId) {
        const esc = (s) => String(s ?? '').replace(/[&<>"]/g,
            (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
        const f = (v) => Number(v || 0).toFixed(2);
        try {
            const data = await api.get(`/financier/rooms/${roomId}/residents-finance`);
            const rows = (data.residents || []).map((p) => `<tr style="border-bottom:1px solid var(--border-color);">
                <td style="padding:8px 10px;">${esc(p.full_name || p.username)}</td>
                <td style="padding:8px 10px;text-align:right;color:#991b1b;">${f(Number(p.debt_209) + Number(p.debt_205))} ₽</td>
                <td style="padding:8px 10px;text-align:right;color:#15803d;">${f(Number(p.overpayment_209) + Number(p.overpayment_205))} ₽</td>
                <td style="padding:8px 10px;text-align:right;">${f(p.current_total_cost)} ₽</td>
            </tr>`).join('') || '<tr><td colspan="4" style="padding:16px;text-align:center;color:var(--text-secondary);">Нет жильцов</td></tr>';
            const overlay = document.createElement('div');
            overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:3000;display:flex;align-items:center;justify-content:center;padding:20px;';
            overlay.innerHTML = `<div style="background:var(--bg-card,#fff);border-radius:12px;max-width:560px;width:100%;max-height:82vh;overflow:auto;box-shadow:0 20px 60px rgba(0,0,0,0.3);">
                <div style="display:flex;justify-content:space-between;align-items:center;padding:14px 16px;border-bottom:1px solid var(--border-color);"><b>Жильцы квартиры · долги</b><button data-rr-close style="background:none;border:none;font-size:22px;line-height:1;cursor:pointer;">×</button></div>
                <table style="width:100%;border-collapse:collapse;font-size:13px;"><thead><tr style="background:var(--bg-page);font-size:11px;color:var(--text-secondary);text-transform:uppercase;"><th style="text-align:left;padding:8px 10px;">Жилец</th><th style="text-align:right;padding:8px 10px;">Долг</th><th style="text-align:right;padding:8px 10px;">Переплата</th><th style="text-align:right;padding:8px 10px;">Итог</th></tr></thead><tbody>${rows}</tbody></table></div>`;
            overlay.addEventListener('click', (e) => {
                if (e.target === overlay || e.target.closest('[data-rr-close]')) overlay.remove();
            });
            document.body.appendChild(overlay);
        } catch (e) {
            toast('Ошибка: ' + e.message, 'error');
        }
    },

    setMode(mode) {
        if (this.state.mode === mode) return;
        this.state.mode = mode;
        this.state.page = 1;
        const t = document.getElementById('debtsTitle');
        if (t) t.textContent = mode === 'rooms' ? 'Долги по квартирам' : 'Список жильцов и долгов';
        const bu = document.getElementById('debtsModeUsers');
        const br = document.getElementById('debtsModeRooms');
        const sty = 'border-radius:0; padding:5px 12px; font-size:12px;';
        if (bu) { bu.className = 'action-btn ' + (mode === 'users' ? 'primary-btn' : 'secondary-btn'); bu.style.cssText = sty; }
        if (br) { br.className = 'action-btn ' + (mode === 'rooms' ? 'primary-btn' : 'secondary-btn'); br.style.cssText = sty; }
        this.loadUsers();
        this.loadStats();  // KPI-плашка зависит от режима (квартир с долгом / должников)
    },

    updatePagination() {
        if (!this.dom.pageInfo) return;
        const totalPages = Math.ceil(this.state.total / this.state.limit) || 1;
        this.dom.pageInfo.textContent = `Стр. ${this.state.page} из ${totalPages} (Всего: ${this.state.total})`;
        this.dom.btnPrev.disabled = this.state.page <= 1;
        this.dom.btnNext.disabled = this.state.page >= totalPages;
    },

    debtChip(amount) {
        const a = Number(amount || 0);
        if (a <= 0) return '';
        if (a >= 10000) return `<span style="background:#fee2e2; color:#991b1b; padding:1px 6px; border-radius:8px; font-size:10px; font-weight:700; margin-left:4px;">КРИТ.</span>`;
        if (a >= 1000) return `<span style="background:#fef3c7; color:#92400e; padding:1px 6px; border-radius:8px; font-size:10px; font-weight:600; margin-left:4px;">средн.</span>`;
        return '';
    },

    renderUsers(users) {
        this.dom.tableBody.innerHTML = '';
        if (!users || !users.length) {
            this.dom.tableBody.innerHTML = '<tr><td colspan="10" style="text-align:center;padding:20px; color:var(--text-secondary);">Нет данных для текущих фильтров</td></tr>';
            return;
        }

        const fragment = document.createDocumentFragment();
        users.forEach(u => {
            const d209 = parseFloat(u.debt_209 || 0), o209 = parseFloat(u.overpayment_209 || 0);
            const d205 = parseFloat(u.debt_205 || 0), o205 = parseFloat(u.overpayment_205 || 0);
            // Bug V: обороты для индикатора движения.
            const od209 = parseFloat(u.obor_debit_209 || 0), oc209 = parseFloat(u.obor_credit_209 || 0);
            const od205 = parseFloat(u.obor_debit_205 || 0), oc205 = parseFloat(u.obor_credit_205 || 0);
            const totalDebt = d209 + d205;
            const total = parseFloat(u.current_total_cost || 0);

            // Цветовой индикатор строки
            let rowBg = '';
            if (totalDebt >= 10000) rowBg = 'background:#fef2f2;';
            else if (totalDebt >= 1000) rowBg = 'background:#fffbeb;';
            else if ((o209 + o205) > 0) rowBg = 'background:#f0fdf4;';

            // Адрес помещения — единый формат для общаги и дома (E2-A).
            // Раньше склеивали dormitory_name/room_number вручную, из-за чего
            // у домов (оба поля NULL) выводилось «— / —». formatRoomAddress
            // сам ветвится по place_type: дом → «ул. X, д. Y, кв. Z».
            const room = u.room ? formatRoomAddress(u.room) : '—';

            // Bug AH: ячейка сальдо с inline-микроисторией движения средств.
            // Раньше показывали только текущее значение + tooltip — админу
            // приходилось вешать курсор, чтобы понять, что произошло.
            // Теперь под главным числом — компактная строчка вида
            // «был 635 · оплатил 635» с цветовым кодированием.
            //
            // Аргументы:
            //   value  — текущее сальдо в этой колонке (debt или overpay)
            //   oborD  — оборот Дт (доначислили) за период
            //   oborC  — оборот Кр (заплатили) за период
            //   isDebt — true для колонки «Долг», false для «Перепл.»
            //   accColor — цвет основного числа (красный/оранжевый для долгов,
            //              зелёный для переплат)
            //
            // Логика начального сальдо (обратное вычисление):
            //   Если у жильца сейчас долг X и были обороты Дт/Кр —
            //   start_debt = X + oborC - oborD
            //   (заплатил и стало X, значит до этого было X + заплатил − начислили)
            //   Если start_debt < 0 — было не долг, а переплата.
            const saldoCell = (value, oborD, oborC, isDebt, accColor) => {
                const hasValue = value > 0.005;
                const hasObor = oborD > 0.005 || oborC > 0.005;

                // Совсем пусто — без движения и без сальдо.
                if (!hasValue && !hasObor) {
                    return `<span style="color:#ccc;">—</span>`;
                }

                // Helper: компактное форматирование суммы без «₽» и без копеек,
                // если они .00 — экономим место.
                const f = (v) => {
                    const abs = Math.abs(v);
                    if (abs < 0.005) return '0';
                    // Тысячи разделяем тонким пробелом.
                    const fixed = abs >= 10000 ? abs.toFixed(0) : abs.toFixed(2);
                    return fixed.replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
                };

                // Вычисляем «было в начале периода» по обратной формуле.
                // Только для «Долг» — для «Перепл.» это симметрично, но мы
                // отдадим расшифровку колонке Долг (чтобы не дублировать).
                let startDebt = null;
                if (isDebt && hasObor) {
                    // value уже >0 ИЛИ был долг и сейчас 0 — обе ветки покрыты.
                    startDebt = value + oborC - oborD;
                }

                // Случай 1: переплата (isDebt=false), просто показываем число.
                // Движение здесь не описываем — оно в соседней колонке Долг.
                if (!isDebt) {
                    if (!hasValue) return `<span style="color:#ccc;">—</span>`;
                    return `<div style="line-height:1.25;">
                        <div style="font-weight:600; color:${accColor};">${f(value)}</div>
                        <div style="font-size:10.5px; color:#16a34a;">переплата</div>
                    </div>`;
                }

                // ── Колонка «Долг» — раскладываем движение в одну строчку.

                // 1. Долг 0, есть обороты — погашен (или начислили + сразу оплатили)
                if (!hasValue && hasObor) {
                    if (oborC > 0.005 && oborD < 0.005) {
                        // Был долг — оплатили полностью.
                        return `<div style="line-height:1.25;">
                            <div style="font-weight:600; color:#15803d;">0 <span style="font-size:11px;">✓</span></div>
                            <div style="font-size:10.5px; color:#6b7280;">был ${f(startDebt)} · оплатил ${f(oborC)}</div>
                        </div>`;
                    }
                    if (oborD > 0.005 && oborC > 0.005) {
                        // Начислили и оплатили — нулевое сальдо в результате.
                        return `<div style="line-height:1.25;">
                            <div style="font-weight:600; color:#15803d;">0 <span style="font-size:11px;">⊜</span></div>
                            <div style="font-size:10.5px; color:#6b7280;">+${f(oborD)} начисл · −${f(oborC)} оплат</div>
                        </div>`;
                    }
                    // Только начисление 0→0 — экзотика, fallback.
                    return `<div style="line-height:1.25;">
                        <div style="font-weight:600; color:#9ca3af;">0</div>
                        <div style="font-size:10.5px; color:#6b7280;">+${f(oborD)} начислено</div>
                    </div>`;
                }

                // 2. Долг есть + обороты — раскрываем движение.
                if (hasValue && hasObor) {
                    // Долг вырос (доначислили больше, чем оплатили).
                    if (value > (startDebt || 0) + 0.005) {
                        return `<div style="line-height:1.25;">
                            <div style="font-weight:600; color:#b91c1c;">${f(value)}</div>
                            <div style="font-size:10.5px; color:#b91c1c;">был ${f(startDebt)} · +${f(oborD - oborC)} ↑</div>
                        </div>`;
                    }
                    // Долг уменьшился — заплатил часть.
                    if ((startDebt || 0) > value + 0.005) {
                        return `<div style="line-height:1.25;">
                            <div style="font-weight:600; color:#a16207;">${f(value)}</div>
                            <div style="font-size:10.5px; color:#a16207;">был ${f(startDebt)} · оплатил ${f(oborC)}</div>
                        </div>`;
                    }
                    // Без изменения, но обороты были (начислили = оплатил).
                    return `<div style="line-height:1.25;">
                        <div style="font-weight:600; color:${accColor};">${f(value)}</div>
                        <div style="font-size:10.5px; color:#6b7280;">+${f(oborD)} · −${f(oborC)}</div>
                    </div>`;
                }

                // 3. Долг есть, оборотов нет — статичный долг (висит).
                return `<div style="line-height:1.25;">
                    <div style="font-weight:600; color:${accColor};">${f(value)}</div>
                    <div style="font-size:10.5px; color:#9ca3af;">без движения</div>
                </div>`;
            };

            const tr = el('tr', { class: 'table-row', style: { cssText: rowBg } },
                el('td', {}, String(u.id)),
                // Bug AI: ФИО кликабельно — открывает модалку «карточка жильца»
                // с разбором было/оплатил/осталось по каждому счёту + история.
                el('td', {
                    style: { fontWeight: '600', cursor: 'pointer', color: '#4338ca' },
                    title: 'Открыть карточку жильца с раскладкой долга',
                    onclick: () => this.openUserCard(u),
                }, u.username),
                el('td', { style: { fontSize: '12px' } }, room),
            );

            // «Не найден в счёте»: ФИО жильца не было в последнем импорте этого
            // счёта за период (seen_2xx === false). Отличаем от «долг 0».
            const notFoundCell = (acct) =>
                `<span style="color:#b45309; font-size:11px; font-style:italic;" title="ФИО не найдено в последнем импорте счёта ${acct} за этот период — данных по счёту нет (это не «долг 0»)">не найден</span>`;

            // Долг/Перепл 209 с движением
            const d209Td = el('td', { style: { borderLeft: '2px solid #eee' } });
            d209Td.innerHTML = (u.seen_209 === false && d209 < 0.005)
                ? notFoundCell('209')
                : saldoCell(d209, od209, oc209, true, '#c0392b');
            tr.appendChild(d209Td);

            const o209Td = el('td', {});
            o209Td.innerHTML = saldoCell(o209, od209, oc209, false, '#27ae60');
            tr.appendChild(o209Td);

            // Долг/Перепл 205 с движением
            const d205Td = el('td', { style: { borderLeft: '2px solid #eee' } });
            d205Td.innerHTML = (u.seen_205 === false && d205 < 0.005)
                ? notFoundCell('205')
                : saldoCell(d205, od205, oc205, true, '#d35400');
            tr.appendChild(d205Td);

            const o205Td = el('td', {});
            o205Td.innerHTML = saldoCell(o205, od205, oc205, false, '#27ae60');
            tr.appendChild(o205Td);

            // Суммарный долг + чип
            const sumCell = el('td', { style: { fontWeight: '700', color: totalDebt > 0 ? '#b91c1c' : 'var(--text-secondary)' } });
            sumCell.innerHTML = totalDebt > 0 ? `${fmtMoney(totalDebt)}${this.debtChip(totalDebt)}` : '—';
            tr.appendChild(sumCell);

            tr.appendChild(el('td', { style: { fontWeight: 'bold' } }, total !== 0 ? fmtMoney(total) : '—'));
            // Группа кнопок: «История» — модалка sparkline через все импорты;
            // «Корр.» — ручная корректировка сальдо. Раньше была только Корр.
            const actionsCell = el('td', { style: { textAlign: 'right', whiteSpace: 'nowrap' } });
            actionsCell.appendChild(el('button', {
                class: 'action-btn', style: { padding: '4px 8px', fontSize: '12px', background: '#fff', color: '#4338ca', border: '1px solid #c7d2fe', marginRight: '4px' },
                title: 'История долгов через все импорты 1С',
                onclick: () => this.openUserDebtHistory(u.id, u.username),
            }, '📊'));
            // 🔍 — поиск ФИО в архивах последних импортов 1С. Use case:
            // у жильца «—» в обоих счетах, а в Excel он должен быть. Эта
            // кнопка показывает где он есть/нет в архивах + значения.
            actionsCell.appendChild(el('button', {
                class: 'action-btn', style: { padding: '4px 8px', fontSize: '12px', background: '#fff', color: '#0ea5e9', border: '1px solid #bae6fd', marginRight: '4px' },
                title: 'Найти ФИО в архивах последних импортов 1С (диагностика «почему нет долга»)',
                onclick: () => this.openCheckCoverage(u.id, u.username),
            }, '🔍'));
            // Кнопка «Сбросить баланс» — обнуляет debt/overpay у всех reading
            // жильца. Полезно когда после отката импорта у жильца остались
            // зависшие сальдо в других периодах.
            actionsCell.appendChild(el('button', {
                class: 'action-btn', style: { padding: '4px 8px', fontSize: '12px', background: '#fff', color: '#b91c1c', border: '1px solid #fecaca', marginRight: '4px' },
                title: 'Сбросить баланс жильца — обнулить debt/overpay во ВСЕХ reading-ах. Использовать когда после отката импорта остались зависшие сальдо.',
                onclick: () => this.resetUserBalance(u.id, u.username),
            }, '🧹'));
            actionsCell.appendChild(el('button', {
                class: 'action-btn', style: { padding: '4px 10px', fontSize: '12px', background: '#6366f1', color: '#fff' },
                onclick: () => this.openAdjustModal(u.id, u.username),
            }, 'Корр.'));
            tr.appendChild(actionsCell);
            fragment.appendChild(tr);
        });
        this.dom.tableBody.appendChild(fragment);
    },

    // ==========================================================================
    // МОДАЛКА КОРРЕКТИРОВКИ (замена prompt-цепочки)
    // ==========================================================================
    openAdjustModal(userId, username) {
        if (!this.dom.adjustModal) return;
        this.dom.adjustForm.reset();
        this.dom.adjustUserId.value = String(userId);
        this.dom.adjustUserName.textContent = username;
        this.dom.adjustAccount.value = '209';
        this.dom.adjustModal.classList.add('open');
        setTimeout(() => this.dom.adjustAmount?.focus(), 50);
    },

    closeAdjustModal() {
        this.dom.adjustModal?.classList.remove('open');
    },

    async submitAdjust(e) {
        e.preventDefault();
        const payload = {
            user_id: Number(this.dom.adjustUserId.value),
            amount: parseFloat(this.dom.adjustAmount.value),
            description: (this.dom.adjustDescription.value || '').trim(),
            account_type: this.dom.adjustAccount.value,
        };
        if (isNaN(payload.amount)) return toast('Введите число', 'error');
        if (!payload.description) return toast('Укажите причину', 'error');

        const btn = this.dom.adjustForm.querySelector('button[type="submit"]');
        setLoading(btn, true, 'Сохранение...');
        try {
            await api.post('/admin/adjustments', payload);
            toast('Корректировка сохранена', 'success');
            this.closeAdjustModal();
            this.reload();
        } catch (err) {
            toast(err.message, 'error');
        } finally {
            setLoading(btn, false, '<i class="fa-solid fa-floppy-disk"></i> Сохранить корректировку');
        }
    },

    // ==========================================================================
    // ЭКСПОРТ
    // ==========================================================================
    async exportExcel() {
        const params = new URLSearchParams();
        if (this.state.search) params.set('search', this.state.search);
        if (this.state.filterType === 'debtors') params.set('only_debtors', 'true');
        if (this.state.filterType === 'overpaid') params.set('only_overpaid', 'true');
        if (this.state.dormitory) params.set('dormitory', this.state.dormitory);
        if (this.state.minDebt) params.set('min_debt', this.state.minDebt);
        setLoading(this.dom.btnExport, true);
        try {
            await api.download(`/financier/debts/export?${params}`, `debts_${Date.now()}.xlsx`);
            toast('Экспорт готов', 'success');
        } catch (e) {
            toast('Ошибка экспорта: ' + e.message, 'error');
        } finally {
            setLoading(this.dom.btnExport, false);
        }
    },

    // ==========================================================================
    // ИСТОРИЯ ИМПОРТОВ
    // ==========================================================================
    async loadImportHistory() {
        if (!this.dom.importHistoryList) return;
        this.dom.importHistoryList.innerHTML = '<div style="padding:20px; text-align:center; color:var(--text-secondary);"><i class="fa-solid fa-spinner fa-spin"></i> Загрузка…</div>';
        try {
            const logs = await api.get('/financier/debts/import-history?limit=20');
            if (!logs.length) {
                this.dom.importHistoryList.innerHTML = '<div style="padding:20px; text-align:center; color:var(--text-secondary); font-size:13px;">Ещё не было импортов.</div>';
                return;
            }
            // Шапка с кнопкой массовой чистки + список.
            const reverted = logs.filter(l => l.status === 'reverted' || l.status === 'failed').length;
            const headerBar = `
                <div style="display:flex; justify-content:space-between; align-items:center; padding:8px 12px; background:var(--bg-page); border-bottom:1px solid var(--border-color); font-size:12px;">
                    <span style="color:var(--text-secondary);">
                        Записей: <b>${logs.length}</b>${reverted > 0 ? ` · откаченных: <b style="color:#dc2626;">${reverted}</b>` : ''}
                    </span>
                    <button class="action-btn" data-history-action="cleanup"
                            style="padding:4px 10px; font-size:11px; background:#fef3c7; color:#92400e; border:1px solid #fde68a;"
                            title="Удалить откаченные и устаревшие записи истории (оставить 5 последних completed на каждый счёт)">
                        <i class="fa-solid fa-broom"></i> Очистить устаревшие
                    </button>
                </div>`;
            this.dom.importHistoryList.innerHTML = headerBar + logs.map(log => this.renderHistoryRow(log)).join('');
        } catch (e) {
            this.dom.importHistoryList.innerHTML = `<div style="padding:16px; color:var(--danger-color);">Ошибка: ${esc(e.message)}</div>`;
        }
    },

    renderHistoryRow(log) {
        const statusColors = {
            pending:   ['#3b82f6', '#dbeafe', 'В процессе'],
            completed: ['#059669', '#d1fae5', 'Готово'],
            failed:    ['#dc2626', '#fee2e2', 'Ошибка'],
            reverted:  ['#6b7280', '#f3f4f6', 'Откачен'],
        };
        const [fg, bg, label] = statusColors[log.status] || ['#6b7280', '#f3f4f6', log.status];
        const canUndo = log.status === 'completed';
        const hasNotFound = log.not_found_count > 0;

        return `
            <div style="padding:10px 12px; border-bottom:1px solid var(--border-color); display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
                <span style="background:${bg}; color:${fg}; padding:2px 8px; border-radius:10px; font-size:11px; font-weight:600; white-space:nowrap;">${esc(label)}</span>
                <span style="font-weight:600; color:#1f2937; font-size:13px; white-space:nowrap;">№${log.id} · ${esc(log.account_type)}</span>
                <span style="color:var(--text-secondary); font-size:12px; flex:1; min-width:140px;">${esc(fmtDateTime(log.started_at))} · ${esc(log.started_by || '—')}</span>
                <span style="font-size:12px; color:var(--text-secondary); white-space:nowrap;" title="Обработано / Обновлено / Создано">
                    📊 ${log.processed} / ✎ ${log.updated} / +${log.created}
                </span>
                ${hasNotFound ? `
                    <button class="action-btn secondary-btn" data-history-action="view-not-found" data-log-id="${log.id}"
                            style="padding:3px 8px; font-size:11px; background:#fef3c7; color:#92400e; border-color:#fde68a; white-space:nowrap;">
                        ⚠ ${log.not_found_count}
                    </button>` : ''}
                ${log.has_archive ? `
                    <a href="/api/financier/debts/import-history/${log.id}/download"
                       class="action-btn secondary-btn" download
                       title="Скачать оригинальный xlsx из 1С"
                       style="padding:3px 8px; font-size:11px; white-space:nowrap; text-decoration:none;">
                        <i class="fa-solid fa-download"></i>
                    </a>` : ''}
                ${log.status === 'completed' ? `
                    <button class="action-btn secondary-btn" data-history-action="diff" data-log-id="${log.id}"
                            title="Сравнить с предыдущим импортом того же счёта"
                            style="padding:3px 8px; font-size:11px; white-space:nowrap; background:#eef2ff; color:#4338ca; border-color:#c7d2fe;">
                        <i class="fa-solid fa-code-compare"></i> Diff
                    </button>
                    <button class="action-btn secondary-btn" data-history-action="diagnose" data-log-id="${log.id}"
                            title="Диагностика парсера: какие колонки нашёл, какие значения извлёк (для отладки «почему долг неправильный»)"
                            style="padding:3px 8px; font-size:11px; white-space:nowrap; background:#fffbeb; color:#92400e; border-color:#fde68a;">
                        <i class="fa-solid fa-microscope"></i>
                    </button>
                    ${log.has_archive ? `
                        <button class="action-btn secondary-btn" data-history-action="reparse" data-log-id="${log.id}"
                                title="Bug AE: переимпорт того же файла с актуальной логикой парсера. Полезно если reading'и созданы старой версией (debt берёт начальное сальдо вместо погашенного оборотами)."
                                style="padding:3px 8px; font-size:11px; white-space:nowrap; background:#ecfdf5; color:#065f46; border-color:#a7f3d0;">
                            <i class="fa-solid fa-arrows-rotate"></i> Переимпорт
                        </button>` : ''}` : ''}
                ${canUndo ? `
                    <button class="action-btn danger-btn" data-history-action="undo" data-log-id="${log.id}"
                            style="padding:3px 8px; font-size:11px; white-space:nowrap;">
                        <i class="fa-solid fa-rotate-left"></i> Откатить
                    </button>` : ''}
                <button class="action-btn" data-history-action="delete" data-log-id="${log.id}"
                        style="padding:3px 8px; font-size:11px; background:#f3f4f6; color:#6b7280; border:1px solid #d1d5db; white-space:nowrap;"
                        title="Удалить запись истории (без отката, если данные уже неактуальны)">
                    <i class="fa-regular fa-trash-can"></i>
                </button>
                ${log.error ? `<div style="width:100%; font-size:11px; color:#b91c1c; margin-top:4px;">${esc(log.error)}</div>` : ''}
            </div>
        `;
    },

    async undoImport(logId) {
        if (!await showConfirm(`Откатить импорт №${logId}?\nБудут восстановлены долги/переплаты, которые были ДО этого импорта, и удалены созданные им черновики. Действие необратимо.`, { title: 'Откат импорта', confirmText: 'Откатить', danger: true })) return;
        try {
            const res = await api.post(`/financier/debts/import-history/${logId}/undo`);
            toast(`Откачено: восстановлено ${res.restored_readings}, удалено ${res.removed_drafts}`, 'success');
            this.reload();
            this.loadImportHistory();
        } catch (e) {
            toast('Ошибка отката: ' + e.message, 'error');
        }
    },

    /** Bug AE: Переимпорт лога 1С из архива.
     *  Reading'и созданные старой версией парсера (до Bug U-fix6) могут иметь
     *  debt = начальное сальдо вместо погашенного оборотами. Этот endpoint
     *  берёт archive_path и запускает import_debts_task — pipeline UPDATE-ит
     *  существующие reading'и значениями из актуальной логики. */
    async reparseImport(logId) {
        if (!await showConfirm(
            `Переимпортировать импорт №${logId} из архива?\n\n` +
            `• Файл из 1С возьмётся из архивного хранилища\n` +
            `• Парсер применит актуальную логику (с учётом оборотов Дт/Кр)\n` +
            `• Долги жильцов обновятся, погашенные оборотами обнулятся\n` +
            `• Создастся новый лог импорта (старый останется для аудита)\n\n` +
            `Полезно если у жильцов в «Долги 1С» видны старые цифры (Муравьев Павел: 635,92 ₽ долг, хотя по ОСВ — погашено).`,
            { title: 'Переимпорт', confirmText: 'Переимпортировать' }
        )) return;
        try {
            const res = await api.post(`/financier/debts/import-history/${logId}/reparse`);
            toast(
                `Переимпорт запущен (task=${res.task_id?.slice?.(0, 8) || '—'}), счёт ${res.account_type}. ` +
                `Обнови историю через ~10-15 сек.`,
                'success'
            );
            // Через 12 секунд автоматически перезагружаем историю и таблицу
            setTimeout(() => {
                this.loadImportHistory();
                this.reload();
            }, 12000);
        } catch (e) {
            toast('Ошибка переимпорта: ' + (e.message || 'неизвестно'), 'error');
        }
    },

    /** Удаление одной записи истории импорта БЕЗ отката данных.
     *  Use case: импорт устарел (после rebuild/reload-period долги в БД
     *  обновлены другим импортом), запись «висит» с устаревшими цифрами. */
    /** Диагностика парсера: какие колонки нашёл, какие значения извлёк
     *  для sample-жильцов. Помогает понять «почему у Бендаса всё ещё
     *  2385.07» без захода на сервер за логами. */
    /** Этап 2: модалка-анализатор целостности долгов.
     *  Сравнивает applied_state свежих 209/205-импортов с readings БД,
     *  показывает три категории: drift / missing / extra. */
    async openIntegrityModal() {
        const overlay = document.createElement('div');
        overlay.style.cssText = 'position:fixed; inset:0; background:rgba(0,0,0,0.45); z-index:9999; display:flex; align-items:center; justify-content:center; padding:20px;';
        overlay.innerHTML = `
            <div style="background:#fff; border-radius:8px; width:min(1000px, 100%); max-height:90vh; display:flex; flex-direction:column;">
                <div style="display:flex; justify-content:space-between; align-items:center; padding:14px 18px; border-bottom:1px solid var(--border-color);">
                    <h3 style="margin:0; font-size:15px;">
                        🩺 Целостность долгов: applied_state ↔ БД
                    </h3>
                    <button data-close-integrity style="background:none; border:none; font-size:20px; color:#6b7280; cursor:pointer;">×</button>
                </div>
                <div id="integrityContent" style="padding:14px 18px; overflow-y:auto; flex:1;">
                    <p style="color:var(--text-secondary);"><i class="fa-solid fa-spinner fa-spin"></i> Анализ...</p>
                </div>
            </div>
        `;
        document.body.appendChild(overlay);
        const close = () => overlay.remove();
        overlay.querySelector('[data-close-integrity]').addEventListener('click', close);
        overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });

        try {
            const data = await api.get('/financier/debts/integrity-check');
            this._renderIntegrityContent(overlay, data);
        } catch (e) {
            const c = overlay.querySelector('#integrityContent');
            if (c) c.innerHTML = `<p style="color:var(--danger-color);">Ошибка: ${esc(e.message)}</p>`;
        }
    },

    _renderIntegrityContent(overlay, data) {
        const cont = overlay.querySelector('#integrityContent');
        if (!cont) return;
        const s = data.summary || {};
        const f = (v) => Number(v || 0).toFixed(2);

        const allClean = s.drift_count === 0 && s.missing_in_db_count === 0 && s.extra_in_db_count === 0;

        const driftHtml = !s.drift_count ? '' : `
            <div style="display:flex; justify-content:space-between; align-items:center; margin:18px 0 6px;">
                <h4 style="margin:0; color:#a16207;">⚠ Drift (${s.drift_count}) — долг в БД не совпадает со свежим импортом</h4>
                <button data-fix-category="drift"
                        style="padding:6px 12px; background:#a16207; color:#fff; border:none; border-radius:4px; cursor:pointer; font-size:12px; font-weight:500;">
                    <i class="fa-solid fa-wand-magic-sparkles"></i> Исправить все ${s.drift_count}
                </button>
            </div>
            <p style="font-size:11px; color:#6b7280; margin:0 0 6px;">
                Записать «Ожидается» в БД (UPDATE по reading.id). Альтернатива — нажать ↻ Переимпорт на соответствующем логе.
            </p>
            <table style="width:100%; border-collapse:collapse; font-size:11.5px;">
                <thead style="background:#fffbeb;">
                    <tr>
                        <th style="padding:5px 7px; text-align:left;">ФИО</th>
                        <th style="padding:5px 7px; text-align:left;">Комната</th>
                        <th style="padding:5px 7px; text-align:right;">Ожидается 209</th>
                        <th style="padding:5px 7px; text-align:right;">В БД 209</th>
                        <th style="padding:5px 7px; text-align:right;">Ожидается 205</th>
                        <th style="padding:5px 7px; text-align:right;">В БД 205</th>
                        <th style="padding:5px 7px; text-align:right;">Δ</th>
                        <th style="padding:5px 7px;"></th>
                    </tr>
                </thead>
                <tbody>
                ${data.drift.map(d => `
                    <tr>
                        <td style="padding:4px 7px;">${esc(d.username || '—')}</td>
                        <td style="padding:4px 7px; color:#6b7280;">${esc(d.room_label || '—')}</td>
                        <td style="padding:4px 7px; text-align:right;">${f(d.expected.debt_209)}</td>
                        <td style="padding:4px 7px; text-align:right; color:#b91c1c;">${f(d.actual.debt_209)}</td>
                        <td style="padding:4px 7px; text-align:right;">${f(d.expected.debt_205)}</td>
                        <td style="padding:4px 7px; text-align:right; color:#b91c1c;">${f(d.actual.debt_205)}</td>
                        <td style="padding:4px 7px; text-align:right; font-weight:600;">${f(d.max_abs_diff)}</td>
                        <td style="padding:4px 7px;">
                            <button data-fix-user="${d.user_id}" title="Исправить только этого"
                                    style="padding:3px 7px; background:#fff; color:#a16207; border:1px solid #fde68a; border-radius:3px; cursor:pointer; font-size:11px;">
                                🛠
                            </button>
                        </td>
                    </tr>
                `).join('')}
                </tbody>
            </table>`;

        const missingHtml = !s.missing_in_db_count ? '' : `
            <div style="display:flex; justify-content:space-between; align-items:center; margin:18px 0 6px;">
                <h4 style="margin:0; color:#dc2626;">❗ Missing (${s.missing_in_db_count}) — в файле есть, в БД нет reading</h4>
                <button data-fix-category="missing"
                        style="padding:6px 12px; background:#dc2626; color:#fff; border:none; border-radius:4px; cursor:pointer; font-size:12px; font-weight:500;">
                    <i class="fa-solid fa-plus"></i> Создать все ${s.missing_in_db_count}
                </button>
            </div>
            <p style="font-size:11px; color:#6b7280; margin:0 0 6px;">
                INSERT недостающих reading'ов с ожидаемыми значениями из applied_state.
            </p>
            <table style="width:100%; border-collapse:collapse; font-size:11.5px;">
                <thead style="background:#fef2f2;">
                    <tr>
                        <th style="padding:5px 7px; text-align:left;">ФИО</th>
                        <th style="padding:5px 7px; text-align:left;">Комната</th>
                        <th style="padding:5px 7px; text-align:right;">Ожидается 209</th>
                        <th style="padding:5px 7px; text-align:right;">Ожидается 205</th>
                        <th style="padding:5px 7px;"></th>
                    </tr>
                </thead>
                <tbody>
                ${data.missing_in_db.map(m => `
                    <tr>
                        <td style="padding:4px 7px;">${esc(m.username || '—')}</td>
                        <td style="padding:4px 7px; color:#6b7280;">${esc(m.room_label || '—')}</td>
                        <td style="padding:4px 7px; text-align:right;">${f(m.expected.debt_209)}</td>
                        <td style="padding:4px 7px; text-align:right;">${f(m.expected.debt_205)}</td>
                        <td style="padding:4px 7px;">
                            <button data-fix-user="${m.user_id}" title="Создать reading этому"
                                    style="padding:3px 7px; background:#fff; color:#dc2626; border:1px solid #fecaca; border-radius:3px; cursor:pointer; font-size:11px;">
                                🛠
                            </button>
                        </td>
                    </tr>
                `).join('')}
                </tbody>
            </table>`;

        const extraHtml = !s.extra_in_db_count ? '' : `
            <div style="display:flex; justify-content:space-between; align-items:center; margin:18px 0 6px;">
                <h4 style="margin:0; color:#7c3aed;">👻 Extra/Zombie (${s.extra_in_db_count}) — в БД долг есть, в файле жильца нет</h4>
                <button data-zombie-from-integrity
                        style="padding:6px 12px; background:#7c3aed; color:#fff; border:none; border-radius:4px; cursor:pointer; font-size:12px; font-weight:500;">
                    <i class="fa-solid fa-broom"></i> Открыть Zombie-cleanup
                </button>
            </div>
            <p style="font-size:11px; color:#6b7280; margin:0 0 6px;">
                Зануляется через кнопку 👻 в шапке таблицы (отдельная модалка с подтверждением).
            </p>
            <table style="width:100%; border-collapse:collapse; font-size:11.5px;">
                <thead style="background:#f5f3ff;">
                    <tr>
                        <th style="padding:5px 7px; text-align:left;">ФИО</th>
                        <th style="padding:5px 7px; text-align:left;">Комната</th>
                        <th style="padding:5px 7px; text-align:right;">Долг 209</th>
                        <th style="padding:5px 7px; text-align:right;">Долг 205</th>
                    </tr>
                </thead>
                <tbody>
                ${data.extra_in_db.map(z => `
                    <tr>
                        <td style="padding:4px 7px;">${esc(z.username || '—')}</td>
                        <td style="padding:4px 7px; color:#6b7280;">${esc(z.room_label || '—')}</td>
                        <td style="padding:4px 7px; text-align:right; color:#b91c1c;">${f(z.actual.debt_209)}</td>
                        <td style="padding:4px 7px; text-align:right; color:#b91c1c;">${f(z.actual.debt_205)}</td>
                    </tr>
                `).join('')}
                </tbody>
            </table>`;

        const fixAllBtn = (s.drift_count + s.missing_in_db_count) === 0 ? '' : `
            <div style="margin-bottom:14px; padding:12px 14px; background:#eff6ff; border-left:3px solid #2563eb; border-radius:4px; display:flex; justify-content:space-between; align-items:center;">
                <div style="font-size:13px;">
                    <b>Найдено расхождений:</b> Drift=${s.drift_count}, Missing=${s.missing_in_db_count}, Extra=${s.extra_in_db_count}.
                    Drift+Missing исправляются автоматически из applied_state.
                </div>
                <button data-fix-category="all"
                        style="padding:8px 14px; background:#2563eb; color:#fff; border:none; border-radius:4px; cursor:pointer; font-size:13px; font-weight:600; white-space:nowrap;">
                    <i class="fa-solid fa-wand-magic-sparkles"></i> Исправить всё (${s.drift_count + s.missing_in_db_count})
                </button>
            </div>`;

        cont.innerHTML = `
            <div style="display:grid; grid-template-columns:repeat(3,1fr); gap:8px; margin-bottom:12px;">
                <div style="padding:10px; background:#fffbeb; border-radius:4px; border-left:3px solid #a16207;">
                    <div style="font-size:11px; color:#6b7280;">Drift</div>
                    <div style="font-size:22px; font-weight:700; color:#a16207;">${s.drift_count}</div>
                </div>
                <div style="padding:10px; background:#fef2f2; border-radius:4px; border-left:3px solid #dc2626;">
                    <div style="font-size:11px; color:#6b7280;">Missing</div>
                    <div style="font-size:22px; font-weight:700; color:#dc2626;">${s.missing_in_db_count}</div>
                </div>
                <div style="padding:10px; background:#f5f3ff; border-radius:4px; border-left:3px solid #7c3aed;">
                    <div style="font-size:11px; color:#6b7280;">Extra/Zombie</div>
                    <div style="font-size:22px; font-weight:700; color:#7c3aed;">${s.extra_in_db_count}</div>
                </div>
            </div>
            <div style="font-size:11px; color:#6b7280; margin-bottom:12px;">
                Сверка с логами 209=№${data.latest_209_log_id || '—'}, 205=№${data.latest_205_log_id || '—'}.
                Порог расхождения: ${data.threshold_rub} ₽. Жильцов в applied_state: ${s.expected_users}. Reading'ов в БД: ${s.actual_readings}.
            </div>
            ${allClean ? `
                <div style="margin-top:20px; padding:24px; text-align:center; color:#15803d;">
                    <i class="fa-solid fa-circle-check" style="font-size:32px;"></i>
                    <p style="margin:12px 0 0; font-weight:600;">Целостность данных в норме</p>
                    <p style="font-size:12px; color:#6b7280;">Никаких расхождений между импортом и БД не обнаружено.</p>
                </div>
            ` : (fixAllBtn + driftHtml + missingHtml + extraHtml)}
        `;

        // Прицепляем хендлеры на все кнопки фикса (group и individual).
        cont.querySelectorAll('[data-fix-category]').forEach(btn => {
            btn.addEventListener('click', () => {
                const cat = btn.getAttribute('data-fix-category');
                this._integrityFix(overlay, { category: cat });
            });
        });
        cont.querySelectorAll('[data-fix-user]').forEach(btn => {
            btn.addEventListener('click', () => {
                const uid = parseInt(btn.getAttribute('data-fix-user'), 10);
                if (uid) this._integrityFix(overlay, { category: 'user', user_id: uid });
            });
        });
        cont.querySelector('[data-zombie-from-integrity]')?.addEventListener('click', () => {
            overlay.remove();
            this.openZombieModal();
        });
    },

    /** Auto-fix Bug AK: вызывает /debts/integrity-fix с подтверждением.
     *  После фикса перезагружает модалку integrity-check, чтобы показать
     *  актуальное состояние (расхождений должно стать меньше или 0). */
    async _integrityFix(overlay, params) {
        const { category, user_id } = params;
        let confirmText;
        if (category === 'all') confirmText = 'Применить ВСЕ исправления (drift + missing) из applied_state?';
        else if (category === 'drift') confirmText = 'Записать «Ожидается» в БД для всех drift-расхождений?';
        else if (category === 'missing') confirmText = 'Создать reading-и для всех missing жильцов?';
        else if (category === 'user') confirmText = `Исправить расхождение для user_id=${user_id}?`;
        if (!await showConfirm(confirmText, { title: 'Исправление целостности', confirmText: 'Применить' })) return;

        try {
            const qs = new URLSearchParams({ category, confirm: 'YES' });
            if (user_id) qs.set('user_id', String(user_id));
            const res = await api.post(`/financier/debts/integrity-fix?${qs.toString()}`);
            const total = (res.fixed_drift || 0) + (res.fixed_missing || 0);
            toast(`Исправлено: drift=${res.fixed_drift || 0}, missing=${res.fixed_missing || 0}`, total > 0 ? 'success' : 'info');
            if ((res.errors || []).length) {
                console.warn('integrity-fix errors:', res.errors);
            }
            // Перезагружаем содержимое модалки и таблицу долгов.
            const fresh = await api.get('/financier/debts/integrity-check');
            this._renderIntegrityContent(overlay, fresh);
            this.reload();
            this.loadStats();
        } catch (e) {
            toast('Ошибка фикса: ' + (e.message || 'неизвестно'), 'error');
        }
    },

    /** Этап 3: модалка zombie-сальдо. Находит reading'и с долгом, которых
     *  в свежем импорте 1С уже нет. Кандидаты на зануление. */
    async openZombieModal() {
        const overlay = document.createElement('div');
        overlay.style.cssText = 'position:fixed; inset:0; background:rgba(0,0,0,0.45); z-index:9999; display:flex; align-items:center; justify-content:center; padding:20px;';
        overlay.innerHTML = `
            <div style="background:#fff; border-radius:8px; width:min(900px, 100%); max-height:90vh; display:flex; flex-direction:column;">
                <div style="display:flex; justify-content:space-between; align-items:center; padding:14px 18px; border-bottom:1px solid var(--border-color);">
                    <h3 style="margin:0; font-size:15px;">
                        👻 Zombie-сальдо: долги без свежего импорта 1С
                    </h3>
                    <button data-close-zombie style="background:none; border:none; font-size:20px; color:#6b7280; cursor:pointer;">×</button>
                </div>
                <div id="zombieContent" style="padding:14px 18px; overflow-y:auto; flex:1;">
                    <p style="color:var(--text-secondary);"><i class="fa-solid fa-spinner fa-spin"></i> Поиск...</p>
                </div>
            </div>
        `;
        document.body.appendChild(overlay);
        const close = () => overlay.remove();
        overlay.querySelector('[data-close-zombie]').addEventListener('click', close);
        overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });

        try {
            const data = await api.get('/financier/debts/zombie-readings');
            this._renderZombieContent(overlay, data);
        } catch (e) {
            const c = overlay.querySelector('#zombieContent');
            if (c) c.innerHTML = `<p style="color:var(--danger-color);">Ошибка: ${esc(e.message)}</p>`;
        }
    },

    _renderZombieContent(overlay, data) {
        const cont = overlay.querySelector('#zombieContent');
        if (!cont) return;
        if (data.note) {
            cont.innerHTML = `<p style="color:var(--text-secondary);">${esc(data.note)}</p>`;
            return;
        }
        if (!data.count) {
            cont.innerHTML = `
                <div style="padding:24px; text-align:center; color:#15803d;">
                    <i class="fa-solid fa-circle-check" style="font-size:32px;"></i>
                    <p style="margin:12px 0 0; font-weight:600;">Zombie-сальдо не найдено</p>
                    <p style="font-size:12px; color:#6b7280;">Все долги в БД соответствуют свежему импорту 1С.</p>
                </div>`;
            return;
        }
        const totalSum = data.zombies.reduce((s, z) => s + (z.total_to_clean || 0), 0);
        const rowsHtml = data.zombies.map(z => `
            <tr>
                <td style="padding:6px 8px;">${z.user_id}</td>
                <td style="padding:6px 8px; font-weight:500;">${esc(z.username || '—')}</td>
                <td style="padding:6px 8px; font-size:11px; color:#6b7280;">${esc(z.room_label || '—')}</td>
                <td style="padding:6px 8px; text-align:right; color:#b91c1c;">${z.debt_209 ? z.debt_209.toFixed(2) : '—'}</td>
                <td style="padding:6px 8px; text-align:right; color:#b91c1c;">${z.debt_205 ? z.debt_205.toFixed(2) : '—'}</td>
                <td style="padding:6px 8px; text-align:right; color:#15803d;">${(z.overpayment_209 + z.overpayment_205) > 0 ? (z.overpayment_209 + z.overpayment_205).toFixed(2) : '—'}</td>
            </tr>
        `).join('');
        cont.innerHTML = `
            <div style="margin-bottom:12px; padding:10px 12px; background:#fef2f2; border-left:3px solid #b91c1c; border-radius:4px;">
                <b>Найдено zombie-reading'ов:</b> ${data.count}
                · сумма к занулению: <b>${totalSum.toFixed(2)} ₽</b>
                · сверка с логами 209=№${data.latest_209_log_id || '—'}, 205=№${data.latest_205_log_id || '—'}
                <p style="margin:6px 0 0; font-size:12px; color:#6b7280;">
                    Это reading'и с долгом/переплатой, чьего user_id нет в свежем импорте 1С.
                    Обычно — остатки от старого per-room импорта (Bug AG). Зануление безопасно: reading'и остаются
                    в БД (для аудита), но debt_*/overpayment_* становятся 0.
                </p>
            </div>
            <div style="overflow:auto; max-height:50vh;">
                <table style="width:100%; border-collapse:collapse; font-size:12px;">
                    <thead style="background:#f9fafb; position:sticky; top:0;">
                        <tr>
                            <th style="padding:6px 8px; text-align:left;">user_id</th>
                            <th style="padding:6px 8px; text-align:left;">ФИО</th>
                            <th style="padding:6px 8px; text-align:left;">Комната</th>
                            <th style="padding:6px 8px; text-align:right;">Долг 209</th>
                            <th style="padding:6px 8px; text-align:right;">Долг 205</th>
                            <th style="padding:6px 8px; text-align:right;">Перепл.</th>
                        </tr>
                    </thead>
                    <tbody>${rowsHtml}</tbody>
                </table>
            </div>
            <div style="margin-top:14px; display:flex; justify-content:flex-end; gap:8px;">
                <button data-zombie-confirm
                        style="padding:8px 14px; background:#b91c1c; color:#fff; border:none; border-radius:4px; cursor:pointer; font-weight:500;">
                    <i class="fa-solid fa-broom"></i> Занулить ${data.count} reading'ов
                </button>
            </div>
        `;
        overlay.querySelector('[data-zombie-confirm]')?.addEventListener('click', () => this._confirmZombieCleanup(overlay, data));
    },

    async _confirmZombieCleanup(overlay, data) {
        if (!await showConfirm(
            `Занулить debt_209, debt_205, overpayment_209, overpayment_205 у ${data.count} reading-ов?\n\n` +
            `Reading'и НЕ удалятся — только финансовые поля станут 0₽. Это обратимо через откат импорта или ручную корректировку.`,
            { title: 'Зануление сальдо', confirmText: 'Занулить', danger: true }
        )) return;
        try {
            const res = await api.post('/financier/debts/cleanup-zombie-readings?confirm=YES');
            toast(`Занулено ${res.cleaned} reading'ов`, 'success');
            overlay.remove();
            this.reload();
            this.loadStats();
        } catch (e) {
            toast('Ошибка: ' + (e.message || 'неизвестно'), 'error');
        }
    },

    async openDiagnoseModal(logId) {
        const overlay = document.createElement('div');
        overlay.style.cssText = 'position:fixed; inset:0; background:rgba(0,0,0,0.45); z-index:9999; display:flex; align-items:center; justify-content:center; padding:20px;';
        overlay.innerHTML = `
            <div style="background:#fff; border-radius:8px; width:min(900px, 100%); max-height:90vh; display:flex; flex-direction:column;">
                <div style="display:flex; justify-content:space-between; align-items:center; padding:14px 18px; border-bottom:1px solid var(--border-color);">
                    <h3 style="margin:0; font-size:15px;">
                        🔬 Диагностика парсера №${logId}
                    </h3>
                    <button data-close-diagnose style="background:none; border:none; font-size:20px; color:#6b7280; cursor:pointer;">×</button>
                </div>
                <div style="padding:10px 18px; border-bottom:1px solid var(--border-color); display:flex; gap:8px; align-items:center;">
                    <label style="font-size:12px; color:var(--text-secondary);">Поиск жильца:</label>
                    <input type="text" id="diagnoseFioSearch"
                           placeholder="Бендас / Миронов / любая часть ФИО"
                           style="flex:1; padding:5px 8px; font-size:12px; border:1px solid var(--border-color); border-radius:4px;">
                    <button id="diagnoseSearchBtn" class="action-btn primary-btn" style="padding:5px 10px; font-size:12px;">
                        <i class="fa-solid fa-search"></i> Найти
                    </button>
                </div>
                <div style="padding:16px 18px; overflow-y:auto; flex:1;" id="diagnoseContent">
                    <p style="color:var(--text-secondary); font-size:13px;">
                        <i class="fa-solid fa-spinner fa-spin"></i> Парсим архив… (5-15 сек)
                    </p>
                </div>
            </div>`;
        document.body.appendChild(overlay);
        const close = () => overlay.remove();
        overlay.addEventListener('click', (e) => {
            if (e.target === overlay || e.target.closest('[data-close-diagnose]')) close();
        });

        // Кнопка поиска ФИО + Enter
        const searchInput = overlay.querySelector('#diagnoseFioSearch');
        const searchBtn = overlay.querySelector('#diagnoseSearchBtn');
        const reloadWithSearch = async () => {
            const fio = searchInput?.value?.trim() || '';
            const cont = overlay.querySelector('#diagnoseContent');
            if (cont) cont.innerHTML = `<p style="color:var(--text-secondary);"><i class="fa-solid fa-spinner fa-spin"></i> Ищем «${esc(fio)}»…</p>`;
            try {
                const url = fio
                    ? `/financier/debts/import-history/${logId}/parser-diagnose?fio_search=${encodeURIComponent(fio)}`
                    : `/financier/debts/import-history/${logId}/parser-diagnose`;
                const data = await api.get(url);
                this._renderDiagnoseContent(overlay, data, fio);
            } catch (e) {
                if (cont) cont.innerHTML = `<p style="color:var(--danger-color);">Ошибка: ${esc(e.message)}</p>`;
            }
        };
        searchBtn?.addEventListener('click', reloadWithSearch);
        searchInput?.addEventListener('keydown', (e) => { if (e.key === 'Enter') reloadWithSearch(); });

        try {
            const data = await api.get(`/financier/debts/import-history/${logId}/parser-diagnose`);
            this._renderDiagnoseContent(overlay, data, '');
            return;
        } catch (e) {
            const cont = overlay.querySelector('#diagnoseContent');
            if (cont) cont.innerHTML = `<p style="color:var(--danger-color);">Ошибка: ${esc(e.message)}</p>`;
            return;
        }
    },

    _renderDiagnoseContent(overlay, data, searchQuery) {
        const cont = overlay.querySelector('#diagnoseContent');
        if (!cont) return;
        try {
            const sectionsHtml = Object.keys(data.section_markers || {}).length
                ? Object.entries(data.section_markers).map(([k, v]) => `<span style="background:#dbeafe; color:#1e40af; padding:2px 7px; border-radius:4px; font-size:11px;">${esc(k)}: col ${v}</span>`).join(' ')
                : '<span style="color:#dc2626;">не найдены</span>';

            const accountHtml = data.account_total
                ? `<div style="background:#dcfce7; padding:8px 10px; border-radius:6px; font-size:12px;">
                       <b>Итоговая строка счёта найдена:</b><br>
                       row ${data.account_total.row_idx}, label_col ${data.account_total.label_col}, label «${esc(data.account_total.label)}»<br>
                       <b>Числовые позиции:</b> ${(data.account_total.numeric_positions || []).join(', ')}<br>
                       <b>Значения:</b> ${Object.entries(data.account_total.all_values || {}).map(([c, v]) => `col${c}=${Number(v).toLocaleString('ru-RU')}`).join(' · ')}
                   </div>`
                : `<div style="background:#fee2e2; color:#991b1b; padding:8px 10px; border-radius:6px; font-size:12px;">
                       ❌ Итоговая строка счёта (209.X / 205.X) НЕ найдена в первых 20 строках. Парсер пойдёт fallback'ом.
                   </div>`;

            const chosen = data.chosen || {};
            const chosenHtml = chosen.debt_col_last !== null
                ? `<div style="background:#fff; border:1px solid var(--border-color); padding:8px 10px; border-radius:6px; font-size:12px;">
                       <b>Парсер выбрал колонки:</b><br>
                       <b>Дебет:</b> начало <span style="color:#dc2626;">col ${chosen.debt_col_first}</span> · конец <span style="color:#059669;">col ${chosen.debt_col_last}</span><br>
                       <b>Кредит:</b> начало <span style="color:#dc2626;">col ${chosen.overpay_col_first}</span> · конец <span style="color:#059669;">col ${chosen.overpay_col_last}</span><br>
                       <b>Стратегия:</b> ${esc(chosen.strategy || '—')}
                       ${chosen.debt_col_first === chosen.debt_col_last ? '<br><b style="color:#dc2626;">⚠ debt_first == debt_last — парсер сводит «начало» и «конец» к одной колонке (НЕПРАВИЛЬНО!)</b>' : ''}
                   </div>`
                : '<div style="color:#dc2626;">⚠ Парсер не определил колонки!</div>';

            const renderSample = (s) => {
                // Сравнение с БД (если есть db_lookup из fio_search режима).
                const db = s.db_lookup;
                let dbBlock = '';
                if (db) {
                    if (db.matched_user_id) {
                        const mismatchColor = db.mismatch ? '#dc2626' : '#059669';
                        const mismatchIcon = db.mismatch ? '⚠' : '✓';
                        const dbDebt = db.db_debt !== null ? Number(db.db_debt).toFixed(2) : 'NULL (нет reading)';
                        dbBlock = `
                            <div style="margin-top:6px; padding:6px 8px; background:${db.mismatch ? '#fef2f2' : '#dcfce7'}; border-left:3px solid ${mismatchColor}; border-radius:4px; font-size:11px;">
                                <b>В БД</b> (user_id=${db.matched_user_id}, username=${esc(db.matched_username || '')}):
                                ${mismatchIcon} debt = ${dbDebt} (ожидается ${db.expected_debt})
                                ${db.fuzzy && db.fuzzy.score ? `<br><i>fuzzy: matched «${esc(db.fuzzy.key || '')}» score ${db.fuzzy.score}${db.fuzzy.too_low ? ' ⚠ TOO LOW' : ''}</i>` : ''}
                                ${db.mismatch ? '<br><b style="color:#991b1b;">⚠ Значения не совпадают — переимпорт нужен или wrong-user fuzzy.</b>' : ''}
                            </div>`;
                    } else {
                        dbBlock = `
                            <div style="margin-top:6px; padding:6px 8px; background:#fef2f2; border-left:3px solid #dc2626; border-radius:4px; font-size:11px;">
                                <b>⚠ Жилец НЕ найден в БД</b>
                                ${db.fuzzy ? `<br>лучший fuzzy: «${esc(db.fuzzy.key || '')}» score ${db.fuzzy.score} (порог 80)` : ''}
                                <br>Эти деньги (${db.expected_debt} / ${db.expected_overpayment}) попадут в not_found.
                            </div>`;
                    }
                }
                // Raw values по колонкам
                const rawHtml = s.raw_values ? `
                    <div style="font-size:10.5px; color:var(--text-secondary); margin-top:2px;">
                        ${Object.entries(s.raw_values).map(([k, v]) => `${esc(k)}=${v === null ? '<i>null</i>' : v}`).join(' · ')}
                    </div>` : '';
                return `
                    <div style="padding:6px 8px; background:#f9fafb; border-radius:4px; margin-bottom:4px; font-size:12px;">
                        <b>${esc(s.fio)}</b> (col ${s.fio_col})<br>
                        <span style="color:#dc2626;">debt = ${s.debt_extracted}</span> · <span style="color:#7c3aed;">overpayment = ${s.overpayment_extracted}</span>
                        ${rawHtml}
                        ${dbBlock}
                    </div>`;
            };
            const samplesHtml = (data.samples || []).length
                ? `<div style="margin-top:14px;">
                       <h4 style="margin:0 0 6px 0; font-size:13px;">${searchQuery ? `Найдено по «${esc(searchQuery)}»: ${data.samples.length}` : 'Sample 3 жильцов:'}</h4>
                       ${(data.samples || []).map(renderSample).join('')}
                   </div>`
                : (searchQuery ? `<div style="margin-top:14px; padding:10px; background:#fee2e2; color:#991b1b; border-radius:4px;">По запросу «${esc(searchQuery)}» в файле никого не найдено.</div>` : '');

            cont.innerHTML = `
                <div style="display:grid; gap:12px;">
                    <div>
                        <div style="font-size:11px; color:var(--text-secondary); margin-bottom:4px;">SECTION MARKERS:</div>
                        ${sectionsHtml}
                    </div>
                    <div>
                        <div style="font-size:11px; color:var(--text-secondary); margin-bottom:4px;">«Дебет» / «Кредит» позиции (header):</div>
                        Дебет: ${(data.debit_cols_in_header || []).join(', ') || '—'} · Кредит: ${(data.credit_cols_in_header || []).join(', ') || '—'}
                    </div>
                    ${accountHtml}
                    ${chosenHtml}
                    ${samplesHtml}
                </div>
            `;
        } catch (e) {
            const cont = overlay.querySelector('#diagnoseContent');
            if (cont) cont.innerHTML = `<p style="color:var(--danger-color);">Ошибка: ${esc(e.message)}</p>`;
        }
    },

    async deleteImportHistory(logId) {
        if (!await showConfirm(
            `Удалить запись истории импорта №${logId}?\n\n` +
            `ВНИМАНИЕ: это удаление БЕЗ отката данных. Используйте только если\n` +
            `этот импорт уже не актуален (данные перетёрты последующим импортом\n` +
            `или массовым rebuild). Если нужен откат — жми «Откатить» вместо.`,
            { title: 'Удаление записи', confirmText: 'Удалить', danger: true }
        )) return;
        try {
            await api.delete(`/financier/debts/import-history/${logId}`);
            toast(`Запись №${logId} удалена из истории`, 'success');
            this.loadImportHistory();
        } catch (e) {
            toast('Ошибка удаления: ' + e.message, 'error');
        }
    },

    /** Массовая чистка: удаляет все откаченные/failed + старые completed
     *  (оставляет последние 5 на каждый счёт). Идеально после массового
     *  rebuild когда в истории накопился мусор. */
    async cleanupImportHistory() {
        if (!await showConfirm(
            `Очистить устаревшие записи истории импорта?\n\n` +
            `Будут удалены:\n` +
            `  • все откаченные (status=reverted)\n` +
            `  • все failed (с ошибкой)\n` +
            `  • completed старше последних 5 на каждый счёт (209/205).\n\n` +
            `Актуальные последние импорты сохранятся. Действие необратимо.`,
            { title: 'Очистка истории', confirmText: 'Очистить', danger: true }
        )) return;
        try {
            const res = await api.post(
                `/financier/debts/import-history/cleanup?keep_last=5`,
                {}
            );
            toast(`Готово. Осталось записей: ${res.remaining !== undefined ? res.remaining : '—'}`, 'success');
            this.loadImportHistory();
        } catch (e) {
            toast('Ошибка чистки: ' + e.message, 'error');
        }
    },

    /** Поиск ФИО жильца в архивах последних импортов 1С. Открывает
     *  модалку которая для каждого импорта показывает: найдено / не
     *  найдено + значения из строки (если найдено). Помогает понять
     *  почему у жильца «—» в долгах. */
    async openCheckCoverage(userId, username) {
        // Простая модалка через document.body. Не используем глобальные
        // modal-helpers чтобы не плодить зависимости.
        const overlay = document.createElement('div');
        overlay.style.cssText = 'position:fixed; inset:0; background:rgba(0,0,0,0.45); z-index:9999; display:flex; align-items:center; justify-content:center; padding:20px;';
        overlay.innerHTML = `
            <div style="background:#fff; border-radius:8px; width:min(720px, 100%); max-height:90vh; display:flex; flex-direction:column;">
                <div style="display:flex; justify-content:space-between; align-items:center; padding:14px 18px; border-bottom:1px solid var(--border-color);">
                    <h3 style="margin:0; font-size:15px;">
                        🔍 Поиск «${esc(username)}» в архивах 1С
                    </h3>
                    <button data-close-coverage style="background:none; border:none; font-size:20px; color:#6b7280; cursor:pointer;">×</button>
                </div>
                <div style="padding:16px 18px; overflow-y:auto; flex:1;" id="coverageContent">
                    <p style="color:var(--text-secondary); font-size:13px;">
                        <i class="fa-solid fa-spinner fa-spin"></i> Парсим архивы… (до 20 сек, openpyxl на read-only)
                    </p>
                </div>
            </div>`;
        document.body.appendChild(overlay);
        const close = () => overlay.remove();
        overlay.addEventListener('click', (e) => {
            if (e.target === overlay || e.target.closest('[data-close-coverage]')) close();
        });

        try {
            const data = await api.get(`/financier/debts/check-resident-coverage/${userId}`);
            const cont = overlay.querySelector('#coverageContent');
            if (!cont) return;

            const headerHtml = `
                <div style="margin-bottom:14px; padding:10px 12px; background:#f3f4f6; border-radius:6px; font-size:12.5px;">
                    <div><b>ФИО в БД:</b> ${esc(data.fio_db)}</div>
                    <div style="color:var(--text-secondary); margin-top:3px;">
                        Проверено импортов: ${data.imports_checked} (последних)
                    </div>
                </div>`;

            const items = (data.results || []).map(r => {
                let body;
                if (r.error) {
                    body = `<div style="color:#dc2626; font-size:11px;">⚠ ${esc(r.error)}</div>`;
                } else if (!r.matches.length) {
                    body = `<div style="color:var(--text-secondary); font-size:11px; font-style:italic;">Не найдено в этом архиве</div>`;
                } else {
                    body = r.matches.map(m => `
                        <div style="padding:6px 8px; background:${m.exact_match ? '#dcfce7' : '#fef3c7'}; border-radius:4px; margin-top:4px; font-size:11px;">
                            <b>${m.exact_match ? '✓ Точное совпадение' : '~ Похожее ФИО'}:</b>
                            ${esc(m.fio_in_excel)}<br>
                            <span style="color:var(--text-secondary);">Excel row ${m.row_excel} · значения: ${m.numeric_values.length ? m.numeric_values.map(v => v.toFixed(2)).join(' / ') : 'все нули'}</span>
                        </div>
                    `).join('');
                }
                const statusColor = r.status === 'completed' ? '#059669' : '#6b7280';
                return `
                    <div style="border:1px solid var(--border-color); border-radius:6px; padding:10px 12px; margin-bottom:8px;">
                        <div style="display:flex; justify-content:space-between; font-size:12px;">
                            <span><b>№${r.log_id} · ${esc(r.account_type)}</b> · <span style="color:${statusColor};">${esc(r.status)}</span></span>
                            <span style="color:var(--text-secondary);">${r.started_at ? esc(r.started_at.split('T')[0]) : '—'}</span>
                        </div>
                        ${body}
                    </div>`;
            }).join('');

            // Подсказки админу.
            const anyFound = (data.results || []).some(r => r.matches && r.matches.length > 0);
            const anyWithValues = (data.results || []).some(r =>
                r.matches && r.matches.some(m => m.numeric_values && m.numeric_values.length > 0)
            );
            let hint;
            if (!anyFound) {
                hint = `<div style="background:#fee2e2; color:#991b1b; padding:10px 12px; border-radius:6px; font-size:12px; margin-top:8px;">
                    💡 <b>ФИО не найдено ни в одном из последних импортов.</b> Жилец не передавался из 1С — обратитесь к бухгалтерии.
                </div>`;
            } else if (anyWithValues) {
                hint = `<div style="background:#fef3c7; color:#92400e; padding:10px 12px; border-radius:6px; font-size:12px; margin-top:8px;">
                    💡 <b>ФИО найдено с цифрами, но в БД у жильца долгов нет.</b> Возможно fuzzy-привязка пошла к другому жильцу. Откройте бейдж «⚠ N» (not_found) у соответствующего импорта и попробуйте reassign.
                </div>`;
            } else {
                hint = `<div style="background:#dcfce7; color:#166534; padding:10px 12px; border-radius:6px; font-size:12px; margin-top:8px;">
                    💡 <b>ФИО найдено, но с нулями.</b> Это нормально — у жильца нет долгов в 1С.
                </div>`;
            }

            cont.innerHTML = headerHtml + items + hint;
        } catch (e) {
            const cont = overlay.querySelector('#coverageContent');
            if (cont) cont.innerHTML = `<p style="color:var(--danger-color);">Ошибка: ${esc(e.message)}</p>`;
        }
    },

    // Разбор: почему ненайденные ФИО не сматчились (категории + ближайший кандидат).
    async renderNotFoundAnalysis(logId) {
        const box = this.dom.notFoundList;
        if (!box) return;
        box.innerHTML = '<div style="padding:20px; text-align:center;"><i class="fa-solid fa-spinner fa-spin"></i> Анализ…</div>';
        try {
            const data = await api.get(`/financier/debts/import-history/${logId}/not-found-analysis`);
            const c = data.categories || {};
            const CAT = {
                same:     { label: 'Скорее тот же',  color: '#15803d', bg: '#dcfce7', desc: 'фамилия+имя+отчество совпали — привязать безопасно' },
                namesake: { label: 'Однофамилец',    color: '#9a3412', bg: '#ffedd5', desc: 'РАЗНЫЙ человек — без проверки не привязывать' },
                absent:   { label: 'Нет в базе',     color: '#991b1b', bg: '#fee2e2', desc: 'не заведён жильцом (новый / наниматель / не-резидент)' },
            };
            const summary = ['same', 'namesake', 'absent'].map(k => `
                <div style="flex:1; background:${CAT[k].bg}; border-radius:8px; padding:10px; text-align:center;">
                    <div style="font-size:22px; font-weight:700; color:${CAT[k].color};">${c[k] || 0}</div>
                    <div style="font-size:11px; font-weight:600; color:${CAT[k].color};">${CAT[k].label}</div>
                    <div style="font-size:10px; color:var(--text-secondary); margin-top:2px;">${CAT[k].desc}</div>
                </div>`).join('');
            const rows = (data.items || []).map(it => {
                const cat = CAT[it.category] || CAT.absent;
                const cand = it.candidate
                    ? `${esc(it.candidate.username)}${it.candidate.room ? ` · <span style="color:var(--text-secondary);">${esc(it.candidate.room)}</span>` : ''}`
                    : '<span style="color:var(--text-tertiary);">—</span>';
                return `<tr style="border-bottom:1px solid #eef2f7;">
                    <td style="padding:5px 8px;">${esc(it.fio)}</td>
                    <td style="padding:5px 8px; text-align:right; font-family:monospace; color:#991b1b;">${Number(it.debt || 0).toFixed(2)}</td>
                    <td style="padding:5px 8px; text-align:center;"><span style="background:${cat.bg}; color:${cat.color}; padding:1px 7px; border-radius:8px; font-size:11px; font-weight:700;">${it.best_score}</span></td>
                    <td style="padding:5px 8px; font-size:12px;">${cand}${it.reason ? ` <span style="color:var(--text-tertiary); font-size:10px;">(${esc(it.reason)})</span>` : ''}</td>
                </tr>`;
            }).join('');
            box.innerHTML = `
                <button class="action-btn secondary-btn" id="btnNfBack" style="font-size:12px; padding:5px 10px; margin-bottom:12px;">← К списку (привязка)</button>
                <div style="display:flex; gap:8px; margin-bottom:12px;">${summary}</div>
                <table style="width:100%; border-collapse:collapse; font-size:12px;">
                    <thead style="background:var(--bg-page); font-size:10px; color:var(--text-tertiary); text-transform:uppercase;">
                        <tr>
                            <th style="text-align:left; padding:5px 8px;">ФИО из 1С</th>
                            <th style="text-align:right; padding:5px 8px;">Долг</th>
                            <th style="text-align:center; padding:5px 8px;" title="0–100: насколько близок лучший кандидат в базе">Score</th>
                            <th style="text-align:left; padding:5px 8px;">Ближайший в базе</th>
                        </tr>
                    </thead>
                    <tbody>${rows}</tbody>
                </table>`;
            document.getElementById('btnNfBack')?.addEventListener('click', () => this.openNotFoundModal(logId));
        } catch (e) {
            box.innerHTML = `<div style="padding:16px; color:var(--danger-color);">Ошибка анализа: ${esc(e.message)}</div>`;
        }
    },

    // ==========================================================================
    // МОДАЛКА «НЕ НАЙДЕННЫЕ»
    // ==========================================================================
    async openNotFoundModal(logId) {
        if (!this.dom.notFoundModal) return;
        this.dom.notFoundModal.classList.add('open');
        this.dom.notFoundLogMeta.textContent = `импорт №${logId}`;
        this.dom.notFoundList.innerHTML = '<div style="padding:20px; text-align:center;"><i class="fa-solid fa-spinner fa-spin"></i> Загрузка…</div>';
        try {
            const data = await api.get(`/financier/debts/import-history/${logId}/not-found`);
            // Part B: явно подписываем счёт (209/205), чтобы было видно «в каком
            // счёте не найдено». Суммы по каждому ФИО рендерит renderNotFoundRow.
            this.dom.notFoundLogMeta.textContent = `импорт №${logId} · счёт ${data.account_type || '—'}`;
            const list = data.not_found_users || [];
            if (!list.length) {
                this.dom.notFoundList.innerHTML = '<div style="padding:20px; text-align:center; color:var(--text-secondary);">Все ФИО из этого импорта привязаны.</div>';
                return;
            }
            this.dom.notFoundList.innerHTML = `
                <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:10px; margin-bottom:12px;">
                    <p class="hint-text" style="font-size:12px; margin:0;">
                        ФИО из Excel, которых fuzzy-матчер не смог привязать к жильцу.
                        <b>Суммы долга/переплаты подгружены автоматически</b> — нажмите
                        «Найти похожих» (если жилец есть в системе) или «Создать жильца».
                    </p>
                    <button class="action-btn secondary-btn" id="btnNfAnalysis" style="white-space:nowrap; font-size:12px; padding:5px 10px;" title="Разобрать почему не сматчились: ближайший кандидат + категория">
                        📊 Почему не нашлись?
                    </button>
                </div>
                ${list.map(item => {
                    // Backend нормализует к dict {fio, debt, overpayment}.
                    // Старые импорты (до фикса) — debt/overpayment = "0".
                    const fio = (typeof item === 'object') ? item.fio : item;
                    const debt = (typeof item === 'object') ? Number(item.debt) || 0 : 0;
                    const overpay = (typeof item === 'object') ? Number(item.overpayment) || 0 : 0;
                    return this.renderNotFoundRow(fio, logId, data.account_type, debt, overpay);
                }).join('')}
            `;
            document.getElementById('btnNfAnalysis')?.addEventListener('click', () => this.renderNotFoundAnalysis(logId));
            // Контекст для click-handler. Меняется при каждом openNotFoundModal,
            // handler читает из state — нет накопления listeners.
            this._nfCtx = { logId, accountType: data.account_type };
            if (!this._nfClickHandlerAttached) {
                this.dom.notFoundList.addEventListener('click', (e) => {
                    const btn = e.target.closest('button[data-nf-action]');
                    if (!btn || !this._nfCtx) return;
                    const { logId, accountType } = this._nfCtx;
                    const action = btn.dataset.nfAction;
                    const row = btn.closest('.nf-row');
                    if (!row) return;
                    const fio = row.dataset.fio;
                    if (action === 'find') {
                        this._nfFindCandidates(row, fio, logId, accountType);
                    } else if (action === 'create') {
                        this._nfShowCreateForm(row, fio, logId, accountType);
                    } else if (action === 'legacy') {
                        this._nfShowLegacyForm(row, fio, logId, accountType);
                    } else if (action === 'pick-candidate') {
                        this._nfPickCandidate(row, fio, logId, accountType, Number(btn.dataset.userId), btn.dataset.username);
                    } else if (action === 'edit-fio') {
                        this._nfEditFio(btn, Number(btn.dataset.userId), btn.dataset.username);
                    } else if (action === 'submit-create') {
                        this._nfSubmitCreate(row, fio, logId, accountType);
                    } else if (action === 'submit-legacy') {
                        this._nfSubmitLegacy(row, fio, logId, accountType);
                    }
                });
                this._nfClickHandlerAttached = true;
            }
            // Инжектим CSS для иконки-карандаша «Исправить ФИО» один раз —
            // нужен hover-стейт и opacity, которые inline в HTML не работают.
            if (!document.getElementById('nf-fio-edit-styles')) {
                const styleEl = document.createElement('style');
                styleEl.id = 'nf-fio-edit-styles';
                styleEl.textContent = `
                    .nf-edit-fio-btn {
                        background: transparent;
                        border: none;
                        padding: 2px 5px;
                        border-radius: 4px;
                        color: var(--text-tertiary);
                        opacity: 0.35;
                        cursor: pointer;
                        font-size: 11px;
                        transition: opacity 0.15s, background 0.15s, color 0.15s;
                    }
                    .nf-candidate:hover .nf-edit-fio-btn { opacity: 0.7; }
                    .nf-edit-fio-btn:hover {
                        opacity: 1 !important;
                        background: #eef2ff;
                        color: #4338ca;
                    }
                `;
                document.head.appendChild(styleEl);
            }
        } catch (e) {
            this.dom.notFoundList.innerHTML = `<div style="padding:16px; color:var(--danger-color);">Ошибка: ${esc(e.message)}</div>`;
        }
    },

    renderNotFoundRow(fio, logId, accountType, debt = 0, overpay = 0) {
        // Каждая строка содержит:
        //  - ФИО + поля для суммы (префилл из импорта, можно править)
        //  - Кнопка «Найти похожих» — раскрывает inline-блок с кандидатами
        //  - Кнопка «Создать жильца» — раскрывает форму создания
        //  - (старое) Inline-форма с логином — fallback если знаешь точный логин
        const safeId = btoa(unescape(encodeURIComponent(fio))).replace(/[^a-zA-Z0-9]/g, '').slice(0, 16);
        const sumHint = (debt > 0 || overpay > 0)
            ? `<span style="font-size:11px; color:#92400e; margin-left:6px;">
                 ${debt > 0 ? `долг ${debt.toLocaleString('ru-RU')} ₽` : ''}
                 ${overpay > 0 ? `${debt > 0 ? ' · ' : ''}переплата ${overpay.toLocaleString('ru-RU')} ₽` : ''}
                 (из файла)
               </span>`
            : '';
        return `
            <div class="nf-row" data-fio="${esc(fio)}" data-row-id="${safeId}"
                 style="border:1px solid var(--border-color); border-radius:8px; margin-bottom:10px; padding:10px;">
                <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:8px; margin-bottom:8px;">
                    <div style="flex:1; min-width:0;">
                        <div style="font-weight:600; font-size:13px; color:#1f2937; overflow-wrap:anywhere;">
                            ${esc(fio)}${sumHint}
                        </div>
                        <div style="font-size:11px; color:var(--text-secondary);">счёт ${esc(accountType)}</div>
                    </div>
                    <div style="display:flex; gap:6px; flex-wrap:wrap;">
                        <input type="number" data-nf-debt step="0.01" placeholder="Долг ₽" value="${debt || ''}" style="width:100px; font-size:12px;">
                        <input type="number" data-nf-overpay step="0.01" placeholder="Перепл. ₽" value="${overpay || ''}" style="width:100px; font-size:12px;">
                    </div>
                </div>
                <div style="display:flex; gap:6px; flex-wrap:wrap;">
                    <button data-nf-action="find" class="action-btn primary-btn" style="padding:5px 10px; font-size:12px;">
                        <i class="fa-solid fa-magnifying-glass"></i> Найти похожих
                    </button>
                    <button data-nf-action="create" class="action-btn success-btn" style="padding:5px 10px; font-size:12px;">
                        <i class="fa-solid fa-user-plus"></i> Создать жильца
                    </button>
                    <button data-nf-action="legacy" class="action-btn secondary-btn" style="padding:5px 10px; font-size:12px;">
                        <i class="fa-solid fa-keyboard"></i> Логин вручную
                    </button>
                </div>
                <div data-nf-pane="candidates" style="display:none; margin-top:10px; padding:10px; background:#f9fafb; border-radius:6px;"></div>
                <div data-nf-pane="create" style="display:none; margin-top:10px; padding:10px; background:#f0fdf4; border-radius:6px; border:1px solid #bbf7d0;"></div>
                <div data-nf-pane="legacy" style="display:none; margin-top:10px;"></div>
            </div>
        `;
    },

    _nfGetSums(row) {
        // Возвращает {debt, overpayment} из input полей в шапке строки.
        const debt = parseFloat(row.querySelector('[data-nf-debt]')?.value) || 0;
        const overpayment = parseFloat(row.querySelector('[data-nf-overpay]')?.value) || 0;
        return { debt, overpayment };
    },

    _nfShowPane(row, paneName) {
        // Прячет все панели в строке, потом показывает нужную.
        row.querySelectorAll('[data-nf-pane]').forEach(p => {
            p.style.display = p.dataset.nfPane === paneName ? '' : 'none';
        });
    },

    _nfRenderCandidates(cands, headerLabel) {
        if (!cands.length) {
            return `<div style="font-size:13px; color:var(--text-secondary); padding:8px;">
                Жильцов не нашлось.
            </div>`;
        }
        return `
            <div style="font-size:11px; color:var(--text-secondary); margin-bottom:8px; text-transform:uppercase;">
                ${headerLabel} (${cands.length})
            </div>
            ${cands.map(c => `
                <div class="nf-candidate" data-user-id="${c.id}" data-username="${esc(c.username)}"
                     style="display:flex; justify-content:space-between; align-items:center; gap:10px;
                            padding:8px 10px; background:#fff; border:1px solid var(--border-color); border-radius:6px; margin-bottom:6px;">
                    <div style="flex:1; min-width:0;">
                        <div class="nf-candidate-name-row" style="font-weight:600; font-size:13px; display:inline-flex; align-items:center; gap:6px;">
                            <span class="nf-candidate-username">${esc(c.username)}</span>
                            <span style="font-size:11px; color:var(--text-secondary);">${c.score}%</span>
                            <button data-nf-action="edit-fio" data-user-id="${c.id}" data-username="${esc(c.username)}"
                                    class="nf-edit-fio-btn"
                                    title="Исправить ФИО в базе (если в системе написано с ошибкой)">
                                <i class="fa-solid fa-pen"></i>
                            </button>
                        </div>
                        <div style="font-size:11px; color:var(--text-secondary);">
                            ${esc(c.room_label)} · ${c.residents_count} чел.
                        </div>
                        ${c.reason ? `<div style="font-size:11px; color:#92400e; margin-top:3px;">
                            <i class="fa-solid fa-circle-info"></i> ${esc(c.reason)}
                        </div>` : ''}
                    </div>
                    <button data-nf-action="pick-candidate" data-user-id="${c.id}" data-username="${esc(c.username)}"
                            class="action-btn primary-btn" style="padding:4px 10px; font-size:12px; white-space:nowrap;">
                        <i class="fa-solid fa-check"></i> Это он
                    </button>
                </div>
            `).join('')}`;
    },

    _nfEditFio(btn, userId, currentUsername) {
        // Превращаем span с username в input + кнопки Save/Cancel inline.
        const cand = btn.closest('.nf-candidate');
        if (!cand) return;
        const nameRow = cand.querySelector('.nf-candidate-name-row');
        if (!nameRow || nameRow.dataset.editing === '1') return;
        nameRow.dataset.editing = '1';
        const originalHtml = nameRow.innerHTML;
        nameRow.innerHTML = `
            <input type="text" data-nf-fio-input value="${esc(currentUsername)}"
                   style="width:65%; padding:3px 6px; font-size:13px; border:1px solid var(--border-color); border-radius:4px;">
            <button data-nf-fio-save class="action-btn success-btn" style="padding:3px 8px; font-size:11px; margin-left:4px;">
                <i class="fa-solid fa-check"></i>
            </button>
            <button data-nf-fio-cancel class="action-btn secondary-btn" style="padding:3px 8px; font-size:11px; margin-left:2px;">
                <i class="fa-solid fa-xmark"></i>
            </button>
        `;
        const input = nameRow.querySelector('[data-nf-fio-input]');
        input.focus();
        input.select();

        nameRow.querySelector('[data-nf-fio-cancel]').addEventListener('click', () => {
            nameRow.innerHTML = originalHtml;
            nameRow.dataset.editing = '';
        });
        const doSave = async () => {
            const newName = input.value.trim();
            if (!newName || newName.length < 3) {
                toast('Имя минимум 3 символа', 'warning');
                return;
            }
            if (newName === currentUsername) {
                nameRow.innerHTML = originalHtml;
                nameRow.dataset.editing = '';
                return;
            }
            try {
                await api.put(`/users/${userId}`, { username: newName });
                toast(`Имя жильца обновлено: ${newName}`, 'success');
                // Обновляем DOM на месте — не пересоздаём всю модалку.
                cand.dataset.username = newName;
                cand.querySelectorAll('button[data-username]').forEach(b => {
                    b.dataset.username = newName;
                });
                nameRow.innerHTML = originalHtml;
                nameRow.querySelector('.nf-candidate-username').textContent = newName;
                nameRow.querySelector('button[data-nf-action="edit-fio"]').dataset.username = newName;
                nameRow.dataset.editing = '';
            } catch (e) {
                toast('Ошибка обновления: ' + e.message, 'error');
            }
        };
        nameRow.querySelector('[data-nf-fio-save]').addEventListener('click', doSave);
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); doSave(); }
            if (e.key === 'Escape') {
                nameRow.innerHTML = originalHtml;
                nameRow.dataset.editing = '';
            }
        });
    },

    async _nfFindCandidates(row, fio, logId, accountType) {
        const pane = row.querySelector('[data-nf-pane="candidates"]');
        this._nfShowPane(row, 'candidates');

        // 1) Сначала рендерим контейнер с поиском и spinner-результатом —
        // чтобы input поиска появился сразу, не ждал API.
        pane.innerHTML = `
            <div style="margin-bottom:10px;">
                <input type="text" data-nf-search placeholder="🔍 Поиск по фамилии или имени (мин. 2 буквы)"
                       style="width:100%; padding:7px 10px; font-size:13px; border:1px solid var(--border-color); border-radius:6px;">
                <div style="font-size:11px; color:var(--text-secondary); margin-top:3px;">
                    Auto-suggest показывает похожих по импортированному ФИО. Введите запрос — найдёт по подстроке.
                </div>
            </div>
            <div data-nf-results>
                <div style="text-align:center; padding:14px; color:var(--text-secondary);">
                    <i class="fa-solid fa-spinner fa-spin"></i> Поиск похожих по «${esc(fio)}»…
                </div>
            </div>`;

        const results = pane.querySelector('[data-nf-results]');
        const input = pane.querySelector('[data-nf-search]');

        // 2) Auto-suggest по fio. Загрузим один раз и оставим как fallback
        // когда input пустой.
        let autoSuggestHtml = '';
        try {
            const data = await api.get(`/financier/debts/find-candidates?fio=${encodeURIComponent(fio)}&limit=15`);
            autoSuggestHtml = this._nfRenderCandidates(data.candidates || [], 'Похожие по импорту');
            results.innerHTML = autoSuggestHtml;
        } catch (e) {
            results.innerHTML = `<div style="color:#b91c1c; padding:8px;">Ошибка: ${esc(e.message)}</div>`;
        }

        // 3) Debounced ручной поиск
        let searchTimer = null;
        input.addEventListener('input', () => {
            clearTimeout(searchTimer);
            const q = input.value.trim();
            if (q.length < 2) {
                // Возвращаем auto-suggest по fio
                results.innerHTML = autoSuggestHtml;
                return;
            }
            results.innerHTML = `
                <div style="text-align:center; padding:14px; color:var(--text-secondary);">
                    <i class="fa-solid fa-spinner fa-spin"></i> Ищу «${esc(q)}»…
                </div>`;
            searchTimer = setTimeout(async () => {
                try {
                    const data = await api.get(`/financier/debts/find-candidates?q=${encodeURIComponent(q)}&limit=20`);
                    results.innerHTML = this._nfRenderCandidates(data.candidates || [], `Найдено по «${esc(q)}»`);
                } catch (err) {
                    results.innerHTML = `<div style="color:#b91c1c; padding:8px;">Ошибка: ${esc(err.message)}</div>`;
                }
            }, 250);
        });
    },

    async _nfPickCandidate(row, fio, logId, accountType, userId, username) {
        const { debt, overpayment } = this._nfGetSums(row);
        const fd = new FormData();
        fd.append('fio', fio);
        fd.append('user_id', String(userId));
        fd.append('debt', String(debt));
        fd.append('overpayment', String(overpayment));
        try {
            await api.post(`/financier/debts/import-history/${logId}/reassign`, fd);
            toast(`Привязано: ${fio} → ${username}`, 'success');
            row.remove();
            this.reload();
            this.loadImportHistory();
        } catch (e) {
            toast('Ошибка: ' + e.message, 'error');
        }
    },

    _nfShowCreateForm(row, fio, logId, accountType) {
        const pane = row.querySelector('[data-nf-pane="create"]');
        this._nfShowPane(row, 'create');
        // Генерим читаемый пароль (без 0/O, 1/l/I).
        const chars = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789';
        const arr = new Uint8Array(12);
        (window.crypto || window.msCrypto).getRandomValues(arr);
        let pwd = ''; for (let i = 0; i < 12; i++) pwd += chars[arr[i] % chars.length];
        pane.innerHTML = `
            <div style="font-size:11px; color:#166534; margin-bottom:8px; text-transform:uppercase;">
                Создать нового жильца + сразу записать долг
            </div>
            <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px;">
                <input type="text" data-nf-login placeholder="Логин (для входа)" value="${esc(fio)}" style="font-size:13px;">
                <input type="text" data-nf-pwd placeholder="Пароль" value="${pwd}" style="font-size:13px; font-family:monospace;">
                <input type="text" data-nf-dorm placeholder="Общежитие" style="font-size:13px;">
                <input type="text" data-nf-room placeholder="Номер комнаты" style="font-size:13px;">
                <input type="number" data-nf-residents value="1" min="1" max="20" placeholder="Жильцов в семье" style="font-size:13px;">
                <select data-nf-type style="font-size:13px;">
                    <option value="family">Семья (по счётчику)</option>
                    <option value="single">Одиночка (per capita)</option>
                </select>
            </div>
            <div style="margin-top:8px;">
                <button data-nf-action="submit-create" class="action-btn success-btn" style="padding:6px 12px; font-size:12px;">
                    <i class="fa-solid fa-check"></i> Создать и привязать
                </button>
            </div>
        `;
    },

    async _nfSubmitCreate(row, fio, logId, accountType) {
        const { debt, overpayment } = this._nfGetSums(row);
        const pane = row.querySelector('[data-nf-pane="create"]');
        const login = pane.querySelector('[data-nf-login]').value.trim();
        const password = pane.querySelector('[data-nf-pwd]').value.trim();
        const dorm = pane.querySelector('[data-nf-dorm]').value.trim();
        const roomNo = pane.querySelector('[data-nf-room]').value.trim();
        const residents = Number(pane.querySelector('[data-nf-residents]').value) || 1;
        const type = pane.querySelector('[data-nf-type]').value;

        if (!login || login.length < 3) return toast('Логин минимум 3 символа', 'warning');
        if (!password || password.length < 6) return toast('Пароль минимум 6 символов', 'warning');
        if (!dorm || !roomNo) return toast('Укажите общежитие и номер комнаты', 'warning');

        try {
            await api.post(`/financier/debts/import-history/${logId}/create-and-match`, {
                fio,
                username: login,
                password,
                dormitory_name: dorm,
                room_number: roomNo,
                debt,
                overpayment,
                residents_count: residents,
                resident_type: type,
            });
            toast(`Создан жилец «${login}», долг ${debt} ₽ записан`, 'success');
            row.remove();
            this.reload();
            this.loadImportHistory();
        } catch (e) {
            toast('Ошибка: ' + e.message, 'error');
        }
    },

    _nfShowLegacyForm(row, fio, logId, accountType) {
        const pane = row.querySelector('[data-nf-pane="legacy"]');
        this._nfShowPane(row, 'legacy');
        pane.innerHTML = `
            <div style="display:flex; gap:6px; align-items:center;">
                <input type="text" data-nf-legacy-login placeholder="Логин жильца" style="flex:1; font-size:13px;" autocomplete="off">
                <button data-nf-action="submit-legacy" class="action-btn primary-btn" style="padding:5px 10px; font-size:12px;">
                    <i class="fa-solid fa-link"></i> Привязать
                </button>
            </div>
            <div style="font-size:11px; color:var(--text-secondary); margin-top:4px;">
                Подходит когда знаешь точный логин жильца — поиск по точному совпадению.
            </div>
        `;
    },

    async _nfSubmitLegacy(row, fio, logId, accountType) {
        const { debt, overpayment } = this._nfGetSums(row);
        const pane = row.querySelector('[data-nf-pane="legacy"]');
        const login = pane.querySelector('[data-nf-legacy-login]').value.trim();
        if (!login) return toast('Укажите логин', 'warning');
        try {
            const userSearch = await api.get(`/users?page=1&limit=5&search=${encodeURIComponent(login)}`);
            const exact = (userSearch.items || []).find(u => u.username.toLowerCase() === login.toLowerCase());
            if (!exact) return toast(`Жилец «${login}» не найден`, 'error');

            const fd = new FormData();
            fd.append('fio', fio);
            fd.append('user_id', String(exact.id));
            fd.append('debt', String(debt));
            fd.append('overpayment', String(overpayment));
            await api.post(`/financier/debts/import-history/${logId}/reassign`, fd);
            toast(`Привязано: ${fio} → ${login}`, 'success');
            row.remove();
            this.reload();
            this.loadImportHistory();
        } catch (e) {
            toast('Ошибка: ' + e.message, 'error');
        }
    },

    closeNotFoundModal() {
        this.dom.notFoundModal?.classList.remove('open');
    },

    async resetUserBalance(userId, username) {
        if (!await showConfirm(
            `Сбросить баланс жильца «${username}»?\n\n` +
            'Будут обнулены debt_209, debt_205, overpayment_209, overpayment_205 у ВСЕХ ' +
            'его reading-ов (во всех периодах). Действие можно отменить только через ' +
            'журнал действий (audit_log).\n\n' +
            'Используйте только если после отката импорта у жильца остались зависшие сальдо.',
            { title: 'Сброс баланса', confirmText: 'Сбросить', danger: true }
        )) return;
        try {
            const res = await api.post(`/financier/users/${userId}/reset-balance`);
            if (res.status === 'noop') {
                toast(`У ${username} баланс уже пустой`, 'info');
            } else {
                toast(`Сброшено reading-ов: ${res.reset_count}`, 'success');
            }
            this.reload();
            this.loadStats();
        } catch (e) {
            toast('Ошибка: ' + e.message, 'error');
        }
    },

    // ==========================================================================
    // DIFF МОДАЛКА — сравнение импорта с предыдущим того же счёта
    //
    // Открывается из кнопки «Diff» в строке истории импортов. Backend
    // /diff отдаёт 5 категорий жильцов; рисуем 5 collapsible-секций.
    // ==========================================================================
    async openDiffModal(logId) {
        // Overlay + skeleton сразу — чтобы юзер видел что клик сработал.
        const old = document.getElementById('debtDiffModal');
        if (old) old.remove();
        const modal = document.createElement('div');
        modal.id = 'debtDiffModal';
        modal.style.cssText = `
            position:fixed; inset:0; background:rgba(0,0,0,0.45); z-index:1000;
            display:flex; align-items:center; justify-content:center; padding:20px;`;
        modal.innerHTML = `
            <div style="background:var(--bg-card); border-radius:12px; max-width:1100px; width:100%;
                        max-height:90vh; display:flex; flex-direction:column; box-shadow:0 12px 40px rgba(0,0,0,0.3);">
                <div style="padding:14px 20px; border-bottom:1px solid var(--border-color);
                            display:flex; align-items:center; justify-content:space-between;">
                    <h3 style="margin:0; font-size:15px;">
                        <i class="fa-solid fa-code-compare" style="color:#4338ca;"></i>
                        Сравнение импорта №${logId}
                    </h3>
                    <button class="secondary-btn" data-close-diff style="padding:6px 12px;">
                        <i class="fa-solid fa-xmark"></i> Закрыть
                    </button>
                </div>
                <div id="debtDiffBody" style="padding:14px 20px; overflow:auto; flex:1;">
                    <div style="text-align:center; padding:40px; color:var(--text-secondary);">
                        <i class="fa-solid fa-spinner fa-spin"></i> Загрузка diff…
                    </div>
                </div>
            </div>`;
        document.body.appendChild(modal);
        modal.addEventListener('click', (e) => {
            if (e.target === modal || e.target.closest('[data-close-diff]')) modal.remove();
        });
        const escHandler = (e) => { if (e.key === 'Escape') { modal.remove(); document.removeEventListener('keydown', escHandler); } };
        document.addEventListener('keydown', escHandler);

        try {
            const data = await api.get(`/financier/debts/import-history/${logId}/diff`);
            this._renderDiff(data);
        } catch (e) {
            const body = document.getElementById('debtDiffBody');
            if (body) body.innerHTML = `
                <div style="padding:16px; background:#fef2f2; border:1px solid #fecaca; border-radius:8px; color:#991b1b;">
                    Ошибка загрузки: ${esc(e.message)}
                </div>`;
        }
    },

    _renderDiff(data) {
        const body = document.getElementById('debtDiffBody');
        if (!body) return;

        if (data.fatal) {
            body.innerHTML = `
                <div style="padding:30px; text-align:center; color:var(--text-secondary);">
                    <i class="fa-solid fa-circle-info" style="font-size:24px; color:#3b82f6;"></i>
                    <div style="margin-top:10px;">${esc(data.fatal)}</div>
                </div>`;
            return;
        }

        const s = data.summary || {};
        const acc = data.account_type;
        const prevDate = data.previous_started_at
            ? new Date(data.previous_started_at).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric' })
            : '—';
        const curDate = data.current_started_at
            ? new Date(data.current_started_at).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric' })
            : '—';

        const header = `
            <div style="margin-bottom:18px; padding:12px 14px; background:#f9fafb; border:1px solid var(--border-color); border-radius:8px; font-size:13px;">
                <div style="margin-bottom:6px;">
                    <b>Счёт ${esc(acc)}:</b> сравнение
                    <span style="color:var(--text-secondary);">№${data.previous_id} (${esc(prevDate)})</span>
                    <i class="fa-solid fa-arrow-right" style="margin:0 6px;"></i>
                    <b>№${data.current_id} (${esc(curDate)})</b>
                </div>
                <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(160px, 1fr)); gap:8px; margin-top:8px;">
                    <div style="padding:8px 10px; background:#fef2f2; border-radius:6px; border:1px solid #fecaca;">
                        <div style="font-size:11px; color:#991b1b; text-transform:uppercase;">Новые должники</div>
                        <div style="font-size:18px; font-weight:700; color:#dc2626;">${s.new_debtors_count || 0}</div>
                    </div>
                    <div style="padding:8px 10px; background:#fff7ed; border-radius:6px; border:1px solid #fed7aa;">
                        <div style="font-size:11px; color:#9a3412; text-transform:uppercase;">Долг вырос</div>
                        <div style="font-size:18px; font-weight:700; color:#ea580c;">${s.debt_grew_count || 0}</div>
                        <div style="font-size:11px; color:var(--text-secondary);">+${fmtMoney(s.sum_new_and_grew || 0)}</div>
                    </div>
                    <div style="padding:8px 10px; background:#f0fdf4; border-radius:6px; border:1px solid #bbf7d0;">
                        <div style="font-size:11px; color:#166534; text-transform:uppercase;">Долг упал</div>
                        <div style="font-size:18px; font-weight:700; color:#16a34a;">${s.debt_dropped_count || 0}</div>
                        <div style="font-size:11px; color:var(--text-secondary);">−${fmtMoney(s.sum_dropped || 0)}</div>
                    </div>
                    <div style="padding:8px 10px; background:#ecfdf5; border-radius:6px; border:1px solid #a7f3d0;">
                        <div style="font-size:11px; color:#065f46; text-transform:uppercase;">Долг закрыт</div>
                        <div style="font-size:18px; font-weight:700; color:#10b981;">${s.debt_closed_count || 0}</div>
                        <div style="font-size:11px; color:var(--text-secondary);">−${fmtMoney(s.sum_closed || 0)}</div>
                    </div>
                    <div style="padding:8px 10px; background:#ede9fe; border-radius:6px; border:1px solid #ddd6fe;">
                        <div style="font-size:11px; color:#5b21b6; text-transform:uppercase;">Новые переплаты</div>
                        <div style="font-size:18px; font-weight:700; color:#7c3aed;">${s.new_overpay_count || 0}</div>
                    </div>
                </div>
            </div>`;

        const sec = (title, items, kind) => {
            if (!items || !items.length) return '';
            const colorMap = {
                new_debtors: '#dc2626',
                debt_grew:   '#ea580c',
                debt_dropped:'#16a34a',
                debt_closed: '#10b981',
                new_overpay: '#7c3aed',
            };
            const c = colorMap[kind] || '#6b7280';
            const rows = items.map(it => {
                const valueCell = kind === 'new_overpay'
                    ? `<td style="text-align:right; font-weight:600; color:${c};">${fmtMoney(it.overpayment)}</td>`
                    : `<td style="text-align:right; color:var(--text-secondary);">${fmtMoney(it.prev_debt)}</td>
                       <td style="text-align:right; font-weight:600;">${fmtMoney(it.current_debt)}</td>
                       <td style="text-align:right; font-weight:600; color:${c};">${it.delta >= 0 ? '+' : ''}${fmtMoney(it.delta)}</td>`;
                return `
                    <tr style="border-bottom:1px solid var(--border-color);">
                        <td style="padding:6px 10px;">${esc(it.username)}</td>
                        <td style="padding:6px 10px; color:var(--text-secondary); font-size:11px;">${esc(it.room_label)}</td>
                        ${valueCell}
                    </tr>`;
            }).join('');
            const headers = kind === 'new_overpay'
                ? '<th style="text-align:left; padding:6px 10px;">Жилец</th><th style="text-align:left; padding:6px 10px;">Комната</th><th style="text-align:right; padding:6px 10px;">Переплата</th>'
                : '<th style="text-align:left; padding:6px 10px;">Жилец</th><th style="text-align:left; padding:6px 10px;">Комната</th><th style="text-align:right; padding:6px 10px;">Было</th><th style="text-align:right; padding:6px 10px;">Стало</th><th style="text-align:right; padding:6px 10px;">Δ</th>';
            return `
                <details style="margin-bottom:14px; border:1px solid var(--border-color); border-radius:8px; overflow:hidden;" open>
                    <summary style="padding:10px 14px; cursor:pointer; background:${c}11; color:${c}; font-weight:600; font-size:13px;">
                        ${esc(title)} (${items.length})
                    </summary>
                    <table style="width:100%; border-collapse:collapse; font-size:12px;">
                        <thead style="background:var(--bg-page); font-size:11px; color:var(--text-secondary); text-transform:uppercase;">
                            <tr>${headers}</tr>
                        </thead>
                        <tbody>${rows}</tbody>
                    </table>
                </details>`;
        };

        body.innerHTML = header
            + sec('Новые должники', data.new_debtors, 'new_debtors')
            + sec('Долг вырос', data.debt_grew, 'debt_grew')
            + sec('Долг упал', data.debt_dropped, 'debt_dropped')
            + sec('Долг закрыт', data.debt_closed, 'debt_closed')
            + sec('Появились переплаты', data.new_overpay, 'new_overpay');

        // Если все секции пусты — показать «всё то же самое»
        if (!data.new_debtors?.length && !data.debt_grew?.length
            && !data.debt_dropped?.length && !data.debt_closed?.length
            && !data.new_overpay?.length) {
            body.innerHTML = header + `
                <div style="text-align:center; padding:40px; color:var(--text-secondary);">
                    <i class="fa-solid fa-equals" style="font-size:24px; color:#10b981;"></i>
                    <div style="margin-top:10px;">Изменений нет — суммы совпадают с прошлым импортом.</div>
                </div>`;
        }
    },

    // ==========================================================================
    // КАРТОЧКА ЖИЛЬЦА — полная раскладка долга по клику на ФИО (Bug AI)
    // ==========================================================================
    /** Модалка по клику на ФИО в таблице «Долги 1С».
     *  Показывает построчно по 209 и 205:
     *    Был долг (начало) → Доначислено → Оплачено → Стало (конец)
     *  + быстрые действия: 📊 история через все импорты, 🔍 поиск в архивах,
     *  ✏ корректировка, 🧹 сброс баланса.
     *
     *  Аргумент u — объект из таблицы со всеми полями (debt_209, obor_*, etc).
     */
    openUserCard(u) {
        document.getElementById('debtUserCardModal')?.remove();
        const d209 = parseFloat(u.debt_209 || 0), o209 = parseFloat(u.overpayment_209 || 0);
        const d205 = parseFloat(u.debt_205 || 0), o205 = parseFloat(u.overpayment_205 || 0);
        const od209 = parseFloat(u.obor_debit_209 || 0), oc209 = parseFloat(u.obor_credit_209 || 0);
        const od205 = parseFloat(u.obor_debit_205 || 0), oc205 = parseFloat(u.obor_credit_205 || 0);
        // Старт = end + Кр_оборот − Дт_оборот (обратное вычисление по дебетовому счёту 209/205).
        const start209 = d209 + oc209 - od209;
        const start205 = d205 + oc205 - od205;
        // Единый адрес для общаги и дома (E2-A): у домов dormitory_name/room_number
        // NULL, formatRoomAddress сам соберёт «ул. X, д. Y, кв. Z».
        const room = u.room ? formatRoomAddress(u.room) : '—';

        const f = (v) => {
            const abs = Math.abs(v);
            if (abs < 0.005) return '0,00';
            return v.toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
        };

        // Helper: рендерит одну секцию счёта (209 или 205) с раскладкой.
        const accountSection = (label, color, startD, endD, oborD, oborC, endO) => {
            const hadDebt = startD > 0.005;
            const hasDebtNow = endD > 0.005;
            const hasOverpayNow = endO > 0.005;
            const noMovement = oborD < 0.005 && oborC < 0.005;
            const noDebtNoMovement = !hadDebt && !hasDebtNow && !hasOverpayNow && noMovement;

            let verdictText, verdictColor, verdictIcon;
            if (noDebtNoMovement) {
                verdictText = 'нет данных из 1С';
                verdictColor = '#9ca3af';
                verdictIcon = '·';
            } else if (hadDebt && !hasDebtNow && oborC > 0) {
                verdictText = 'погашен полностью';
                verdictColor = '#15803d';
                verdictIcon = '✓';
            } else if (hadDebt && hasDebtNow && oborC > 0) {
                verdictText = `оплачено частично (осталось ${f(endD)} ₽)`;
                verdictColor = '#a16207';
                verdictIcon = '⚠';
            } else if (endD > startD + 0.005) {
                verdictText = `долг вырос на ${f(endD - startD)} ₽`;
                verdictColor = '#b91c1c';
                verdictIcon = '↑';
            } else if (hasDebtNow && noMovement) {
                verdictText = 'без движения — долг не оплачивался';
                verdictColor = '#b91c1c';
                verdictIcon = '!';
            } else if (hasOverpayNow) {
                verdictText = `переплата ${f(endO)} ₽`;
                verdictColor = '#15803d';
                verdictIcon = '+';
            } else {
                verdictText = '0 ₽';
                verdictColor = '#15803d';
                verdictIcon = '✓';
            }

            const row = (k, v, vColor, vSign) => `
                <tr>
                    <td style="padding:6px 0; color:#6b7280; font-size:12px;">${k}</td>
                    <td style="padding:6px 0; text-align:right; font-weight:600; color:${vColor || '#111827'}; font-variant-numeric:tabular-nums;">${vSign || ''}${f(v)} ₽</td>
                </tr>`;

            return `
                <div style="border:1px solid var(--border-color); border-radius:8px; padding:14px; background:#fff;">
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
                        <h4 style="margin:0; font-size:14px; color:${color};">${label}</h4>
                        <span style="font-size:11px; padding:3px 8px; background:${verdictColor}15; color:${verdictColor}; border-radius:12px; font-weight:600;">${verdictIcon} ${verdictText}</span>
                    </div>
                    <table style="width:100%; border-collapse:collapse;">
                        ${row('Долг на начало периода', startD, startD > 0 ? '#b91c1c' : '#9ca3af')}
                        ${oborD > 0.005 ? row('+ Доначислили за период', oborD, '#b91c1c', '+') : ''}
                        ${oborC > 0.005 ? row('− Оплачено за период', oborC, '#15803d', '−') : ''}
                        <tr><td colspan="2" style="border-bottom:1px dashed #e5e7eb; padding:2px 0;"></td></tr>
                        ${row('Долг на конец периода', endD, endD > 0 ? '#b91c1c' : '#15803d')}
                        ${endO > 0.005 ? row('Переплата на конец', endO, '#15803d', '+') : ''}
                    </table>
                </div>
            `;
        };

        const modal = document.createElement('div');
        modal.id = 'debtUserCardModal';
        modal.style.cssText = 'position:fixed; inset:0; background:rgba(0,0,0,0.45); z-index:1000; display:flex; align-items:center; justify-content:center; padding:20px;';
        modal.innerHTML = `
            <div style="background:#f9fafb; border-radius:12px; max-width:720px; width:100%; max-height:90vh; display:flex; flex-direction:column; box-shadow:0 12px 40px rgba(0,0,0,0.3);">
                <div style="padding:14px 20px; border-bottom:1px solid var(--border-color); background:#fff; border-radius:12px 12px 0 0; display:flex; align-items:center; justify-content:space-between;">
                    <div>
                        <h3 style="margin:0; font-size:16px;">${esc(u.username)}</h3>
                        <div style="font-size:12px; color:#6b7280; margin-top:2px;">
                            ID ${u.id} · ${esc(room)}
                        </div>
                    </div>
                    <button data-close-card style="background:none; border:none; font-size:20px; color:#6b7280; cursor:pointer;">×</button>
                </div>
                <div style="padding:16px 20px; overflow-y:auto; flex:1; display:flex; flex-direction:column; gap:12px;">
                    ${accountSection('209 — Коммуналка', '#c0392b', start209, d209, od209, oc209, o209)}
                    ${accountSection('205 — Найм', '#d35400', start205, d205, od205, oc205, o205)}

                    <div style="font-size:11px; color:#6b7280; padding:8px 12px; background:#fffbeb; border-left:3px solid #fbbf24; border-radius:4px;">
                        💡 «Долг на начало» считается обратно: <code>конец + оплачено − доначислено</code>.
                        Если в строке «Доначислили» или «Оплачено» нет — значит в этом периоде по этому счёту движения не было.
                    </div>
                </div>
                <div style="padding:12px 20px; border-top:1px solid var(--border-color); background:#fff; border-radius:0 0 12px 12px; display:flex; gap:8px; flex-wrap:wrap; justify-content:flex-end;">
                    <button data-card-action="history" style="padding:8px 12px; background:#fff; color:#4338ca; border:1px solid #c7d2fe; border-radius:4px; cursor:pointer; font-size:12px;">
                        <i class="fa-solid fa-chart-line"></i> История через импорты
                    </button>
                    <button data-card-action="coverage" style="padding:8px 12px; background:#fff; color:#0ea5e9; border:1px solid #bae6fd; border-radius:4px; cursor:pointer; font-size:12px;">
                        <i class="fa-solid fa-magnifying-glass"></i> Найти в архивах 1С
                    </button>
                    <button data-card-action="reset" style="padding:8px 12px; background:#fff; color:#b91c1c; border:1px solid #fecaca; border-radius:4px; cursor:pointer; font-size:12px;">
                        <i class="fa-solid fa-broom"></i> Сбросить баланс
                    </button>
                    <button data-card-action="adjust" style="padding:8px 14px; background:#6366f1; color:#fff; border:none; border-radius:4px; cursor:pointer; font-size:12px; font-weight:600;">
                        <i class="fa-solid fa-pen"></i> Корректировка
                    </button>
                </div>
            </div>
        `;
        document.body.appendChild(modal);
        const close = () => modal.remove();
        modal.querySelector('[data-close-card]').addEventListener('click', close);
        modal.addEventListener('click', (e) => { if (e.target === modal) close(); });
        modal.querySelectorAll('[data-card-action]').forEach(btn => {
            btn.addEventListener('click', () => {
                const action = btn.getAttribute('data-card-action');
                close();
                if (action === 'history') this.openUserDebtHistory(u.id, u.username);
                else if (action === 'coverage') this.openCheckCoverage(u.id, u.username);
                else if (action === 'reset') this.resetUserBalance(u.id, u.username);
                else if (action === 'adjust') this.openAdjustModal(u.id, u.username);
            });
        });
    },

    // ==========================================================================
    // ИСТОРИЯ ДОЛГОВ ЖИЛЬЦА — sparkline 209 + 205 через все импорты
    // ==========================================================================
    async openUserDebtHistory(userId, username) {
        document.getElementById('debtUserHistoryModal')?.remove();
        const modal = document.createElement('div');
        modal.id = 'debtUserHistoryModal';
        modal.style.cssText = `
            position:fixed; inset:0; background:rgba(0,0,0,0.45); z-index:1000;
            display:flex; align-items:center; justify-content:center; padding:20px;`;
        modal.innerHTML = `
            <div style="background:var(--bg-card); border-radius:12px; max-width:820px; width:100%;
                        max-height:85vh; display:flex; flex-direction:column; box-shadow:0 12px 40px rgba(0,0,0,0.3);">
                <div style="padding:14px 20px; border-bottom:1px solid var(--border-color);
                            display:flex; align-items:center; justify-content:space-between;">
                    <h3 style="margin:0; font-size:15px;">
                        <i class="fa-solid fa-chart-line" style="color:#4338ca;"></i>
                        История долгов: ${esc(username)}
                    </h3>
                    <button class="secondary-btn" data-close-uh style="padding:6px 12px;">
                        <i class="fa-solid fa-xmark"></i> Закрыть
                    </button>
                </div>
                <div id="debtUserHistoryBody" style="padding:14px 20px; overflow:auto; flex:1;">
                    <div style="text-align:center; padding:40px; color:var(--text-secondary);">
                        <i class="fa-solid fa-spinner fa-spin"></i> Загрузка…
                    </div>
                </div>
            </div>`;
        document.body.appendChild(modal);
        modal.addEventListener('click', (e) => {
            if (e.target === modal || e.target.closest('[data-close-uh]')) modal.remove();
        });
        const escHandler = (e) => { if (e.key === 'Escape') { modal.remove(); document.removeEventListener('keydown', escHandler); } };
        document.addEventListener('keydown', escHandler);

        try {
            const data = await api.get(`/financier/debts/user-debt-history/${userId}`);
            this._renderUserDebtHistory(data);
        } catch (e) {
            const body = document.getElementById('debtUserHistoryBody');
            if (body) body.innerHTML = `
                <div style="padding:16px; background:#fef2f2; border:1px solid #fecaca; border-radius:8px; color:#991b1b;">
                    Ошибка: ${esc(e.message)}
                </div>`;
        }
    },

    _renderUserDebtHistory(data) {
        const body = document.getElementById('debtUserHistoryBody');
        if (!body) return;

        if (data.fatal) {
            body.innerHTML = `
                <div style="padding:30px; text-align:center; color:var(--text-secondary);">
                    <i class="fa-solid fa-circle-info" style="font-size:24px; color:#f59e0b;"></i>
                    <div style="margin-top:10px;">${esc(data.fatal)}</div>
                </div>`;
            return;
        }
        if (!data.points || !data.points.length) {
            body.innerHTML = `
                <div style="padding:30px; text-align:center; color:var(--text-secondary);">
                    Жилец не встречался ни в одном импорте — долгов нет.
                </div>`;
            return;
        }

        // Разделяем точки на 209 / 205, рисуем 2 sparkline + table
        const points209 = data.points.filter(p => p.account_type === '209');
        const points205 = data.points.filter(p => p.account_type === '205');

        // SVG sparkline — высота 60, ширина ~600
        const renderSpark = (pts, color, account) => {
            if (!pts.length) {
                return `<div style="color:var(--text-secondary); font-size:12px; padding:14px;">${account}: данных нет</div>`;
            }
            const W = 580, H = 60, P = 20;
            const debts = pts.map(p => p.debt);
            const maxD = Math.max(...debts, 1);
            const step = pts.length > 1 ? (W - 2 * P) / (pts.length - 1) : 0;
            const polyline = pts.map((p, i) => {
                const x = P + i * step;
                const y = H - P - (p.debt / maxD) * (H - 2 * P);
                return `${x.toFixed(1)},${y.toFixed(1)}`;
            }).join(' ');
            const last = pts[pts.length - 1];
            return `
                <div style="margin-bottom:12px;">
                    <div style="font-size:12px; color:var(--text-secondary); margin-bottom:4px;">
                        Счёт <b>${account}</b>: ${pts.length} точек, max ${fmtMoney(maxD)}, последний долг <b style="color:${color};">${fmtMoney(last.debt)}</b>
                    </div>
                    <svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" style="border:1px solid var(--border-color); border-radius:6px; background:#fafafa; display:block;">
                        <polyline fill="none" stroke="${color}" stroke-width="2" points="${polyline}"/>
                        ${pts.map((p, i) => {
                            const x = P + i * step;
                            const y = H - P - (p.debt / maxD) * (H - 2 * P);
                            return `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="3" fill="${color}">
                                      <title>${new Date(p.started_at).toLocaleDateString('ru-RU')}: ${fmtMoney(p.debt)}</title>
                                    </circle>`;
                        }).join('')}
                    </svg>
                </div>`;
        };

        const tableRows = data.points
            .slice()
            .reverse()  // самый свежий импорт сверху
            .map(p => `
                <tr style="border-bottom:1px solid var(--border-color);">
                    <td style="padding:6px 10px; font-size:12px;">${new Date(p.started_at).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric' })}</td>
                    <td style="padding:6px 10px;"><span style="background:${p.account_type === '209' ? '#dbeafe' : '#fef3c7'}; color:${p.account_type === '209' ? '#1e40af' : '#92400e'}; padding:2px 6px; border-radius:4px; font-size:11px; font-weight:600;">${p.account_type}</span></td>
                    <td style="padding:6px 10px; text-align:right; font-weight:600; color:${p.debt > 0 ? '#dc2626' : 'var(--text-secondary)'};">${p.debt > 0 ? fmtMoney(p.debt) : '—'}</td>
                    <td style="padding:6px 10px; text-align:right; color:${p.overpayment > 0 ? '#7c3aed' : 'var(--text-secondary)'};">${p.overpayment > 0 ? fmtMoney(p.overpayment) : '—'}</td>
                    <td style="padding:6px 10px; color:var(--text-secondary); font-size:11px;">${esc(p.file_name || '—')}</td>
                </tr>`).join('');

        body.innerHTML = `
            <div style="margin-bottom:10px; color:var(--text-secondary); font-size:12px;">
                Комната: <b>${esc(data.room_label || '—')}</b> ·
                ${data.points.length} ${data.points.length === 1 ? 'импорт' : (data.points.length < 5 ? 'импорта' : 'импортов')}
            </div>
            ${renderSpark(points209, '#dc2626', '209 (Коммуналка)')}
            ${renderSpark(points205, '#ea580c', '205 (Найм)')}
            <table style="width:100%; margin-top:14px; border-collapse:collapse; font-size:13px;">
                <thead style="background:var(--bg-page); font-size:11px; color:var(--text-secondary); text-transform:uppercase;">
                    <tr>
                        <th style="text-align:left; padding:6px 10px;">Дата</th>
                        <th style="text-align:left; padding:6px 10px;">Счёт</th>
                        <th style="text-align:right; padding:6px 10px;">Долг</th>
                        <th style="text-align:right; padding:6px 10px;">Переплата</th>
                        <th style="text-align:left; padding:6px 10px;">Файл</th>
                    </tr>
                </thead>
                <tbody>${tableRows}</tbody>
            </table>`;
    },
};
