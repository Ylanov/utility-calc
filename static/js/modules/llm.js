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
            this.dom.enabled.checked = !!s.enabled;
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
            const cards = [
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
            if (!rows.length) {
                this.dom.callsBody.innerHTML = '<tr><td colspan="7" style="padding:30px; text-align:center; color:var(--text-secondary);">Вызовов ещё не было</td></tr>';
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
                </tr>`).join('');
        } catch (e) {
            console.warn('[llm] calls failed:', e?.message);
        }
    },
};
