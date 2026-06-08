// static/js/modules/safety.js
//
// Вкладка «Охрана труда» — реестр должностей/сотрудников + сигналы по срокам
// обучения ОТ и медосмотра. Источник: /api/admin/ot/* (summary, structure,
// staff CRUD, link-candidates, seed). Lazy-загрузка при первом клике на вкладку.

import { api } from '../core/api.js';
import { el, toast, setLoading, showConfirm, escapeHtml } from '../core/dom.js';

// Источники штата: машинное значение → человекочитаемая подпись.
const SOURCE_LABELS = {
    osnovnoy_shtat: 'Основной штат',
    kes_2025: 'КЭС-2025',
};

// Статус срока (обучение/медосмотр): цвет фона/текста бейджа + подпись.
const STATUS_STYLE = {
    overdue: { bg: '#fee2e2', color: '#991b1b', label: 'просрочено' },
    soon:    { bg: '#ffedd5', color: '#9a3412', label: 'скоро' },
    ok:      { bg: '#dcfce7', color: '#166534', label: 'в норме' },
    none:    { bg: '#f3f4f6', color: '#6b7280', label: 'нет данных' },
};

// Порядок «худшести» статуса — для подсветки строки по наихудшему из двух.
const STATUS_RANK = { overdue: 3, soon: 2, ok: 1, none: 0 };

