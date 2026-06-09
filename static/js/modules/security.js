// static/js/modules/security.js
//
// Вкладка «Безопасность» — обзор сводок сканеров (SonarQube/ZAP/Trivy/Bandit/…),
// которые CI присылает на POST /api/admin/security/report и которые лежат в
// SystemSetting['security_findings']. Обзор + топ-находки + ссылки на канон
// (GitHub Security / SonarQube). Только admin.

import { api } from '../core/api.js';
import { toast } from '../core/dom.js';

function esc(s) {
    if (s === null || s === undefined) return '';
    return String(s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// severity → подпись/цвета. Ключи совпадают с totals бэкенда + произвольные
// (zap: high/medium/low/info; sonar: vulnerabilities/bugs/hotspots/code_smells).
const SEV_META = {
    critical: { label: 'Critical', bg: '#fee2e2', color: '#991b1b' },
    high:     { label: 'High',     bg: '#fee2e2', color: '#b91c1c' },
    medium:   { label: 'Medium',   bg: '#fef3c7', color: '#92400e' },
    low:      { label: 'Low',      bg: '#dbeafe', color: '#1e40af' },
    info:     { label: 'Info',     bg: '#f1f5f9', color: '#475569' },
};
const SEV_RANK = { critical: 0, high: 1, medium: 2, low: 3, info: 4 };

function sevBadge(sev) {
    const key = String(sev || '').toLowerCase();
    const m = SEV_META[key] || { label: sev || '—', bg: '#f1f5f9', color: '#475569' };
    return `<span style="display:inline-block; padding:2px 8px; border-radius:4px; font-size:11px; font-weight:600; background:${m.bg}; color:${m.color};">${esc(m.label)}</span>`;
}

function fmtTime(iso) {
    if (!iso) return '—';
    try {
        return new Date(iso).toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' });
    } catch (e) { return esc(iso); }
}

export const SecurityModule = {
    isInitialized: false,
    dom: {},

    init() {
        this.cacheDom();
        if (!this.isInitialized) {
            this.bind();
            this.isInitialized = true;
        }
        this.refresh();
    },

    cacheDom() {
        this.dom = {
            totals: document.getElementById('securityTotals'),
            tools:  document.getElementById('securityTools'),
            body:   document.getElementById('securityFindingsBody'),
            btn:    document.getElementById('btnRefreshSecurity'),
        };
    },

    bind() {
        this.dom.btn?.addEventListener('click', () => this.refresh());
    },

    async refresh() {
        try {
            const data = await api.get('/admin/security/findings');
            this.render(data);
        } catch (e) {
            toast('Не удалось загрузить сводку безопасности: ' + (e.message || e), 'error');
        }
    },

    render(data) {
        const tools = data?.tools || {};
        const totals = data?.totals || {};
        const toolKeys = Object.keys(tools);

        // ── Агрегат по severity ──
        if (this.dom.totals) {
            if (!toolKeys.length) {
                this.dom.totals.innerHTML =
                    `<div style="padding:16px; text-align:center; color:var(--text-secondary); grid-column:1/-1;">
                       Пока нет данных. CI пришлёт сводку после следующего прогона
                       ${data?.configured === false ? '(сначала задайте SECURITY_SYNC_TOKEN в .env)' : ''}.
                     </div>`;
            } else {
                this.dom.totals.innerHTML = ['critical', 'high', 'medium', 'low'].map(sev => {
                    const m = SEV_META[sev];
                    const n = totals[sev] || 0;
                    const dim = n === 0 ? 'opacity:0.55;' : '';
                    return `<div style="padding:14px; border-radius:8px; background:${m.bg}; ${dim}">
                              <div style="font-size:26px; font-weight:700; color:${m.color};">${n}</div>
                              <div style="font-size:12px; color:${m.color};">${m.label}</div>
                            </div>`;
                }).join('');
            }
        }

        // ── Карточки по инструментам ──
        if (this.dom.tools) {
            this.dom.tools.innerHTML = toolKeys.map(k => {
                const t = tools[k] || {};
                const counts = t.counts || {};
                const chips = Object.keys(counts).map(c =>
                    `<span style="display:inline-block; margin:2px 4px 2px 0; padding:2px 8px; border-radius:4px; font-size:11px; background:#f1f5f9; color:#334155;">
                       ${esc(c)}: <b>${esc(counts[c])}</b></span>`).join('') || '<span style="color:var(--text-secondary); font-size:12px;">нет счётчиков</span>';
                const status = t.status
                    ? `<span style="font-size:12px; color:${/fail|error/i.test(t.status) ? '#b91c1c' : '#15803d'};">● ${esc(t.status)}</span>`
                    : '';
                const link = t.run_url
                    ? `<a href="${esc(t.run_url)}" target="_blank" rel="noopener" style="font-size:12px;">открыть →</a>`
                    : '';
                return `<div class="card" style="margin:0;">
                          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
                            <strong>${esc(t.label || k)}</strong> ${status}
                          </div>
                          <div style="margin-bottom:8px;">${chips}</div>
                          <div style="display:flex; justify-content:space-between; font-size:11px; color:var(--text-secondary);">
                            <span>${fmtTime(t.received_at || t.generated_at)}</span> ${link}
                          </div>
                        </div>`;
            }).join('');
        }

        // ── Объединённая таблица топ-находок (сорт по severity) ──
        if (this.dom.body) {
            const rows = [];
            toolKeys.forEach(k => {
                const t = tools[k] || {};
                (t.top || []).forEach(f => rows.push({
                    sev: f.sev || f.severity || 'info',
                    title: f.title || f.name || f.rule || '—',
                    where: f.where || f.location || f.url || f.file || '—',
                    tool: t.label || k,
                }));
            });
            rows.sort((a, b) => (SEV_RANK[String(a.sev).toLowerCase()] ?? 9) - (SEV_RANK[String(b.sev).toLowerCase()] ?? 9));

            this.dom.body.innerHTML = rows.length
                ? rows.map(r => `<tr>
                      <td>${sevBadge(r.sev)}</td>
                      <td style="font-size:12px; color:var(--text-secondary);">${esc(r.tool)}</td>
                      <td>${esc(r.title)}</td>
                      <td style="font-size:12px; word-break:break-all;">${esc(r.where)}</td>
                    </tr>`).join('')
                : `<tr><td colspan="4" class="text-center" style="padding:32px; color:var(--text-secondary);">
                     Находок нет или данные ещё не пришли из CI.</td></tr>`;
        }
    },
};
