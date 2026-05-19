/**
 * Экран подачи показаний — mobile-first.
 *
 * Источники данных:
 *   - GET /api/readings/state — текущие показания, статус периода,
 *     has_X_meter (см. meters_001_per_user_config), per_capita_amount,
 *     инструкции формата ввода счётчика.
 *   - POST /api/calculate — отправка показаний.
 *
 * UX отличия от старого портала:
 *   - Крупные input'ы (24px font), числовая клавиатура (inputmode=decimal)
 *   - Карточки счётчиков — стек один под другим, не grid (легче на узком экране)
 *   - Если у жильца нет счётчика конкретного ресурса — карточка скрыта
 *   - Per-capita режим (койко-место) — форма не показывается вообще,
 *     только сумма к оплате и кнопка «Закрыть/Перейти»
 *   - Sticky кнопка отправки внизу
 */
import { api, formatMoney } from '../api.js';
import { toast } from '../toast.js';

let _state = null;  // последний загруженный state с сервера

export async function renderReadings(root) {
    root.innerHTML = renderSkeleton();
    try {
        _state = await api.get('/readings/state');
    } catch (e) {
        toast.error('Не удалось загрузить состояние: ' + e.message);
        root.innerHTML = renderError(e.message);
        return;
    }
    root.innerHTML = renderForm(_state);
    bindForm(root);
}

function renderSkeleton() {
    return `
        <div class="screen">
            <h2 style="font-size:22px; font-weight:700; margin-bottom: var(--sp-3);">Показания счётчиков</h2>
            <div class="skeleton skeleton--card" style="height:80px;"></div>
            <div class="skeleton skeleton--card" style="height:140px;"></div>
            <div class="skeleton skeleton--card" style="height:140px;"></div>
            <div class="skeleton skeleton--card" style="height:140px;"></div>
        </div>
    `;
}

function renderError(msg) {
    return `
        <div class="screen">
            <h2 style="font-size:22px; font-weight:700; margin-bottom: var(--sp-3);">Показания счётчиков</h2>
            <div class="status-box status-box--locked">
                <div class="status-box__title">⚠ Ошибка</div>
                <div class="status-box__sub">${escapeHtml(msg)}</div>
            </div>
        </div>
    `;
}

function renderForm(state) {
    // ── Per-capita (койко-место): счётчики не подаются ────────────────
    if (state.billing_mode === 'per_capita') {
        const amount = Number(state.per_capita_amount || 0);
        return `
            <div class="screen">
                <h2 style="font-size:22px; font-weight:700; margin-bottom: var(--sp-3);">Текущий период</h2>
                <div class="card card--primary">
                    <div class="card__label">К оплате</div>
                    <div class="balance">
                        <div class="balance__amount">${formatMoney(amount)} <span class="balance__currency">₽</span></div>
                        <div class="balance__sub">Фиксированная сумма за койко-место</div>
                    </div>
                </div>
                <div class="status-box status-box--info">
                    <div class="status-box__title">ℹ️ Подача счётчиков не требуется</div>
                    <div class="status-box__sub">
                        Вы оформлены на «койко-место» — сумма к оплате фиксированная и берётся из тарифа.
                        Счётчики ГВС/ХВС/электричества подавать не нужно.
                    </div>
                </div>
            </div>
        `;
    }

    // ── Обычный режим (by_meter) ──────────────────────────────────────
    const periodName = state.period_name || 'Текущий период';
    const status = renderStatus(state);
    const meters = renderMeters(state);

    const submitDisabled = !state.is_period_open || state.is_already_approved;
    const submitLabel = state.is_already_approved
        ? 'Утверждено'
        : (state.is_draft ? 'Обновить показания' : 'Отправить показания');

    return `
        <div class="screen">
            <h2 style="font-size:22px; font-weight:700; margin-bottom: var(--sp-2);">${escapeHtml(periodName)}</h2>
            ${status}
            ${state.meter_instructions ? `
                <div class="status-box status-box--info" style="margin-bottom: var(--sp-4);">
                    <div class="status-box__title">📋 Как вводить показания</div>
                    <div class="status-box__sub">${escapeHtml(state.meter_instructions)}</div>
                    ${state.meter_example ? `<div style="margin-top:6px; font-family:monospace; color:var(--text-secondary); font-size:13px;">Пример: <b>${escapeHtml(state.meter_example)}</b></div>` : ''}
                </div>
            ` : ''}

            <form id="readingsForm" class="meter-cards" novalidate ${submitDisabled ? 'aria-disabled="true"' : ''}>
                ${meters}
            </form>

            <button id="submitReadings" class="btn btn--primary" type="button" ${submitDisabled ? 'disabled' : ''}>
                ${escapeHtml(submitLabel)}
            </button>
        </div>
    `;
}

