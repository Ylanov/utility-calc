// static/js/modules/passport.js
//
// «Поиск жильца» — глобальный поиск по ФИО → карточка 360°: всё о жильце из
// ВСЕХ источников в одном месте. Read-only композиция (бэк: passport-360).
//   * где живёт + тариф (за что платит);
//   * ВСЯ история показаний из всех путей (QR/приложение/Google-таблица/ручные/
//     черновики + буфер Google ещё не промоутнутый + подачи соседей по комнате);
//   * долги/переплаты 1С (последнее загруженное, вкл. неутверждённые + опубликованное)
//     и ГИС ГМП.

import { api } from '../core/api.js';
import { toast } from '../core/dom.js';

function esc(s) {
    if (s == null) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
function money(v) {
    return Number(v || 0).toLocaleString('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function dt(iso) {
    if (!iso) return '—';
    try { return new Date(iso).toLocaleDateString('ru-RU'); } catch { return iso; }
}

const CHARGE_LABELS = {
    charge_hot_water: 'ГВС', charge_cold_water: 'ХВС', charge_sewage: 'Водоотв.',
    charge_electricity: 'Электр.', charge_heating: 'Отопл.', charge_maintenance: 'Содерж.',
    charge_social_rent: 'Наём', charge_waste: 'ТКО',
};

export const PassportModule = {
    isInitialized: false,

    init() {
        this.dom = {
            search: document.getElementById('p360Search'),
            results: document.getElementById('p360Results'),
            card: document.getElementById('p360Card'),
        };
        if (!this.dom.search) return;
        if (!this.isInitialized) {
            this.bind();
            this.isInitialized = true;
        }
        this.dom.search.focus();
    },

    bind() {
        let t = null;
        this.dom.search.addEventListener('input', () => {
            clearTimeout(t);
            const q = this.dom.search.value.trim();
            if (q.length < 2) { this.hideResults(); return; }
            t = setTimeout(() => this.searchFio(q), 250);
        });
        // Закрытие выпадашки по клику вне.
        document.addEventListener('click', (e) => {
            if (this.dom.results && !this.dom.results.contains(e.target) && e.target !== this.dom.search) {
                this.hideResults();
            }
        });
    },

    hideResults() { if (this.dom.results) this.dom.results.style.display = 'none'; },

    async searchFio(q) {
        try {
            const res = await api.get(`/users?search=${encodeURIComponent(q)}&limit=20`);
            const items = res.items || [];
            if (!items.length) {
                this.dom.results.innerHTML = '<div style="padding:12px; color:#9ca3af;">Ничего не найдено</div>';
                this.dom.results.style.display = 'block';
                return;
            }
            this.dom.results.innerHTML = items.map(u => {
                const rm = u.room || {};   // вложенный объект room (RoomResponse), не плоские поля
                const hint = rm.room_number ? ` · ${esc(rm.dormitory_name || '')} ${esc(rm.room_number)}`.trimEnd() : '';
                return `<div class="p360-opt" data-uid="${u.id}" style="padding:10px 14px; cursor:pointer; border-bottom:1px solid var(--border-color,#f1f1f1);">
                    <b>${esc(u.username || '—')}</b><span style="color:#9ca3af; font-size:12px;">${hint}</span>
                </div>`;
            }).join('');
            this.dom.results.style.display = 'block';
            this.dom.results.querySelectorAll('.p360-opt').forEach(el => {
                el.addEventListener('mouseenter', () => { el.style.background = 'var(--bg-hover,#f3f4f6)'; });
                el.addEventListener('mouseleave', () => { el.style.background = ''; });
                el.addEventListener('click', () => {
                    this.hideResults();
                    this.dom.search.value = el.querySelector('b').textContent;
                    this.loadCard(el.getAttribute('data-uid'));
                });
            });
        } catch (e) {
            toast('Ошибка поиска: ' + (e?.message || e), 'error');
        }
    },

    async loadCard(uid) {
        this.dom.card.innerHTML = '<div class="card" style="text-align:center; padding:40px; color:var(--primary-color);"><i class="fa-solid fa-spinner fa-spin" style="font-size:2rem;"></i></div>';
        try {
            const d = await api.get(`/admin/residents/${uid}/passport-360`);
            this.dom.card.innerHTML = this.renderCard(d);
            this._wire(d);
        } catch (e) {
            this.dom.card.innerHTML = `<div class="card" style="color:#b91c1c; padding:24px;">Не удалось загрузить карточку: ${esc(e?.message || e)}</div>`;
        }
    },

    renderCard(d) {
        const r = d.resident || {};
        const room = r.room;
        const t = r.tariff;
        const addr = room ? esc(room.address) : '<span style="color:#b91c1c;">без комнаты</span>';
        const chargeChips = t && t.charges
            ? Object.entries(t.charges).filter(([, v]) => v)
                .map(([k]) => `<span class="p360-chip">${CHARGE_LABELS[k] || k}</span>`).join(' ') || '<span style="color:#9ca3af;">нет начислений</span>'
            : '—';

        // ── Шапка ──
        const header = `<div class="card" style="margin-bottom:14px;">
            <div style="display:flex; justify-content:space-between; align-items:flex-start; flex-wrap:wrap; gap:8px;">
                <div>
                    <h2 style="margin:0 0 4px;">${esc(r.fio || '—')}
                        ${r.is_deleted ? '<span class="p360-badge" style="background:#fee2e2; color:#b91c1c;">выселен</span>' : ''}</h2>
                    <div style="color:var(--text-secondary); font-size:13px;">
                        Вход (логин): <b>${esc(r.login || '—')}</b> · роль: ${esc(r.role || '—')} · id ${r.user_id}
                    </div>
                    <div style="margin-top:6px; font-size:14px;"><i class="fa-solid fa-location-dot" style="color:#6b7280;"></i> ${addr}
                        ${room && room.is_singles_apartment ? '<span class="p360-badge" style="background:#fef3c7; color:#92400e;">холостяцкая</span>' : ''}
                        ${room ? `<span style="color:#9ca3af; font-size:12px;"> · жильцов в комнате: ${room.total_room_residents}</span>` : ''}
                    </div>
                </div>
                <div style="text-align:right; min-width:220px;">
                    <div style="font-size:12px; color:var(--text-secondary);">За что платит (тариф)</div>
                    <div style="font-weight:600; margin:2px 0;">${t ? esc(t.name) : '—'}${t && t.tariff_type === 'unconditional' ? ' <span class="p360-badge" style="background:#ede9fe; color:#6d28d9;">БЕЗ УСЛОВИЙ</span>' : ''}</div>
                    <div style="display:flex; flex-wrap:wrap; gap:4px; justify-content:flex-end;">${chargeChips}</div>
                </div>
            </div>
        </div>`;

        return header + this._renderDebts(d.debts || {})
            + this._renderFinance(d.finance) + this._renderReadings(d.readings || [], d.buffer || []);
    },

    // Начисления ПО ТАРИФУ — те же числа, что в финотчётности (reuse finance-detail).
    _renderFinance(fin) {
        if (!fin || !fin.history) return '';
        const f = (v) => v == null ? '—' : Number(v).toLocaleString('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        const dlt = (v) => v == null ? '' : ` <span style="color:#9ca3af;">(${v > 0 ? '+' : ''}${Number(v).toLocaleString('ru-RU', { maximumFractionDigits: 2 })})</span>`;
        const rows = (fin.history || []).filter(h => h.reading_id).map(h => `<tr>
            <td>${esc(h.period_name)}</td>
            <td style="text-align:right;">${h.hot_water == null ? '—' : h.hot_water}${dlt(h.delta_hot)}</td>
            <td style="text-align:right;">${h.cold_water == null ? '—' : h.cold_water}${dlt(h.delta_cold)}</td>
            <td style="text-align:right;">${h.electricity == null ? '—' : h.electricity}${dlt(h.delta_elect)}</td>
            <td style="text-align:right;">${f(h.total_209)}</td>
            <td style="text-align:right;">${f(h.total_205)}</td>
            <td style="text-align:right; font-weight:600;">${f(h.total_cost)}</td>
        </tr>`).join('');
        const c = fin.current;
        let receipt = '';
        if (c && c.costs) {
            const L = { cost_hot_water: 'ГВС', cost_cold_water: 'ХВС', cost_sewage: 'Водоотв.', cost_electricity: 'Электр.', cost_maintenance: 'Содерж.', cost_social_rent: 'Наём', cost_waste: 'ТКО', cost_fixed_part: 'Фикс.' };
            const items = Object.entries(L).filter(([k]) => Math.abs(c.costs[k] || 0) > 0.005)
                .map(([k, lab]) => `<span class="p360-chip">${lab}: ${f(c.costs[k])}</span>`).join(' ');
            receipt = `<div style="margin-top:10px; font-size:13px; padding-top:8px; border-top:1px dashed var(--border-color,#eee);">
                <b>Квитанция за ${esc(fin.period ? fin.period.name : '')}</b> (расчёт по тарифу): ${items || '<span style="color:#9ca3af;">нет начислений</span>'}
                <div style="margin-top:4px;">Итого: <b>${f(c.total_cost)} ₽</b> · коммуналка (209) ${f(c.total_209)} · наём (205) ${f(c.total_205)}</div></div>`;
        }
        return `<div class="card" style="margin-bottom:14px;">
            <div class="card-header" style="margin-bottom:10px;"><h3><i class="fa-solid fa-calculator" style="color:#0ea5e9; margin-right:6px;"></i> Начисления по тарифу (расчёт как в финотчётности)</h3></div>
            <div style="overflow-x:auto;"><table class="p360-table" style="width:100%; font-size:13px;">
                <thead><tr><th>Период</th><th style="text-align:right;">ГВС</th><th style="text-align:right;">ХВС</th><th style="text-align:right;">Эл.</th>
                    <th style="text-align:right;">Коммун. 209</th><th style="text-align:right;">Наём 205</th><th style="text-align:right;">Итого ₽</th></tr></thead>
                <tbody>${rows || '<tr><td colspan="7" style="color:#9ca3af; text-align:center; padding:12px;">Нет рассчитанных начислений.</td></tr>'}</tbody>
            </table></div>${receipt}
        </div>`;
    },

    _renderDebts(debts) {
        const onec = debts.onec || {};
        const pub = debts.published || {};
        const gis = debts.gis || {};
        const stBadge = (s) => s === 'staged'
            ? '<span class="p360-badge" style="background:#fef3c7; color:#92400e;">черновик (не выгружено)</span>'
            : (s === 'completed' ? '<span class="p360-badge" style="background:#dcfce7; color:#166534;">выгружено</span>' : '');
        const accRow = (acc, title) => {
            const o = onec[acc] || {};
            return `<tr>
                <td><b>${title}</b><div style="font-size:11px; color:#9ca3af;">${esc(o.file || '—')} · ${dt(o.at)}</div></td>
                <td style="text-align:right;">${o.found ? money(o.debt) : '—'}${o.overpayment > 0 ? `<div style="color:#16a34a; font-size:12px;">переплата ${money(o.overpayment)}</div>` : ''}</td>
                <td>${stBadge(o.status)}</td>
            </tr>`;
        };
        // Опубликованный баланс (что жилец видит) — из MeterReading. debt_209/205
        // есть только в полной форме; для no_room/zero берём balance_209/205 (всегда есть).
        const d209 = pub.debt_209 != null ? pub.debt_209 : (pub.balance_209 || 0);
        const d205 = pub.debt_205 != null ? pub.debt_205 : (pub.balance_205 || 0);
        const pubLine = `Опубликовано (жилец видит сейчас): <b>${money(d209)}</b> (209) · <b>${money(d205)}</b> (205)`;

        return `<div class="card" style="margin-bottom:14px;">
            <div class="card-header" style="margin-bottom:10px;"><h3><i class="fa-solid fa-scale-balanced" style="color:#f59e0b; margin-right:6px;"></i> Долги и переплаты</h3></div>
            <div style="display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:16px;">
                <div>
                    <div style="font-weight:600; margin-bottom:6px;">1С — последнее загруженное</div>
                    <table class="p360-table"><thead><tr><th>Счёт</th><th style="text-align:right;">Долг, ₽</th><th>Статус</th></tr></thead>
                    <tbody>${accRow('209', '209 · коммуналка')}${accRow('205', '205 · наём')}</tbody></table>
                    <div style="font-size:12px; color:var(--text-secondary); margin-top:8px; padding-top:8px; border-top:1px dashed var(--border-color,#eee);">${pubLine}</div>
                </div>
                <div>
                    <div style="font-weight:600; margin-bottom:6px;">ГИС ГМП ${gis.synced_at ? `<span style="font-size:11px; color:#9ca3af;">(${dt(gis.synced_at)})</span>` : ''}</div>
                    ${gis.found
                        ? `<table class="p360-table"><tbody>
                            <tr><td>209 · коммуналка</td><td style="text-align:right;"><b>${money(gis.debt_209)}</b></td></tr>
                            <tr><td>205 · наём</td><td style="text-align:right;"><b>${money(gis.debt_205)}</b></td></tr>
                           </tbody></table>
                           <button class="action-btn secondary-btn" id="p360GisDetail" style="font-size:12px; margin-top:8px;"><i class="fa-solid fa-list"></i> Начисления ГИС по фамилии</button>
                           <div id="p360GisCharges"></div>`
                        : '<div style="color:#9ca3af; padding:6px 0;">Нет в находках ГИС ГМП (или всё оплачено / иное написание ФИО).</div>'}
                </div>
            </div>
        </div>`;
    },

    _renderReadings(readings, buffer) {
        const rows = readings.map(r => `<tr style="${r.is_approved ? '' : 'background:#fffbeb;'}">
            <td>${esc(r.period)}</td>
            <td>${dt(r.date)}</td>
            <td style="text-align:right;">${r.hot_water || '—'}</td>
            <td style="text-align:right;">${r.cold_water || '—'}</td>
            <td style="text-align:right;">${r.electricity || '—'}</td>
            <td>${esc(r.source_label)}</td>
            <td>${r.is_approved ? '<span style="color:#166534;">боевое</span>' : '<span style="color:#92400e;">черновик</span>'}${r.own ? '' : ' <span class="p360-badge" style="background:#e0e7ff; color:#3730a3;" title="подал сосед/представитель по комнате">по комнате</span>'}</td>
            <td style="text-align:right;">${r.total_209 ? money(r.total_209) : '—'}</td>
            <td style="text-align:right;">${r.total_205 ? money(r.total_205) : '—'}</td>
        </tr>`).join('');

        const bufRows = buffer.map(b => `<tr style="background:#f0f9ff;">
            <td colspan="2">${dt(b.date)} <span style="font-size:11px; color:#9ca3af;">${esc(b.raw_room || '')}</span></td>
            <td style="text-align:right;">${b.hot_water || '—'}</td>
            <td style="text-align:right;">${b.cold_water || '—'}</td>
            <td style="text-align:right;">—</td>
            <td>${esc(b.source_label)}</td>
            <td colspan="3"><span class="p360-badge" style="background:#dbeafe; color:#1e40af;">буфер: ${esc(b.status)}</span>${b.linked === false ? ' <span class="p360-badge" style="background:#fef3c7; color:#92400e;" title="Найдено по ФИО, но не привязано к жильцу — утвердите в Реестре показаний">не привязано</span>' : ''} <span style="font-size:11px; color:#9ca3af;">${esc(b.raw_fio || '')}</span></td>
        </tr>`).join('');

        const empty = (!readings.length && !buffer.length)
            ? '<tr><td colspan="9" style="text-align:center; color:#9ca3af; padding:18px;">Показаний нет ни в одном источнике.</td></tr>' : '';

        return `<div class="card">
            <div class="card-header" style="margin-bottom:10px;"><h3><i class="fa-solid fa-chart-line" style="color:var(--primary-color); margin-right:6px;"></i> Показания — все источники, все даты</h3>
                <span style="font-size:12px; color:var(--text-secondary);">боевые: ${readings.length} · буфер Google: ${buffer.length}</span>
            </div>
            <div style="overflow-x:auto;">
            <table class="p360-table" style="width:100%; font-size:13px;">
                <thead><tr>
                    <th>Период</th><th>Дата</th><th style="text-align:right;">ГВС</th><th style="text-align:right;">ХВС</th>
                    <th style="text-align:right;">Эл.</th><th>Источник</th><th>Статус</th>
                    <th style="text-align:right;">209, ₽</th><th style="text-align:right;">205, ₽</th>
                </tr></thead>
                <tbody>${empty}${rows}${bufRows}</tbody>
            </table>
            </div>
        </div>`;
    },

    _wire(d) {
        const btn = document.getElementById('p360GisDetail');
        if (btn) {
            btn.addEventListener('click', async () => {
                const box = document.getElementById('p360GisCharges');
                box.innerHTML = '<div style="padding:8px; color:#9ca3af;">Загрузка…</div>';
                try {
                    const fio = (d.resident && d.resident.fio) || '';
                    const res = await api.get(`/financier/gisgmp/payer-charges?q=${encodeURIComponent(fio)}`);
                    const ch = res.charges || [];
                    if (!ch.length) { box.innerHTML = '<div style="padding:8px; color:#9ca3af;">Начислений в кэше ГИС нет.</div>'; return; }
                    box.innerHTML = `<table class="p360-table" style="font-size:12px; margin-top:8px;">
                        <thead><tr><th>УИН</th><th style="text-align:right;">Сумма</th><th>Назначение</th><th>Статус</th></tr></thead>
                        <tbody>${ch.slice(0, 100).map(c => `<tr>
                            <td>${esc((c.uin || '').slice(0, 12))}…</td>
                            <td style="text-align:right;">${money(c.amount)}</td>
                            <td>${esc((c.purpose || '').slice(0, 40))}</td>
                            <td>${esc(c.ack_status || '')}</td>
                        </tr>`).join('')}</tbody></table>`;
                } catch (e) {
                    box.innerHTML = `<div style="padding:8px; color:#b91c1c;">Ошибка: ${esc(e?.message || e)}</div>`;
                }
            });
        }
    },
};
