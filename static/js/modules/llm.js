// static/js/modules/llm.js — L4: UI ИИ-помощника.
//
// Эндпоинты: GET/PUT /api/admin/llm/settings, POST /token, DELETE /token,
// POST /test, GET /usage, GET /calls, POST /reset-disabled,
// GET /crypto-status.
//
// UX: настройки можно менять без сохранения токена (для смены модели/бюджета).
// Токен сохраняется отдельной кнопкой. Тест-кнопка делает реальный пинг.

import { api } from '../core/api.js';
import { toast } from '../core/dom.js';

function escapeHtml(s) {
    if (s === null || s === undefined) return '';
    return String(s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function fmtDateTime(iso) {
    if (!iso) return '—';
    try {
        const d = new Date(iso);
        return d.toLocaleString('ru-RU', { day:'2-digit', month:'2-digit',
            hour:'2-digit', minute:'2-digit', second:'2-digit' });
    } catch { return iso; }
}

export const LLMModule = {
    isInitialized: false,
    dom: {},

    init() {
        if (!this.isInitialized) {
            this._cacheDom();
            this._bindEvents();
            this.isInitialized = true;
        }
        this.refreshAll();
    },

    _cacheDom() {
        this.dom = {
            cryptoStatus: document.getElementById('llmCryptoStatus'),
            provider:     document.getElementById('llmProvider'),
            model:        document.getElementById('llmModel'),
            budget:       document.getElementById('llmBudget'),
            tokensBudget: document.getElementById('llmTokensBudget'),
            periodStart:  document.getElementById('llmPeriodStart'),
            enabled:      document.getElementById('llmEnabled'),
            tokenInput:   document.getElementById('llmTokenInput'),
            tokenHint:    document.getElementById('llmTokenHint'),
            saveToken:    document.getElementById('llmSaveToken'),
            deleteToken:  document.getElementById('llmDeleteToken'),
            saveSettings: document.getElementById('llmSaveSettings'),
            test:         document.getElementById('llmTest'),
            testResult:   document.getElementById('llmTestResult'),
            disabledBanner: document.getElementById('llmDisabledBanner'),
            disabledText:   document.getElementById('llmDisabledText'),
            resetDisabled:  document.getElementById('llmResetDisabled'),
            usageKpi:     document.getElementById('llmUsageKpi'),
            callsBody:    document.getElementById('llmCallsBody'),
            refreshCalls: document.getElementById('llmRefreshCalls'),
            systemPrompt: document.getElementById('llmSystemPrompt'),
        };
    },

    _bindEvents() {
        this.dom.saveToken?.addEventListener('click', () => this.saveToken());
        this.dom.deleteToken?.addEventListener('click', () => this.deleteToken());
        this.dom.saveSettings?.addEventListener('click', () => this.saveSettings());
        this.dom.test?.addEventListener('click', () => this.runTest());
        this.dom.resetDisabled?.addEventListener('click', () => this.resetDisabled());
        this.dom.refreshCalls?.addEventListener('click', () => this.loadCalls());
    },

    async refreshAll() {
        await Promise.all([
            this.loadCryptoStatus(),
            this.loadSettings(),
            this.loadUsage(),
            this.loadCalls(),
        ]);
    },

    async loadCryptoStatus() {
        try {
            const s = await api.get('/admin/llm/crypto-status');
            if (!s.crypto_ready) {
                this.dom.cryptoStatus.style.display = '';
                this.dom.cryptoStatus.innerHTML = `
                    <div style="background:#fee2e2; border:1px solid #ef4444; border-radius:6px; padding:12px; font-size:12px;">
                        <b><i class="fa-solid fa-circle-exclamation"></i> Не настроен LLM_SECRET_KEY</b><br>
                        ${escapeHtml(s.hint)}
                    </div>`;
            } else {
                this.dom.cryptoStatus.style.display = 'none';
            }
        } catch (e) {
            console.warn('[llm] crypto-status failed:', e?.message);
        }
    },

    async loadSettings() {
        try {
            const s = await api.get('/admin/llm/settings');
            this.dom.provider.value = s.provider || 'disabled';
            this.dom.model.value    = s.model_name || 'GigaChat';
            this.dom.budget.value   = s.daily_budget_rub || 50;
            this.dom.tokensBudget.value = s.monthly_budget_tokens || 0;
            this.dom.periodStart.value  = s.monthly_period_start || '';
            this.dom.enabled.checked = !!s.enabled;
            if (this.dom.systemPrompt) this.dom.systemPrompt.value = s.system_prompt || '';
            if (s.token_set) {
                this.dom.tokenHint.innerHTML = `<i class="fa-solid fa-check" style="color:#10b981;"></i> Токен сохранён: <code>${escapeHtml(s.token_hint)}</code>`;
            } else {
                this.dom.tokenHint.innerHTML = `<i class="fa-solid fa-circle-info" style="color:#6b7280;"></i> Токен не задан`;
            }
            if (s.disabled_until) {
                this.dom.disabledBanner.style.display = '';
                this.dom.disabledText.textContent = `Авто-блокировка до ${fmtDateTime(s.disabled_until)}: ${s.disabled_reason || ''}`;
            } else {
                this.dom.disabledBanner.style.display = 'none';
            }
        } catch (e) {
            toast('Не удалось загрузить настройки: ' + e.message, 'error');
        }
    },

    async saveSettings() {
        const body = {
            provider: this.dom.provider.value,
            model_name: this.dom.model.value.trim() || 'GigaChat',
            enabled: this.dom.enabled.checked,
            daily_budget_rub: Number(this.dom.budget.value) || 50,
            monthly_budget_tokens: Number(this.dom.tokensBudget.value) || 0,
            monthly_period_start: this.dom.periodStart.value || '',
            system_prompt: this.dom.systemPrompt ? this.dom.systemPrompt.value : '',
        };
        try {
            await api.put('/admin/llm/settings', body);
            toast('Настройки сохранены', 'success');
            this.loadSettings();
        } catch (e) {
            toast('Ошибка: ' + e.message, 'error');
        }
    },

    async saveToken() {
        const tok = (this.dom.tokenInput.value || '').trim();
        if (tok.length < 10) {
            toast('Токен слишком короткий — проверь ввод', 'warning');
            return;
        }
        try {
            await api.post('/admin/llm/token', { token: tok });
            this.dom.tokenInput.value = '';
            toast('Токен сохранён (зашифрован)', 'success');
            this.loadSettings();
        } catch (e) {
            toast('Не удалось сохранить токен: ' + e.message, 'error');
        }
    },

    async deleteToken() {
        if (!confirm('Стереть токен и выключить ИИ?')) return;
        try {
            const resp = await fetch('/api/admin/llm/token', {
                method: 'DELETE', credentials: 'include',
            });
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            toast('Токен удалён', 'success');
            this.loadSettings();
        } catch (e) {
            toast('Не удалось удалить: ' + e.message, 'error');
        }
    },

    async runTest() {
        this.dom.testResult.style.display = '';
        this.dom.testResult.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Запрашиваю ИИ…';
        try {
            const r = await api.post('/admin/llm/test', {});
            if (r.ok) {
                this.dom.testResult.innerHTML = `
                    <div><b>✅ OK</b> · ${r.latency_ms} мс · ${r.cost_rub ?? 0} ₽</div>
                    <pre style="background:#f1f5f9; padding:8px; border-radius:4px; margin-top:6px; white-space:pre-wrap;">${escapeHtml(r.text || '(пустой ответ)')}</pre>
                    ${r.disabled_after ? '<div style="color:#dc2626; margin-top:4px;"><i class="fa-solid fa-triangle-exclamation"></i> Бюджет на сегодня исчерпан, ИИ выключен до полуночи.</div>' : ''}`;
                toast('Тест прошёл успешно', 'success');
            } else {
                this.dom.testResult.innerHTML = `
                    <div style="color:#dc2626;"><b>❌ Ошибка</b></div>
                    <pre style="background:#fee2e2; padding:8px; border-radius:4px; margin-top:6px; white-space:pre-wrap;">${escapeHtml(r.error || 'unknown')}</pre>`;
                toast('Тест упал: ' + (r.error || 'unknown'), 'error');
            }
            this.loadUsage();
            this.loadCalls();
        } catch (e) {
            this.dom.testResult.innerHTML = `<div style="color:#dc2626;">${escapeHtml(e.message)}</div>`;
        }
    },

    async resetDisabled() {
        try {
            await api.post('/admin/llm/reset-disabled', {});
            toast('Блокировка снята', 'success');
            this.loadSettings();
        } catch (e) {
            toast('Ошибка: ' + e.message, 'error');
        }
    },

    async loadUsage() {
        try {
            const u = await api.get('/admin/llm/usage?days=30');
            let cards;
            if (u.budget_mode === 'tokens' && u.token_stats) {
                // L8 Freemium: KPI в токенах.
                const t = u.token_stats;
                cards = [
                    { label: 'Токенов потрачено за месяц',
                      value: t.tokens_used.toLocaleString('ru-RU'),
                      sub: `из ${t.tokens_budget.toLocaleString('ru-RU')} (${t.used_pct.toFixed(1)}%)`,
                      color: t.used_pct > 80 ? '#ef4444'
                            : t.used_pct > 50 ? '#f59e0b' : '#10b981' },
                    { label: 'Осталось токенов',
                      value: t.tokens_remaining.toLocaleString('ru-RU'),
                      sub: `с ${t.period_start}`, color: '#3b82f6' },
                    { label: 'Вызовов сегодня', value: u.today_calls || 0,
                      sub: '', color: '#6b7280' },
                    { label: 'За 30 дней (вызовов)', value: u.total_calls,
                      sub: `${u.total_success} ok / ${u.total_failed} err`,
                      color: '#8b5cf6' },
                ];
            } else {
                // Рубль-режим.
                cards = [
                    { label: 'Сегодня потрачено',
                      value: `${Number(u.today_cost_rub || 0).toFixed(2)} ₽`,
                      sub: `из ${u.today_budget_rub} ₽ (${u.today_used_pct.toFixed(0)}%)`,
                      color: u.today_used_pct > 80 ? '#f59e0b' : '#10b981' },
                    { label: 'Вызовов сегодня', value: u.today_calls || 0,
                      sub: '', color: '#3b82f6' },
                    { label: 'За 30 дней', value: u.total_calls,
                      sub: `${u.total_success} ok / ${u.total_failed} err`, color: '#6b7280' },
                    { label: 'Сумма за 30 дней',
                      value: `${Number(u.total_cost_rub || 0).toFixed(2)} ₽`,
                      sub: '', color: '#8b5cf6' },
                ];
            }
            this.dom.usageKpi.innerHTML = cards.map(c => `
                <div style="background:var(--bg-card); border:1px solid var(--border-color); border-radius:8px; padding:12px;">
                    <div style="color:var(--text-secondary); font-size:11px; text-transform:uppercase;">${escapeHtml(c.label)}</div>
                    <div style="font-size:22px; font-weight:700; color:${c.color}; margin-top:4px;">${c.value}</div>
                    ${c.sub ? `<div style="color:var(--text-tertiary); font-size:11px; margin-top:2px;">${escapeHtml(c.sub)}</div>` : ''}
                </div>`).join('');
        } catch (e) {
            console.warn('[llm] usage failed:', e?.message);
        }
    },

    async loadCalls() {
        try {
            const rows = await api.get('/admin/llm/calls?limit=50');
            this._lastCalls = rows;
            if (!rows.length) {
                this.dom.callsBody.innerHTML = '<tr><td colspan="8" style="padding:30px; text-align:center; color:var(--text-secondary);">Вызовов ещё не было</td></tr>';
                return;
            }
            this.dom.callsBody.innerHTML = rows.map(r => `
                <tr style="border-top:1px solid var(--border-color);">
                    <td style="padding:6px 10px;">${escapeHtml(fmtDateTime(r.occurred_at))}</td>
                    <td style="padding:6px 10px;">${escapeHtml(r.purpose)}</td>
                    <td style="padding:6px 10px;">${escapeHtml(r.model_name)}</td>
                    <td style="padding:6px 10px; text-align:right; font-family:monospace;">${r.prompt_tokens || '—'}/${r.response_tokens || '—'}</td>
                    <td style="padding:6px 10px; text-align:right; font-family:monospace;">${r.cost_rub ? r.cost_rub.toFixed(4) : '—'}</td>
                    <td style="padding:6px 10px; text-align:right; font-family:monospace;">${r.latency_ms || '—'}</td>
                    <td style="padding:6px 10px;">${r.success
                        ? '<span style="color:#10b981;">✓</span>'
                        : `<span style="color:#dc2626;" title="${escapeHtml(r.error_short || '')}">✗ ${escapeHtml((r.error_short || '').slice(0, 60))}</span>`}</td>
                    <td style="padding:6px 10px; text-align:center;">${(r.response_text || r.prompt_text)
                        ? `<button class="icon-btn" data-call-report="${r.id}" title="Что ИИ сделал — промпт и ответ"><i class="fa-solid fa-eye"></i></button>`
                        : '<span style="color:var(--text-tertiary);">—</span>'}</td>
                </tr>`).join('');
            // Делегация: открыть отчёт по клику на «глаз».
            this.dom.callsBody.onclick = (e) => {
                const btn = e.target.closest('button[data-call-report]');
                if (!btn) return;
                const call = (this._lastCalls || []).find(
                    c => String(c.id) === btn.dataset.callReport);
                if (call) this._showCallReport(call);
            };
        } catch (e) {
            console.warn('[llm] calls failed:', e?.message);
        }
    },

    /** Модалка «что ИИ сделал»: результат (ответ) + что проверял (промпт). */
    _showCallReport(call) {
        const purposeRu = {
            daily_briefing: '🌅 Утренняя сводка',
            error_analysis: '🐛 Разбор ошибки',
            anomaly_triage: '🔍 Триаж аномалии',
            user_summary: '👤 Разбор жильца',
            test: '🧪 Тест соединения',
        }[call.purpose] || call.purpose;
        const esc = (t) => escapeHtml(t || '');
        const overlay = document.createElement('div');
        overlay.style.cssText = 'position:fixed; inset:0; background:rgba(0,0,0,0.55); z-index:10000; display:flex; align-items:center; justify-content:center; padding:20px;';
        overlay.innerHTML = `
            <div style="background:var(--bg-card,#fff); border-radius:12px; padding:20px 22px; max-width:760px; width:100%; max-height:88vh; overflow:auto; box-shadow:0 20px 60px rgba(0,0,0,0.3);">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
                    <h3 style="margin:0; font-size:16px;">${esc(purposeRu)}</h3>
                    <button data-report-close class="icon-btn"><i class="fa-solid fa-xmark"></i></button>
                </div>
                <div style="font-size:12px; color:var(--text-secondary); margin-bottom:14px;">
                    ${esc(fmtDateTime(call.occurred_at))} · ${esc(call.model_name)} ·
                    ${call.prompt_tokens || '—'}/${call.response_tokens || '—'} токенов ·
                    ${call.cost_rub ? call.cost_rub.toFixed(4) + ' ₽' : '—'} ·
                    ${call.success ? '<span style="color:#10b981;">успех</span>' : '<span style="color:#dc2626;">ошибка</span>'}
                </div>
                ${call.error_short ? `<div style="background:#fef2f2; border:1px solid #fecaca; border-radius:8px; padding:10px; margin-bottom:12px; font-size:12px; color:#991b1b;"><b>Ошибка:</b> ${esc(call.error_short)}</div>` : ''}
                <div style="margin-bottom:6px; font-weight:600; font-size:13px; color:#0369a1;">📤 Что ИИ ответил (результат):</div>
                <pre style="background:#f0f9ff; border:1px solid #bae6fd; border-radius:8px; padding:12px; font-size:12.5px; white-space:pre-wrap; word-break:break-word; margin:0 0 16px; font-family:inherit;">${call.response_text ? esc(call.response_text) : '<i style="color:#9ca3af;">пусто (нет ответа или старый вызов до обновления)</i>'}</pre>
                <details>
                    <summary style="cursor:pointer; font-weight:600; font-size:13px; color:var(--text-secondary);">📥 Что ушло в ИИ (промпт + данные, которые он проверял)</summary>
                    <pre style="background:var(--bg-page); border:1px solid var(--border-color); border-radius:8px; padding:12px; font-size:11.5px; white-space:pre-wrap; word-break:break-word; margin:8px 0 0; font-family:inherit; max-height:320px; overflow:auto;">${call.prompt_text ? esc(call.prompt_text) : '<i style="color:#9ca3af;">пусто</i>'}</pre>
                </details>
            </div>`;
        overlay.addEventListener('click', (e) => {
            if (e.target === overlay || e.target.closest('[data-report-close]')) overlay.remove();
        });
        document.body.appendChild(overlay);
    },
};