function renderStatus(state) {
    if (state.is_already_approved) {
        return `<div class="status-box status-box--locked">
            <div class="status-box__title">🔒 Показания утверждены</div>
            <div class="status-box__sub">Бухгалтерия их уже приняла. Изменения недоступны.</div>
        </div>`;
    }
    if (!state.is_period_open) {
        return `<div class="status-box status-box--locked">
            <div class="status-box__title">🔒 Приём показаний закрыт</div>
            <div class="status-box__sub">Дождитесь начала следующего периода подачи.</div>
        </div>`;
    }
    if (state.is_draft) {
        return `<div class="status-box status-box--draft">
            <div class="status-box__title">✏️ Черновик сохранён</div>
            <div class="status-box__sub">Вы можете уточнить значения до конца периода.</div>
        </div>`;
    }
    return `<div class="status-box status-box--open">
        <div class="status-box__title">🟢 Приём показаний открыт</div>
        <div class="status-box__sub">Введите текущие значения со счётчиков.</div>
    </div>`;
}

function renderMeters(state) {
    const cards = [];
    // ГВС
    if (state.has_hw_meter !== false) {
        cards.push(meterCard({
            key: 'hot',
            icon: '🔥',
            iconClass: 'meter-card__icon--hot',
            title: 'Горячая вода',
            unit: 'м³',
            prev: state.prev_hot,
            current: state.current_hot,
            disabled: !state.is_period_open || state.is_already_approved,
        }));
    }
    // ХВС
    if (state.has_cw_meter !== false) {
        cards.push(meterCard({
            key: 'cold',
            icon: '❄️',
            iconClass: 'meter-card__icon--cold',
            title: 'Холодная вода',
            unit: 'м³',
            prev: state.prev_cold,
            current: state.current_cold,
            disabled: !state.is_period_open || state.is_already_approved,
        }));
    }
    // Электричество
    if (state.has_el_meter !== false) {
        cards.push(meterCard({
            key: 'elect',
            icon: '⚡',
            iconClass: 'meter-card__icon--elect',
            title: 'Электричество',
            unit: 'кВт·ч',
            prev: state.prev_elect,
            current: state.current_elect,
            disabled: !state.is_period_open || state.is_already_approved,
        }));
    }
    if (!cards.length) {
        return `<div class="status-box status-box--info">
            <div class="status-box__title">ℹ️ Счётчики отсутствуют</div>
            <div class="status-box__sub">У вас не установлены счётчики — расход считается по нормативу.</div>
        </div>`;
    }
    return cards.join('');
}

function meterCard({ key, icon, iconClass, title, unit, prev, current, disabled }) {
    const prevText = prev !== null && prev !== undefined ? Number(prev).toFixed(3) : '0.000';
    const value = current !== null && current !== undefined ? Number(current) : '';
    return `
        <div class="meter-card" data-meter="${key}">
            <div class="meter-card__header">
                <div class="meter-card__icon ${iconClass}">${icon}</div>
                <div>
                    <div class="meter-card__title">${title}</div>
                    <div class="meter-card__sub">Прошлое: ${prevText} ${unit}</div>
                </div>
            </div>
            <div class="meter-input-row">
                <input
                    type="number"
                    class="meter-input"
                    name="${key}"
                    inputmode="decimal"
                    step="0.001"
                    min="0"
                    placeholder="0.000"
                    value="${value}"
                    autocomplete="off"
                    ${disabled ? 'disabled' : ''}
                    aria-label="${title}, ${unit}"
                />
                <span class="meter-unit">${unit}</span>
            </div>
            <div class="meter-error" data-error="${key}"></div>
        </div>
    `;
}

