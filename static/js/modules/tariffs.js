// static/js/modules/tariffs.js
import { api } from '../core/api.js';
import { setLoading, toast, escapeHtml } from '../core/dom.js';

export const TariffsModule = {
    isInitialized: false,
    tariffsList: [], // Массив для хранения всех загруженных профилей

    // Карта соответствия: ключ_в_БД -> id_в_HTML для числовых значений
    MAPPING: {
        maintenance_repair: 't_main',
        social_rent: 't_rent',
        waste_disposal: 't_waste',
        heating: 't_heat',
        water_heating: 't_w_heat',
        water_supply: 't_w_sup',
        sewage: 't_sew',
        electricity_rate: 't_el_rate',
        electricity_per_sqm: 't_el_sqm',
        // Сумма за «койко-место» (для холостяков, billing_mode='per_capita').
        // 0 = тариф для одиночек не применяется.
        per_capita_amount: 't_per_capita',
    },

    // Утилита: форматировать дату для отображения
    _formatDate(isoStr) {
        if (!isoStr) return '—';
        try {
            return new Date(isoStr).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' });
        } catch { return isoStr; }
    },

    init() {
        this.cacheDOM();

        // События вешаем только один раз
        if (!this.isInitialized) {
            this.bindEvents();
            this.isInitialized = true;
        }

        // Данные загружаем каждый раз при открытии вкладки
        this.load();
        this.loadSchedule();
        this.loadScheduledTariffs();
    },

    cacheDOM() {
        this.dom = {
            form: document.getElementById('tariffsForm'),
            selector: document.getElementById('tariffSelector'),
            btnCreate: document.getElementById('btnCreateNewTariff'),
            btnDelete: document.getElementById('btnDeleteTariff'),
            inputId: document.getElementById('t_id'),
            inputName: document.getElementById('t_name'),
            inputEffectiveFrom: document.getElementById('t_effective_from'),
            scheduledAlert: document.getElementById('tariffScheduledAlert'),
            scheduledCard: document.getElementById('scheduledTariffsCard'),
            scheduledList: document.getElementById('scheduledTariffsList'),
            btnRefreshScheduled: document.getElementById('btnRefreshScheduled'),
            // Калькулятор
            previewArea: document.getElementById('prev_area'),
            previewResidents: document.getElementById('prev_residents'),
            previewHot: document.getElementById('prev_hot'),
            previewCold: document.getElementById('prev_cold'),
            previewElect: document.getElementById('prev_elect'),
            previewResult: document.getElementById('tariffPreviewResult'),
            // «Где применяется» + массовая привязка
            usageBlock: document.getElementById('tariffUsageBlock'),
            usageStats: document.getElementById('tariffUsageStats'),
            usageDetails: document.getElementById('tariffUsageDetails'),
            assignDormSelect: document.getElementById('assignDormSelect'),
            btnAssignDorm: document.getElementById('btnAssignDorm'),
            btnUnassignDorm: document.getElementById('btnUnassignDorm'),
        };
    },

    bindEvents() {
        if (this.dom.form) {
            this.dom.form.addEventListener('submit', (e) => this.handleSubmit(e));
        }
        if (this.dom.selector) {
            this.dom.selector.addEventListener('change', (e) => this.handleSelectChange(e));
        }
        if (this.dom.btnCreate) {
            this.dom.btnCreate.addEventListener('click', (e) => {
                e.preventDefault();
                this.clearFormForNew();
            });
        }
        if (this.dom.btnDelete) {
            this.dom.btnDelete.addEventListener('click', (e) => {
                e.preventDefault();
                this.handleDelete();
            });
        }
        if (this.dom.btnRefreshScheduled) {
            this.dom.btnRefreshScheduled.addEventListener('click', () => this.loadScheduledTariffs());
        }
        // Показываем/скрываем предупреждение когда меняется дата effective_from
        if (this.dom.inputEffectiveFrom) {
            this.dom.inputEffectiveFrom.addEventListener('change', () => this._updateScheduledAlert());
        }

        // Событие для кнопки сохранения графика
        const btnSaveSchedule = document.getElementById('btnSaveSchedule');
        if (btnSaveSchedule) {
            btnSaveSchedule.addEventListener('click', () => this.saveSchedule());
        }

        // Калькулятор-превью: пересчитываем при любом изменении.
        // Дебаунс 250ms — пользователь печатает в полях тарифа, не нагружаем сервер.
        const debouncedPreview = this._debounce(() => this.recalcPreview(), 250);
        const previewInputs = [
            this.dom.previewArea, this.dom.previewResidents,
            this.dom.previewHot, this.dom.previewCold, this.dom.previewElect,
        ];
        previewInputs.forEach(i => i?.addEventListener('input', debouncedPreview));
        // Любое изменение цены тарифа тоже триггерит превью
        Object.values(this.MAPPING).forEach(htmlId => {
            const inp = document.getElementById(htmlId);
            inp?.addEventListener('input', debouncedPreview);
        });

        // Массовая привязка к общежитию
        this.dom.btnAssignDorm?.addEventListener('click', () => this.assignToDormitory(false));
        this.dom.btnUnassignDorm?.addEventListener('click', () => this.assignToDormitory(true));
    },

    _debounce(fn, ms = 250) {
        let t;
        return (...args) => {
            clearTimeout(t);
            t = setTimeout(() => fn(...args), ms);
        };
    },

    _updateScheduledAlert() {
        if (!this.dom.scheduledAlert || !this.dom.inputEffectiveFrom) return;
        const val = this.dom.inputEffectiveFrom.value;
        if (val && new Date(val) > new Date()) {
            this.dom.scheduledAlert.style.display = 'block';
        } else {
            this.dom.scheduledAlert.style.display = 'none';
        }
    },

    async loadSchedule() {
        try {
            const data = await api.getCached('/settings/submission-period', { ttlSeconds: 600 });
            document.getElementById('setStartDay').value = data.start_day;
            document.getElementById('setEndDay').value = data.end_day;
        } catch (e) {
            console.warn('Failed to load submission schedule settings', e);
            toast('Не удалось загрузить график', 'error');
        }
    },

    async saveSchedule() {
        const btn = document.getElementById('btnSaveSchedule');
        const start = parseInt(document.getElementById('setStartDay').value);
        const end = parseInt(document.getElementById('setEndDay').value);

        if (isNaN(start) || isNaN(end)) {
            toast('Введите корректные числа', 'error');
            return;
        }

        setLoading(btn, true, 'Сохранение...');
        try {
            await api.post('/settings/submission-period', { start_day: start, end_day: end });
            api.invalidateCache('/settings/submission-period');
            toast('График успешно обновлен', 'success');
        } catch (e) {
            toast(e.message, 'error');
        } finally {
            setLoading(btn, false, 'Сохранить график');
        }
    },

    async load(selectedId = null) {
        try {
            // ИСПРАВЛЕНИЕ: Используем новый endpoint /with-stats для получения
            // количества жильцов на каждом тарифе. Это позволяет администратору
            // видеть в селекторе сколько людей затронет изменение тарифа.
            this.tariffsList = await api.get('/tariffs/with-stats');
            this.populateSelector();

            if (this.tariffsList.length > 0) {
                // Если не передан конкретный ID для выбора, берем первый (базовый)
                const targetId = selectedId || this.tariffsList[0].id;
                this.dom.selector.value = targetId;
                this.fillForm(targetId);
            } else {
                this.clearFormForNew();
            }
        } catch (error) {
            // Fallback: если /with-stats не доступен, используем обычный endpoint
            try {
                this.tariffsList = await api.get('/tariffs');
                this.populateSelector();
                if (this.tariffsList.length > 0) {
                    const targetId = selectedId || this.tariffsList[0].id;
                    this.dom.selector.value = targetId;
                    this.fillForm(targetId);
                }
            } catch (fallbackError) {
                toast('Не удалось загрузить тарифы: ' + fallbackError.message, 'error');
            }
        }
    },

    populateSelector() {
        if (!this.dom.selector) return;
        this.dom.selector.innerHTML = '';

        this.tariffsList.forEach(t => {
            const opt = document.createElement('option');
            opt.value = t.id;

            // ИСПРАВЛЕНИЕ: Показываем количество жильцов на тарифе в селекторе.
            // Администратор видит "Базовый тариф (12 чел.)" вместо просто "Базовый тариф".
            // Это критично перед редактированием или удалением — сразу видно масштаб влияния.
            if (t.user_count !== undefined && t.user_count > 0) {
                opt.textContent = `${t.name} (${t.user_count} чел.)`;
            } else if (t.user_count !== undefined) {
                opt.textContent = `${t.name} (нет жильцов)`;
            } else {
                opt.textContent = t.name;
            }

            this.dom.selector.appendChild(opt);
        });
    },

    handleSelectChange(e) {
        const id = parseInt(e.target.value);
        if (!isNaN(id)) {
            this.fillForm(id);
        }
    },

    fillForm(id) {
        const tariff = this.tariffsList.find(t => t.id === id);
        if (!tariff) return;

        // Заполняем системные поля (ID и Название)
        if (this.dom.inputId) this.dom.inputId.value = tariff.id;
        if (this.dom.inputName) this.dom.inputName.value = tariff.name;

        // Дата вступления в силу
        if (this.dom.inputEffectiveFrom) {
            this.dom.inputEffectiveFrom.value = tariff.effective_from
                ? tariff.effective_from.substring(0, 16)  // "YYYY-MM-DDTHH:MM"
                : '';
        }
        this._updateScheduledAlert();

        // Проходим по нашей карте и заполняем инпуты с ценами
        for (const [dbKey, htmlId] of Object.entries(this.MAPPING)) {
            const input = document.getElementById(htmlId);
            if (input && tariff[dbKey] !== undefined) {
                input.value = Number(tariff[dbKey]).toFixed(2);
            }
        }

        // Базовый тариф (id=1) удалять нельзя, прячем кнопку
        if (this.dom.btnDelete) {
            this.dom.btnDelete.style.display = (tariff.id === 1) ? 'none' : 'block';
        }

        // Загружаем «Где применяется» + список общежитий + перерисовываем превью.
        this.loadUsage(tariff.id);
        this.loadDormitoriesForAssign();
        this.recalcPreview();
    },

    clearFormForNew() {
        if (this.dom.selector) this.dom.selector.value = "";
        if (this.dom.inputId) this.dom.inputId.value = "";
        if (this.dom.inputName) this.dom.inputName.value = "Новый профиль";
        if (this.dom.inputEffectiveFrom) this.dom.inputEffectiveFrom.value = "";
        if (this.dom.scheduledAlert) this.dom.scheduledAlert.style.display = 'none';

        for (const htmlId of Object.values(this.MAPPING)) {
            const input = document.getElementById(htmlId);
            if (input) input.value = "0.00";
        }

        if (this.dom.btnDelete) this.dom.btnDelete.style.display = 'none';
        // Скрываем «Где применяется» — у нового тарифа ещё некого
        if (this.dom.usageBlock) this.dom.usageBlock.style.display = 'none';
        this.recalcPreview();
    },

    // ====================================================================
    // КАЛЬКУЛЯТОР-ПРЕВЬЮ
    // ====================================================================
    /** Берёт текущие значения формы тарифа, дёргает /tariffs/preview, рисует разбивку. */
    async recalcPreview() {
        if (!this.dom.previewResult) return;

        // Собираем тариф из формы (а не из БД) — превью должен реагировать
        // на изменения в реальном времени, до сохранения.
        const tariffData = {};
        for (const [dbKey, htmlId] of Object.entries(this.MAPPING)) {
            const inp = document.getElementById(htmlId);
            tariffData[dbKey] = inp ? (parseFloat(inp.value) || 0) : 0;
        }
        const num = (id, def) => parseFloat(document.getElementById(id)?.value) || def;
        const payload = {
            tariff_data: tariffData,
            apartment_area: num('prev_area', 18),
            residents_count: parseInt(document.getElementById('prev_residents')?.value) || 1,
            total_room_residents: parseInt(document.getElementById('prev_residents')?.value) || 1,
            volume_hot: num('prev_hot', 3),
            volume_cold: num('prev_cold', 5),
            volume_electricity: num('prev_elect', 100),
        };

        try {
            const r = await api.post('/tariffs/preview', payload);
            this._renderPreview(r);
        } catch (e) {
            this.dom.previewResult.innerHTML = `<span style="color:var(--danger-color);">Ошибка калькулятора: ${escapeHtml(e.message)}</span>`;
        }
    },

    _renderPreview(r) {
        const fmt = v => Number(v || 0).toLocaleString('ru-RU', {
            minimumFractionDigits: 2, maximumFractionDigits: 2,
        }) + ' ₽';
        const rowsHtml = Object.entries(r.breakdown)
            .filter(([k, v]) => k !== 'total_cost' && Number(v) !== 0)
            .map(([k, v]) => `
                <div style="display:flex; justify-content:space-between; padding:3px 0; font-size:12px;">
                    <span style="color:var(--text-secondary);">${escapeHtml(this._labelForCost(k))}</span>
                    <span style="font-family:monospace;">${fmt(v)}</span>
                </div>`).join('');
        this.dom.previewResult.innerHTML = `
            <div style="display:grid; grid-template-columns:1fr 1fr; gap:14px;">
                <div>${rowsHtml || '<span style="color:var(--text-secondary);">Все компоненты = 0</span>'}</div>
                <div style="border-left:1px dashed var(--border-color); padding-left:14px;">
                    <div style="display:flex; justify-content:space-between; font-size:12px; color:var(--text-secondary);">
                        <span>Счёт 209 (комм.)</span><span style="font-family:monospace;">${fmt(r.total_209)}</span>
                    </div>
                    <div style="display:flex; justify-content:space-between; font-size:12px; color:var(--text-secondary); margin-top:4px;">
                        <span>Счёт 205 (наём)</span><span style="font-family:monospace;">${fmt(r.total_205)}</span>
                    </div>
                    <hr style="margin:8px 0; border:none; border-top:1px solid var(--border-color);">
                    <div style="display:flex; justify-content:space-between; font-size:14px; font-weight:700;">
                        <span>ИТОГО</span><span style="color:#059669;">${fmt(r.total_cost)}</span>
                    </div>
                </div>
            </div>`;
    },

    _labelForCost(key) {
        return ({
            cost_hot_water: 'ГВС', cost_cold_water: 'ХВС', cost_sewage: 'Водоотв.',
            cost_electricity: 'Электр.', cost_maintenance: 'Содержание', cost_social_rent: 'Наём',
            cost_waste: 'ТКО', cost_fixed_part: 'Отопление',
        })[key] || key;
    },

    // ====================================================================
    // «ГДЕ ПРИМЕНЯЕТСЯ» + МАССОВАЯ ПРИВЯЗКА К ОБЩЕЖИТИЮ
    // ====================================================================
    async loadUsage(tariffId) {
        if (!this.dom.usageBlock) return;
        this.dom.usageBlock.style.display = 'block';
        this.dom.usageStats.innerHTML = '<span style="color:var(--text-secondary);">Загрузка…</span>';
        this.dom.usageDetails.innerHTML = '';
        try {
            const data = await api.get(`/tariffs/${tariffId}/usage`);
            this._renderUsage(data);
        } catch (e) {
            this.dom.usageStats.innerHTML = `<span style="color:var(--danger-color);">Ошибка: ${escapeHtml(e.message)}</span>`;
        }
    },

    _renderUsage(d) {
        const stat = (label, value, color) => `
            <div style="background:var(--bg-page); padding:10px 12px; border-radius:8px;">
                <div style="font-size:11px; color:var(--text-secondary); text-transform:uppercase;">${escapeHtml(label)}</div>
                <div style="font-size:20px; font-weight:700; color:${color};">${value}</div>
            </div>`;
        this.dom.usageStats.innerHTML = [
            stat('Привязано комнат',          d.by_room.rooms_count, '#3b82f6'),
            stat('Жильцов в этих комнатах',   d.by_room.users_in_rooms, '#10b981'),
            stat('Персональная привязка',     d.by_user_direct.count, '#7c3aed'),
            d.fallback_default_users
                ? stat('На дефолте', d.fallback_default_users, '#f59e0b')
                : '',
            stat('Всего применяется',         d.total_effective, '#059669'),
        ].filter(Boolean).join('');

        let detailsHtml = '';
        if (d.by_room.by_dormitory.length) {
            detailsHtml += `
                <div style="margin-bottom:8px;">
                    <strong style="font-size:12px;">По общежитиям:</strong>
                    <div style="margin-top:4px;">
                        ${d.by_room.by_dormitory.map(g => `
                            <span style="display:inline-block; margin:2px; padding:3px 8px; background:#dbeafe; color:#1e40af; border-radius:10px; font-size:11px; font-weight:600;">
                                <i class="fa-solid fa-building"></i> ${escapeHtml(g.dormitory)} — ${g.rooms_count} комн.
                            </span>`).join('')}
                    </div>
                </div>`;
        }
        if (d.by_user_direct.count > 0) {
            detailsHtml += `<div style="font-size:11px; color:var(--text-secondary); margin-top:8px;">
                Жильцы с персональной привязкой имеют ПРИОРИТЕТ ниже комнатной — если у их комнаты задан другой тариф, применится комнатный.
            </div>`;
        }
        this.dom.usageDetails.innerHTML = detailsHtml || '';
    },

    /** Загружает уникальные общежития из существующих тарифов/комнат
     * (через usage всех тарифов). Простой подход: дёргаем уже знакомый
     * /housing/dormitories или (если его нет) собираем из usage активного
     * тарифа. Здесь возьмём с housing endpoint — он есть в проекте. */
    async loadDormitoriesForAssign() {
        if (!this.dom.assignDormSelect) return;
        // Один раз грузим
        if (this.dom.assignDormSelect.dataset.loaded) return;
        try {
            const data = await api.get('/rooms/dormitories');
            const list = Array.isArray(data) ? data : (data.items || []);
            const opts = list.map(d => {
                const name = typeof d === 'string' ? d : (d.name || d.dormitory_name || d.dormitory);
                return `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`;
            }).join('');
            this.dom.assignDormSelect.innerHTML =
                '<option value="">Выберите общежитие…</option>' + opts;
            this.dom.assignDormSelect.dataset.loaded = '1';
        } catch (e) {
            // Фоллбек: если такого endpoint нет, пробуем взять список из текущего usage —
            // у других тарифов могут быть привязки, в которых видны общежития. Не критично.
            console.warn('Не удалось загрузить список общежитий:', e.message);
        }
    },

    async assignToDormitory(unassign = false) {
        const dorm = this.dom.assignDormSelect?.value;
        if (!dorm) return toast('Выберите общежитие', 'warning');
        const tariffId = parseInt(this.dom.inputId?.value);
        if (!unassign && !tariffId) return toast('Сначала выберите тариф', 'warning');

        const action = unassign
            ? `снять комнатный тариф со ВСЕХ комнат общежития «${dorm}»?\nЖильцы вернутся на персональный тариф (или дефолтный).`
            : `привязать тариф «${this.dom.inputName?.value || ''}» ко ВСЕМ комнатам общежития «${dorm}»?\nЭто переопределит персональные тарифы жильцов.`;
        if (!confirm('Вы уверены — ' + action)) return;

        try {
            const r = await api.post('/tariffs/assign-to-dormitory', {
                dormitory_name: dorm,
                tariff_id: unassign ? null : tariffId,
            });
            toast(`Готово: затронуто ${r.rooms_affected} комнат`, 'success');
            // Перезагружаем «Где применяется» для всех тарифов — состояние изменилось.
            if (tariffId) this.loadUsage(tariffId);
        } catch (e) {
            toast('Ошибка: ' + e.message, 'error');
        }
    },

    async loadScheduledTariffs() {
        if (!this.dom.scheduledCard || !this.dom.scheduledList) return;
        try {
            const list = await api.get('/tariffs/scheduled');
            if (!list || list.length === 0) {
                this.dom.scheduledCard.style.display = 'none';
                return;
            }
            this.dom.scheduledCard.style.display = 'block';

            // ИСПРАВЛЕНО: t.name приходит из БД и может содержать символы вроде <,>,",
            // которые сломают HTML или дадут XSS. Экранируем перед вставкой.
            const rows = list.map(t => {
                const dateStr = this._formatDate(t.effective_from);
                return `
                    <div style="display:flex; justify-content:space-between; align-items:center; padding:12px 16px; border-bottom:1px solid var(--border-color); flex-wrap:wrap; gap:8px;">
                        <div>
                            <span style="font-weight:600; font-size:14px;">${escapeHtml(t.name)}</span>
                            <span style="margin-left:10px; font-size:12px; background:#fef3c7; color:#92400e; padding:2px 8px; border-radius:12px;">
                                <i class="fa-solid fa-clock"></i> вступает ${escapeHtml(dateStr)}
                            </span>
                        </div>
                        <div style="font-size:12px; color:var(--text-secondary);">
                            Свет: ${Number(t.electricity_rate).toFixed(2)} ₽/кВт ·
                            ГВС: ${Number(t.water_heating).toFixed(2)} ₽/м³ ·
                            ХВС: ${Number(t.water_supply).toFixed(2)} ₽/м³
                        </div>
                    </div>`;
            }).join('');
            this.dom.scheduledList.innerHTML = rows;
        } catch (e) {
            this.dom.scheduledCard.style.display = 'none';
        }
    },

    async handleSubmit(e) {
        e.preventDefault();
        const btnSubmit = this.dom.form.querySelector('button[type="submit"]');

        // Собираем название
        const data = {
            name: this.dom.inputName.value.trim()
        };

        if (!data.name) {
            toast('Введите название тарифа', 'error');
            return;
        }

        // Если есть ID (редактирование) — добавляем его
        const idVal = this.dom.inputId.value;
        if (idVal) {
            data.id = parseInt(idVal);
        }

        // Дата вступления в силу (необязательная)
        const effFrom = this.dom.inputEffectiveFrom ? this.dom.inputEffectiveFrom.value : '';
        data.effective_from = effFrom ? new Date(effFrom).toISOString() : null;

        // Собираем данные цен из формы обратно в объект по карте
        for (const [dbKey, htmlId] of Object.entries(this.MAPPING)) {
            const input = document.getElementById(htmlId);
            if (input) {
                data[dbKey] = parseFloat(input.value) || 0;
            }
        }

        setLoading(btnSubmit, true, 'Сохранение...');

        try {
            const savedTariff = await api.post('/tariffs', data);
            // После создания/обновления тарифа кеш в users.js / housing.js
            // протух — сбрасываем все ключи `/tariffs*`.
            api.invalidateCache('/tariffs');

            const isScheduled = data.effective_from && new Date(data.effective_from) > new Date();
            if (isScheduled) {
                toast(`Тариф запланирован! Вступит в силу ${this._formatDate(data.effective_from)}`, 'success');
            } else {
                toast('Тарифный профиль успешно сохранен!', 'success');
            }

            sessionStorage.removeItem('tariffs_cache');

            // Перезагружаем активные тарифы и запланированные
            await this.load(isScheduled ? null : savedTariff.id);
            await this.loadScheduledTariffs();
        } catch (error) {
            toast('Ошибка сохранения: ' + error.message, 'error');
        } finally {
            setLoading(btnSubmit, false, 'Сохранить изменения тарифа');
        }
    },

    async handleDelete() {
        const idVal = this.dom.inputId.value;
        if (!idVal) return;

        const id = parseInt(idVal);
        if (id === 1) {
            toast('Базовый тариф удалить нельзя', 'error');
            return;
        }

        // ИСПРАВЛЕНИЕ: Показываем администратору сколько жильцов на этом тарифе.
        // Ранее администратор видел просто "Удалить?" — без информации о последствиях.
        // Теперь он знает что при удалении N жильцов будут пересажены на базовый тариф.
        const tariff = this.tariffsList.find(t => t.id === id);
        const tariffName = tariff ? tariff.name : `ID=${id}`;
        const userCount = tariff && tariff.user_count !== undefined ? tariff.user_count : '?';

        let confirmMsg = `Удалить тарифный профиль "${tariffName}"?`;
        if (userCount > 0) {
            confirmMsg += `\n\n⚠️ На этом тарифе ${userCount} жилец(ов). Они будут автоматически переведены на базовый тариф.`;
        }

        if (!confirm(confirmMsg)) return;

        const originalText = this.dom.btnDelete.innerText;
        this.dom.btnDelete.innerText = 'Удаление...';
        this.dom.btnDelete.disabled = true;

        try {
            await api.delete(`/tariffs/${id}`);

            if (userCount > 0) {
                toast(`Тариф удален. ${userCount} жилец(ов) переведены на базовый тариф.`, 'success');
            } else {
                toast('Тарифный профиль удален', 'success');
            }

            // ВАЖНО: Очищаем кэш тарифов (старый ключ + новый getCached-ключ).
            sessionStorage.removeItem('tariffs_cache');
            api.invalidateCache('/tariffs');

            // Перезагружаем и переключаемся на базовый тариф (id=1)
            this.load(1);
            this.loadScheduledTariffs();
        } catch (error) {
            toast('Ошибка удаления: ' + error.message, 'error');
        } finally {
            this.dom.btnDelete.innerText = originalText;
            this.dom.btnDelete.disabled = false;
        }
    }
};