export const SafetyModule = {
    isInitialized: false,
    structure: { sources: [], kes_groups: [], departments: [] },

    async init() {
        const root = document.getElementById('safety');
        if (!root) return;

        this.cacheDOM();

        if (!this.isInitialized) {
            this.bindEvents();
            this.isInitialized = true;
        }

        await this.loadStructure();
        await this.reload();
    },

    // Вызывается app.js::refreshModuleData при возврате на вкладку.
    refresh() {
        return this.reload();
    },

    cacheDOM() {
        this.dom = {
            stats: document.getElementById('safetyStats'),
            alerts: document.getElementById('safetyAlerts'),
            tableBody: document.getElementById('safetyTableBody'),

            sourceFilter: document.getElementById('safetySourceFilter'),
            kesGroupFilter: document.getElementById('safetyKesGroupFilter'),
            deptFilter: document.getElementById('safetyDeptFilter'),
            statusFilter: document.getElementById('safetyStatusFilter'),
            searchInput: document.getElementById('safetySearchInput'),

            btnRefresh: document.getElementById('btnRefreshSafety'),
            btnAdd: document.getElementById('btnSafetyAdd'),
            btnSeed: document.getElementById('btnSafetySeed'),
        };

        this.modal = {
            window: document.getElementById('safetyModal'),
            title: document.getElementById('safetyModalTitle'),
            form: document.getElementById('safetyForm'),
            linkList: document.getElementById('safetyLinkList'),
            kesGroupList: document.getElementById('safetyKesGroupList'),
            deptList: document.getElementById('safetyDeptList'),
            inputs: {
                id: document.getElementById('safetyEditId'),
                userId: document.getElementById('safetyUserId'),
                source: document.getElementById('safetySource'),
                kesGroup: document.getElementById('safetyKesGroup'),
                department: document.getElementById('safetyDepartment'),
                position: document.getElementById('safetyPosition'),
                fullName: document.getElementById('safetyFullName'),
                otTrainingDate: document.getElementById('safetyOtTrainingDate'),
                otTrainingPeriod: document.getElementById('safetyOtTrainingPeriod'),
                medicalDate: document.getElementById('safetyMedicalDate'),
                medicalPeriod: document.getElementById('safetyMedicalPeriod'),
                medicalType: document.getElementById('safetyMedicalType'),
                soutDate: document.getElementById('safetySoutDate'),
                soutClass: document.getElementById('safetySoutClass'),
                inductionDate: document.getElementById('safetyInductionDate'),
                otInstructionsDate: document.getElementById('safetyOtInstructionsDate'),
                internshipDate: document.getElementById('safetyInternshipDate'),
                ebGroup: document.getElementById('safetyEbGroup'),
                sizNote: document.getElementById('safetySizNote'),
                birthDate: document.getElementById('safetyBirthDate'),
                isActive: document.getElementById('safetyIsActive'),
                note: document.getElementById('safetyNote'),
            },
        };

        // Кеш кандидатов автоподстановки ФИО: «нормализованное ФИО» → user_id.
        // Заполняется при поиске в /link-candidates, читается при сохранении.
        this._linkCandidates = new Map();
    },

    bindEvents() {
        if (this.dom.btnRefresh) this.dom.btnRefresh.addEventListener('click', () => this.reload());

        // Фильтры — при изменении сразу перезагружаем таблицу (summary не трогаем).
        [this.dom.sourceFilter, this.dom.kesGroupFilter, this.dom.deptFilter, this.dom.statusFilter]
            .forEach(sel => { if (sel) sel.addEventListener('change', () => this.loadStaff()); });

        // Поиск с дебаунсом.
        if (this.dom.searchInput) {
            let timer = null;
            this.dom.searchInput.addEventListener('input', () => {
                clearTimeout(timer);
                timer = setTimeout(() => this.loadStaff(), 300);
            });
        }

        if (this.dom.btnAdd) this.dom.btnAdd.addEventListener('click', () => this.openModal());
        if (this.dom.btnSeed) this.dom.btnSeed.addEventListener('click', () => this.seedStructure());

        if (this.modal.form) {
            this.modal.form.addEventListener('submit', (e) => this.handleSave(e));
        }

        // Закрытие модалки (крестик / отмена / клик по фону).
        if (this.modal.window) {
            this.modal.window.addEventListener('click', (e) => {
                if (e.target === this.modal.window || e.target.closest('[data-safety-close]')) {
                    this.modal.window.classList.remove('open');
                }
            });
        }

        // Автоподстановка жильца в поле ФИО (datalist + live-поиск).
        if (this.modal.inputs.fullName) {
            let linkTimer = null;
            this.modal.inputs.fullName.addEventListener('input', () => {
                // Сброс привязки: пользователь редактирует — старый user_id невалиден,
                // пока совпадение не подтвердится по тексту в handleSave.
                this.modal.inputs.userId.value = '';
                const q = this.modal.inputs.fullName.value.trim();
                clearTimeout(linkTimer);
                if (q.length < 2) return;
                linkTimer = setTimeout(() => this.loadLinkCandidates(q), 300);
            });
        }
    },

    // ── Структура: источники / КЭС-группы / подразделения для фильтров и форм ──
    async loadStructure() {
        try {
            const data = await api.get('/admin/ot/structure');
            this.structure = {
                sources: data.sources || [],
                kes_groups: data.kes_groups || [],
                departments: data.departments || [],
            };
        } catch (e) {
            this.structure = { sources: [], kes_groups: [], departments: [] };
        }
        this.fillStructureControls();
    },

    fillStructureControls() {
        // Источник — известный набор, подписи из SOURCE_LABELS (fallback на raw).
        if (this.dom.sourceFilter) {
            const prev = this.dom.sourceFilter.value;
            this.dom.sourceFilter.innerHTML = '';
            this.dom.sourceFilter.appendChild(new Option('Все источники', ''));
            (this.structure.sources || []).forEach(s => {
                this.dom.sourceFilter.appendChild(new Option(SOURCE_LABELS[s] || s, s));
            });
            if (prev) this.dom.sourceFilter.value = prev;
        }

        const fillSelect = (sel, items, allLabel) => {
            if (!sel) return;
            const prev = sel.value;
            sel.innerHTML = '';
            sel.appendChild(new Option(allLabel, ''));
            (items || []).forEach(v => sel.appendChild(new Option(v, v)));
            if (prev) sel.value = prev;
        };
        fillSelect(this.dom.kesGroupFilter, this.structure.kes_groups, 'Все группы');
        fillSelect(this.dom.deptFilter, this.structure.departments, 'Все подразделения');

        // datalist в форме.
        const fillDatalist = (list, items) => {
            if (!list) return;
            list.innerHTML = '';
            (items || []).forEach(v => {
                const opt = document.createElement('option');
                opt.value = String(v);
                list.appendChild(opt);
            });
        };
        fillDatalist(this.modal.kesGroupList, this.structure.kes_groups);
        fillDatalist(this.modal.deptList, this.structure.departments);
    },

    // ── Перезагрузка всего экрана: KPI + сигналы + таблица ──
    async reload() {
        await Promise.all([
            this.loadSummary(),
            this.loadStaff(),
        ]);
    },

    async loadSummary() {
        if (!this.dom.stats) return;
        try {
            const s = await api.get('/admin/ot/summary');
            this.renderSummary(s);
            this.renderAlerts(s.alerts || []);
        } catch (e) {
            this.dom.stats.innerHTML = `<div style="padding:14px; color:var(--danger-color); grid-column:1/-1;">Ошибка загрузки аналитики: ${escapeHtml(e.message)}</div>`;
            if (this.dom.alerts) {
                this.dom.alerts.innerHTML = `<div style="padding:12px; color:var(--danger-color);">Ошибка: ${escapeHtml(e.message)}</div>`;
            }
        }
    },

    renderSummary(s) {
        const card = (bg, color, icon, value, label) => `
            <div style="background:${bg}; border-radius:10px; padding:14px 12px; border:1px solid ${color}33;">
                <div style="display:flex; align-items:center; gap:8px; color:${color}; font-size:12px; margin-bottom:4px;">
                    <span style="font-size:16px;">${icon}</span>${label}
                </div>
                <div style="font-size:22px; font-weight:700; color:#111827;">${value}</div>
            </div>
        `;
        // «Просрочено» / «Скоро» — сумма по обучению и медосмотрам.
        const ot = s.ot_training || {};
        const med = s.medical || {};
        const overdue = (ot.overdue || 0) + (med.overdue || 0);
        const soon = (ot.soon || 0) + (med.soon || 0);
        this.dom.stats.innerHTML = [
            card('#eff6ff', '#2563eb', '🗂', s.total ?? 0, 'Всего должностей'),
            card('#ecfdf5', '#10b981', '✅', s.filled ?? 0, 'Заполнено ФИО'),
            card('#f3f4f6', '#6b7280', '🚪', s.vacant ?? 0, 'Вакансий'),
            card('#fef2f2', '#dc2626', '🔴', overdue, 'Просрочено'),
            card('#fff7ed', '#ea580c', '🟠', soon, 'Скоро'),
        ].join('');
    },

    renderAlerts(alerts) {
        if (!this.dom.alerts) return;
        if (!alerts.length) {
            this.dom.alerts.innerHTML = `<div style="padding:14px; text-align:center; color:var(--text-secondary); font-style:italic;">Нет приближающихся сроков.</div>`;
            return;
        }
        this.dom.alerts.innerHTML = '';
        alerts.forEach(a => {
            const overdue = a.status === 'overdue';
            const accent = overdue ? '#dc2626' : '#ea580c';
            const bg = overdue ? '#fef2f2' : '#fff7ed';
            const statusText = overdue ? 'просрочено' : `скоро, до ${this.formatDate(a.due)}`;
            const node = el('div', {
                style: {
                    background: bg,
                    borderLeft: `3px solid ${accent}`,
                    borderRadius: '6px',
                    padding: '8px 10px',
                    fontSize: '12px',
                },
            },
                el('div', { style: { fontWeight: '600', color: '#1f2937' } }, a.full_name || '—'),
                el('div', { style: { color: 'var(--text-secondary)', marginTop: '2px' } },
                    [a.position, a.department].filter(Boolean).join(' · ') || '—'),
                el('div', { style: { marginTop: '4px', display: 'flex', gap: '6px', alignItems: 'center', flexWrap: 'wrap' } },
                    el('span', {
                        style: {
                            background: accent, color: '#fff', borderRadius: '8px',
                            padding: '1px 8px', fontSize: '11px', fontWeight: '600',
                        },
                    }, a.kind || '—'),
                    el('span', { style: { color: accent, fontWeight: '600' } }, statusText)
                )
            );
            this.dom.alerts.appendChild(node);
        });
    },

    // ── Таблица сотрудников ──
    _staffParams() {
        const p = {};
        if (this.dom.sourceFilter?.value) p.source = this.dom.sourceFilter.value;
        if (this.dom.kesGroupFilter?.value) p.kes_group = this.dom.kesGroupFilter.value;
        if (this.dom.deptFilter?.value) p.department = this.dom.deptFilter.value;
        if (this.dom.statusFilter?.value) p.status = this.dom.statusFilter.value;
        const q = (this.dom.searchInput?.value || '').trim();
        if (q) p.q = q;
        return p;
    },

    async loadStaff() {
        if (!this.dom.tableBody) return;
        const params = this._staffParams();
        const qs = Object.keys(params).length ? `?${new URLSearchParams(params)}` : '';
        this.dom.tableBody.innerHTML = `<tr><td colspan="8" class="text-center" style="padding:24px; color:var(--text-secondary);"><i class="fa-solid fa-spinner fa-spin"></i> Загрузка…</td></tr>`;
        try {
            const data = await api.get(`/admin/ot/staff${qs}`);
            const items = data.items || [];
            const total = data.total ?? items.length;

            // Кнопка импорта структуры — заметна только когда реестр пуст
            // и фильтры не выставлены (иначе «пусто» — это результат фильтра).
            const noFilters = !qs;
            if (this.dom.btnSeed) {
                this.dom.btnSeed.style.display = (noFilters && total === 0) ? '' : 'none';
            }

            this.renderStaff(items);
        } catch (e) {
            this.dom.tableBody.innerHTML = `<tr><td colspan="8" class="text-center" style="padding:24px; color:var(--danger-color);">Ошибка загрузки: ${escapeHtml(e.message)}</td></tr>`;
        }
    },

    renderStaff(items) {
        this.dom.tableBody.innerHTML = '';
        if (!items.length) {
            this.dom.tableBody.innerHTML = `<tr><td colspan="8" class="text-center" style="padding:36px; color:var(--text-secondary); font-style:italic;">Записи не найдены. Измените фильтры или добавьте должность.</td></tr>`;
            return;
        }
        items.forEach(item => this.dom.tableBody.appendChild(this.renderStaffRow(item)));
    },

    renderStaffRow(item) {
        const otStatus = item.ot_status || 'none';
        const medStatus = item.medical_status || 'none';

        // Подсветка строки по наихудшему из двух статусов.
        const worst = STATUS_RANK[otStatus] >= STATUS_RANK[medStatus] ? otStatus : medStatus;
        const rowTint = worst === 'overdue' ? '#fef2f2'
            : worst === 'soon' ? '#fff7ed' : '';

        const fioCell = item.full_name
            ? el('td', { style: { fontWeight: '600', color: '#1f2937' } }, item.full_name)
            : el('td', { style: { color: '#9ca3af', fontStyle: 'italic' } }, 'вакансия');

        const row = el('tr', {
            class: 'hover:bg-gray-50 transition-colors',
            style: rowTint ? { background: rowTint } : {},
        },
            el('td', { class: 'text-sm' }, item.department || '—'),
            el('td', {}, item.position || '—'),
            fioCell,
            el('td', {}, this.dateWithBadge(item.ot_training_date, otStatus, item.next_ot_training)),
            el('td', {}, this.dateWithBadge(item.medical_date, medStatus, item.next_medical)),
            el('td', { class: 'text-sm' }, this.soutCell(item)),
            el('td', { class: 'text-center text-sm' }, item.eb_group || '—'),
            el('td', { class: 'text-center' },
                el('button', {
                    class: 'btn-icon', title: 'Редактировать',
                    style: { background: 'transparent', border: 'none', cursor: 'pointer', marginRight: '4px' },
                    onclick: () => this.openModal(item),
                }, '✏️'),
                el('button', {
                    class: 'btn-icon btn-delete', title: 'Удалить',
                    style: { background: 'transparent', border: 'none', cursor: 'pointer' },
                    onclick: () => this.deleteRecord(item),
                }, '🗑')
            )
        );
        return row;
    },

    // Ячейка «дата + бейдж статуса». При наличии next-даты показываем её в title.
    dateWithBadge(date, status, nextDate) {
        const st = STATUS_STYLE[status] || STATUS_STYLE.none;
        const wrap = el('div', { style: { display: 'flex', flexDirection: 'column', gap: '3px' } },
            el('span', { class: 'text-sm' }, this.formatDate(date)),
            el('span', {
                title: nextDate ? `Следующий срок: ${this.formatDate(nextDate)}` : '',
                style: {
                    alignSelf: 'flex-start',
                    background: st.bg, color: st.color,
                    borderRadius: '8px', padding: '1px 8px',
                    fontSize: '11px', fontWeight: '600',
                },
            }, st.label)
        );
        return wrap;
    },

    soutCell(item) {
        const d = this.formatDate(item.sout_date);
        const cls = item.sout_class ? ` (кл. ${item.sout_class})` : '';
        if (d === '—' && !item.sout_class) return '—';
        return `${d}${cls}`;
    },

    // ── Автоподстановка жильца по ФИО ──
    async loadLinkCandidates(q) {
        try {
            const data = await api.get(`/admin/ot/link-candidates?q=${encodeURIComponent(q)}`);
            const items = data.items || [];
            const list = this.modal.linkList;
            if (!list) return;
            list.innerHTML = '';
            this._linkCandidates.clear();
            items.forEach(c => {
                const opt = document.createElement('option');
                opt.value = String(c.username);
                list.appendChild(opt);
                // Ключ — нормализованное (lower/trim) ФИО для матчинга при сохранении.
                this._linkCandidates.set(String(c.username).trim().toLowerCase(), c.id);
            });
        } catch (e) {
            // Тихо — автоподстановка не критична.
        }
    },

    // ── Модалка ред./создания ──
    openModal(item = null) {
        const inp = this.modal.inputs;
        this.modal.form.reset();
        this.modal.linkList.innerHTML = '';
        this._linkCandidates.clear();

        if (item) {
            this.modal.title.textContent = 'Редактировать должность';
            inp.id.value = item.id;
            inp.userId.value = item.user_id ?? '';
            inp.source.value = item.source || 'osnovnoy_shtat';
            inp.kesGroup.value = item.kes_group || '';
            inp.department.value = item.department || '';
            inp.position.value = item.position || '';
            inp.fullName.value = item.full_name || '';
            inp.otTrainingDate.value = this.toInputDate(item.ot_training_date);
            inp.otTrainingPeriod.value = item.ot_training_period_months ?? '';
            inp.medicalDate.value = this.toInputDate(item.medical_date);
            inp.medicalPeriod.value = item.medical_period_months ?? '';
            inp.medicalType.value = item.medical_type || '';
            inp.soutDate.value = this.toInputDate(item.sout_date);
            inp.soutClass.value = item.sout_class || '';
            inp.inductionDate.value = this.toInputDate(item.induction_date);
            inp.otInstructionsDate.value = this.toInputDate(item.ot_instructions_date);
            inp.internshipDate.value = this.toInputDate(item.internship_date);
            inp.ebGroup.value = item.eb_group || '';
            inp.sizNote.value = item.siz_note || '';
            inp.birthDate.value = this.toInputDate(item.birth_date);
            inp.isActive.checked = item.is_active !== false;
            inp.note.value = item.note || '';
        } else {
            this.modal.title.textContent = 'Добавить должность';
            inp.id.value = '';
            inp.userId.value = '';
            inp.source.value = 'osnovnoy_shtat';
            inp.isActive.checked = true;
            // Префилл фильтрами текущего вида — удобно при массовом вводе.
            if (this.dom.kesGroupFilter?.value) inp.kesGroup.value = this.dom.kesGroupFilter.value;
            if (this.dom.deptFilter?.value) inp.department.value = this.dom.deptFilter.value;
            if (this.dom.sourceFilter?.value) inp.source.value = this.dom.sourceFilter.value;
        }
        this.modal.window.classList.add('open');
    },

    async handleSave(e) {
        e.preventDefault();
        const btn = this.modal.form.querySelector('.confirm-btn');
        const inp = this.modal.inputs;
        const id = inp.id.value;

        const position = (inp.position.value || '').trim();
        if (!position) return toast('Укажите должность', 'error');

        const fullName = (inp.fullName.value || '').trim();

        // Привязка к жильцу: если введённое ФИО точно совпало с кандидатом из
        // /link-candidates — берём его user_id. Иначе — вакансия (user_id=null).
        let userId = null;
        if (fullName) {
            const matched = this._linkCandidates.get(fullName.toLowerCase());
            if (matched != null) userId = matched;
            else if (inp.userId.value) userId = parseInt(inp.userId.value);
        }

        const numOrNull = (v) => {
            const s = (v || '').trim();
            if (!s) return null;
            const n = parseInt(s);
            return Number.isNaN(n) ? null : n;
        };
        const strOrNull = (v) => {
            const s = (v || '').trim();
            return s || null;
        };

        const data = {
            source: inp.source.value || 'osnovnoy_shtat',
            kes_group: strOrNull(inp.kesGroup.value),
            department: strOrNull(inp.department.value),
            position: position,
            full_name: fullName || null,
            user_id: userId,
            birth_date: this.fromInputDate(inp.birthDate.value),
            sout_date: this.fromInputDate(inp.soutDate.value),
            sout_class: strOrNull(inp.soutClass.value),
            induction_date: this.fromInputDate(inp.inductionDate.value),
            ot_instructions_date: this.fromInputDate(inp.otInstructionsDate.value),
            internship_date: this.fromInputDate(inp.internshipDate.value),
            siz_note: strOrNull(inp.sizNote.value),
            eb_group: strOrNull(inp.ebGroup.value),
            ot_training_date: this.fromInputDate(inp.otTrainingDate.value),
            ot_training_period_months: numOrNull(inp.otTrainingPeriod.value),
            medical_date: this.fromInputDate(inp.medicalDate.value),
            medical_type: strOrNull(inp.medicalType.value),
            medical_period_months: numOrNull(inp.medicalPeriod.value),
            note: strOrNull(inp.note.value),
            is_active: !!inp.isActive.checked,
        };

        setLoading(btn, true, 'Сохранение...');
        try {
            if (id) {
                await api.put(`/admin/ot/staff/${id}`, data);
                toast('Запись обновлена', 'success');
            } else {
                await api.post('/admin/ot/staff', data);
                toast('Должность добавлена', 'success');
            }
            this.modal.window.classList.remove('open');
            await this.loadStructure();
            await this.reload();
        } catch (err) {
            toast(err.message, 'error');
        } finally {
            setLoading(btn, false);
        }
    },

    async deleteRecord(item) {
        const label = item.full_name || item.position || `#${item.id}`;
        if (!await showConfirm(
            `Удалить запись «${label}»?`,
            { title: 'Удаление записи', danger: true, confirmText: 'Удалить' }
        )) return;
        try {
            await api.delete(`/admin/ot/staff/${item.id}`);
            toast('Запись удалена', 'success');
            await this.reload();
        } catch (e) {
            toast(e.message, 'error');
        }
    },

    // ── Импорт структуры из шаблонов ──
    async seedStructure() {
        if (!await showConfirm(
            'Создать реестр должностей из встроенных шаблонов (основной штат + КЭС-2025)?\n\nЗапускать имеет смысл, когда реестр пуст.',
            { title: 'Импорт структуры', confirmText: 'Импортировать' }
        )) return;
        setLoading(this.dom.btnSeed, true, 'Импорт...');
        try {
            const res = await api.post('/admin/ot/seed?force=false');
            if (res.status === 'seeded') {
                toast(`Структура импортирована. Добавлено записей: ${res.inserted ?? 0}`, 'success');
            } else {
                toast('Импорт пропущен: реестр уже не пуст.', 'info');
            }
            await this.loadStructure();
            await this.reload();
        } catch (e) {
            toast('Ошибка импорта: ' + e.message, 'error');
        } finally {
            setLoading(this.dom.btnSeed, false, 'Импортировать структуру');
        }
    },

    // ── Утилиты дат ──
    // Отображение: ISO-дата (или null) → «дд.мм.гггг» либо «—».
    formatDate(iso) {
        if (!iso) return '—';
        const d = new Date(iso);
        if (Number.isNaN(d.getTime())) return '—';
        return d.toLocaleDateString('ru-RU');
    },

    // ISO/дата → значение для <input type=date> (YYYY-MM-DD) либо ''.
    toInputDate(iso) {
        if (!iso) return '';
        // Уже в формате YYYY-MM-DD — берём первые 10 символов без сдвига TZ.
        const s = String(iso);
        if (/^\d{4}-\d{2}-\d{2}/.test(s)) return s.slice(0, 10);
        const d = new Date(iso);
        if (Number.isNaN(d.getTime())) return '';
        const pad = (n) => String(n).padStart(2, '0');
        return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
    },

    // Значение <input type=date> → ISO-дата (YYYY-MM-DD) либо null.
    fromInputDate(val) {
        const s = (val || '').trim();
        return s || null;
    },
};