function bindForm(root) {
    const form = root.querySelector('#readingsForm');
    const submitBtn = root.querySelector('#submitReadings');
    if (!form || !submitBtn) return;

    // На каждое изменение — нормализуем запятую → точка, очищаем ошибку.
    form.addEventListener('input', (e) => {
        if (e.target.matches('.meter-input')) {
            e.target.value = e.target.value.replace(',', '.');
            const card = e.target.closest('.meter-card');
            if (card) card.classList.remove('is-error');
            const errBox = card?.querySelector('.meter-error');
            if (errBox) errBox.textContent = '';
        }
    });

    submitBtn.addEventListener('click', async () => {
        if (submitBtn.disabled) return;

        const data = collectAndValidate(form);
        if (!data) return;  // ошибки уже отображены

        // Confirm для draft (overwrite). На первой подаче спрашивать не надо —
        // лишний шаг для жильца, который и так нажал «Отправить».
        if (_state?.is_draft) {
            const ok = confirm(buildConfirmMessage(data));
            if (!ok) return;
        }

        submitBtn.disabled = true;
        submitBtn.textContent = 'Отправка…';
        try {
            await api.post('/calculate', data);
            toast.success('Спасибо! Показания приняты. Квитанция появится в «Истории» после закрытия периода.');
            // Перезагружаем экран — увидим новый статус (draft), расчёт.
            await renderReadings(root);
        } catch (e) {
            toast.error('Ошибка: ' + e.message);
            submitBtn.disabled = false;
            submitBtn.textContent = _state?.is_draft ? 'Обновить показания' : 'Отправить показания';
        }
    });
}

function collectAndValidate(form) {
    const data = { hot_water: 0, cold_water: 0, electricity: 0 };
    const fields = [
        { key: 'hot',   apiKey: 'hot_water',   present: _state.has_hw_meter !== false, prev: _state.prev_hot },
        { key: 'cold',  apiKey: 'cold_water',  present: _state.has_cw_meter !== false, prev: _state.prev_cold },
        { key: 'elect', apiKey: 'electricity', present: _state.has_el_meter !== false, prev: _state.prev_elect },
    ];
    let valid = true;
    for (const f of fields) {
        if (!f.present) {
            // Нет счётчика — сервер сам подставит из норматива. Шлём prev (или 0).
            data[f.apiKey] = Number(f.prev || 0);
            continue;
        }
        const input = form.querySelector(`input[name="${f.key}"]`);
        const card = input?.closest('.meter-card');
        const errBox = card?.querySelector('.meter-error');
        const value = parseFloat((input?.value || '').trim());
        if (!isFinite(value) || value < 0) {
            valid = false;
            card?.classList.add('is-error');
            if (errBox) errBox.textContent = 'Введите положительное число';
            continue;
        }
        const prev = Number(f.prev || 0);
        if (value < prev) {
            valid = false;
            card?.classList.add('is-error');
            if (errBox) errBox.textContent = `Не меньше прошлого (${prev.toFixed(3)})`;
            continue;
        }
        data[f.apiKey] = value;
    }
    if (!valid) {
        toast.error('Проверьте показания — есть ошибки');
        return null;
    }
    return data;
}

function buildConfirmMessage(data) {
    const lines = ['Перезаписать показания?', ''];
    if (_state.has_hw_meter !== false) lines.push(`🔥 ГВС: ${data.hot_water}`);
    if (_state.has_cw_meter !== false) lines.push(`❄️ ХВС: ${data.cold_water}`);
    if (_state.has_el_meter !== false) lines.push(`⚡ Свет: ${data.electricity}`);
    return lines.join('\n');
}

function escapeHtml(str) {
    if (str == null) return '';
    return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
