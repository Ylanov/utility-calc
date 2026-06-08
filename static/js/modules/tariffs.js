// static/js/modules/tariffs.js
import { api } from '../core/api.js';
import { setLoading, toast, escapeHtml, showPrompt, showConfirm } from '../core/dom.js';

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
        // electricity_per_sqm (ОДН) убран из формулы в мае 2026.
        // Поле в БД оставлено nullable; в форме его больше нет.
        // Bug 29.05.2026: per_capita_amount убран из формы (койко-место
        // больше не используется). В БД поле остаётся для совместимости
        // (Pydantic default=0), но в data не отправляется и значение
        // в форме не читается.
        // Нормативы потребления для жильцов без счётчика (User.has_X_meter=False).
        // Расход тогда = norm_per_capita × residents_count.
        // См. миграцию meters_001_per_user_config.
        hw_norm_per_capita: 't_hw_norm',
        cw_norm_per_capita: 't_cw_norm',
        el_norm_per_capita: 't_el_norm',
        // Коэффициент-санкция для «невозвратчиков» (см. tariffs_norm_001_coefficient).
        norm_coefficient: 't_norm_coef',
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
        this.loadSeasonal();
    },

    cacheDOM() {
        this.dom = {
            form: document.getElementById('tariffsForm'),
            selector: document.getElementById('tariffSelector'),
            btnCreate: document.getElementById('btnCreateNewTariff'),
            btnDuplicate: document.getElementById('btnDuplicateTariff'),
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
            // Модалка «Квартиры на тарифе» — углублённое управление привязками.
            btnTariffRooms: document.getElementById('btnTariffRooms'),
            tariffRoomsBtnCount: document.getElementById('tariffRoomsBtnCount'),
            roomsModal: document.getElementById('tariffRoomsModal'),
            roomsName: document.getElementById('tariffRoomsName'),
            roomsSummary: document.getElementById('tariffRoomsSummary'),
            roomsNote: document.getElementById('tariffRoomsNote'),
            roomsTableWrap: document.getElementById('tariffRoomsTableWrap'),
            // Сезонные переключатели (отопление / подогрев ГВС).
            seasonalLoading: document.getElementById('seasonalLoading'),
            seasonalBody: document.getElementById('seasonalBody'),
            seasonalHeating: document.getElementById('seasonalHeating'),
            seasonalHeatingLabel: document.getElementById('seasonalHeatingLabel'),
            seasonalHotWaterHeating: document.getElementById('seasonalHotWaterHeating'),
            seasonalHotWaterHeatingLabel: document.getElementById('seasonalHotWaterHeatingLabel'),
            btnSeasonalSave: document.getElementById('btnSeasonalSave'),
            btnSeasonalReload: document.getElementById('btnSeasonalReload'),
            // Калькулятор Гкал → ₽/единицу.
            gcalModal: document.getElementById('gcalCalcModal'),
            gcalRubPerGcal: document.getElementById('gcalRubPerGcal'),
            gcalNormGcal: document.getElementById('gcalNormGcal'),
            gcalResult: document.getElementById('gcalResult'),
            gcalUnitLabel: document.getElementById('gcalUnitLabel'),
            gcalUnitTarget: document.getElementById('gcalUnitTarget'),
            gcalNormUnit: document.getElementById('gcalNormUnit'),
            gcalResultUnit: document.getElementById('gcalResultUnit'),
            btnOpenGcalCalc: document.getElementById('btnOpenGcalCalc'),
            btnOpenGcalCalcHeat: document.getElementById('btnOpenGcalCalcHeat'),
            btnApplyGcal: document.getElementById('btnApplyGcal'),
        };
    },

    bindEvents() {
        // Bug 29.05.2026 (Коммит 21): переключение 3 вкладок внутри формы
        // тарифа (Ставки / Нормативы / Применение). Делегируем клик на
        // form чтобы не дублировать listener на каждой кнопке.
        if (this.dom.form) {
            this.dom.form.addEventListener('click', (e) => {
                const btn = e.target.closest('button[data-tariff-tab]');
                if (!btn) return;
                e.preventDefault();
                const targetTab = btn.getAttribute('data-tariff-tab');
                // Снимаем active со всех кнопок и контентов
                this.dom.form.querySelectorAll('button[data-tariff-tab]').forEach(b => {
                    b.classList.remove('active');
                    b.style.color = 'var(--text-secondary)';
                });
                this.dom.form.querySelectorAll('[data-tariff-tab-content]').forEach(c => {
                    c.classList.remove('active');
                });
                // Активируем целевые
                btn.classList.add('active');
                btn.style.color = 'var(--text-main)';
                const content = this.dom.form.querySelector(`[data-tariff-tab-content="${targetTab}"]`);
                if (content) content.classList.add('active');
            });
        }
        if (this.dom.form) {
            this.dom.form.addEventListener('submit', (e) => this.handleSubmit(e));
            // Bug AT: пресеты charge-флагов. Делегируем клик по data-charge-preset.
            this.dom.form.addEventListener('click', (e) => {
                const btn = e.target.closest('button[data-charge-preset]');
                if (!btn) return;
                e.preventDefault();
                this._applyChargePreset(btn.getAttribute('data-charge-preset'));
            });
            // Bug AU: любое изменение charge_* / singles_skip_* чекбокса —
            // мгновенный пересчёт превью внизу страницы.
            this.dom.form.addEventListener('change', (e) => {
                const t = e.target;
                if (!t || t.type !== 'checkbox') return;
                if (!t.id || !(t.id.startsWith('t_charge_') || t.id.startsWith('t_singles_skip_'))) return;
                if (typeof this.recalcPreview === 'function') this.recalcPreview();
            });
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
        if (this.dom.btnDuplicate) {
            this.dom.btnDuplicate.addEventListener('click', (e) => {
                e.preventDefault();
                this.handleDuplicate();
            });
        }

        // Ctrl/Cmd + S → отправка формы. Перехватываем дефолт «сохранить страницу».
        // Слушаем на самой форме чтобы не конфликтовать с другими секциями.
        if (this.dom.form) {
            this.dom.form.addEventListener('keydown', (e) => {
                const isSaveCombo = (e.key === 's' || e.key === 'S') && (e.ctrlKey || e.metaKey);
                if (isSaveCombo) {
                    e.preventDefault();
                    if (typeof this.dom.form.requestSubmit === 'function') {
                        this.dom.form.requestSubmit();
                    } else {
                        this.dom.form.dispatchEvent(new Event('submit', { cancelable: true, bubbles: true }));
                    }
                }
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

        // Модалка «Квартиры на тарифе».
        this.dom.btnTariffRooms?.addEventListener('click', () => this.openRoomsModal());
        // Закрытие: крестик / кнопка «Закрыть» / клик по фону.
        this.dom.roomsModal?.addEventListener('click', (e) => {
            if (e.target === this.dom.roomsModal || e.target.closest('[data-tariff-rooms-close]')) {
                this.dom.roomsModal.classList.remove('open');
            }
        });

        // Сезонные переключатели.
        // Клик по slider или по самому label — переключает чекбокс. Это
        // нативное поведение <label>, но slider стилизованный <span>,
        // поэтому делегируем event и вручную toggle'аем.
        document.querySelectorAll('.toggle-slider').forEach(slider => {
            slider.addEventListener('click', (e) => {
                e.preventDefault();
                const targetId = slider.dataset.target;
                const cb = document.getElementById(targetId);
                if (cb) {
                    cb.checked = !cb.checked;
                    this._renderSeasonalToggles();
                }
            });
        });
        this.dom.seasonalHeating?.addEventListener('change', () => this._renderSeasonalToggles());
        this.dom.seasonalHotWaterHeating?.addEventListener('change', () => this._renderSeasonalToggles());
        this.dom.btnSeasonalSave?.addEventListener('click', () => this.saveSeasonal());
        this.dom.btnSeasonalReload?.addEventListener('click', () => this.loadSeasonal());

        // Калькулятор Гкал → ₽/единицу.
        this.dom.btnOpenGcalCalc?.addEventListener('click', () => this.openGcalCalc('water_heating'));
        this.dom.btnOpenGcalCalcHeat?.addEventListener('click', () => this.openGcalCalc('heating'));
        this.dom.btnApplyGcal?.addEventListener('click', () => this.applyGcal());

        document.querySelectorAll('[data-gcal-close]').forEach(btn => {
            btn.addEventListener('click', () => this.closeGcalCalc());
        });
        // Клик по фону модалки тоже закрывает.
        this.dom.gcalModal?.addEventListener('click', (e) => {
            if (e.target === this.dom.gcalModal) this.closeGcalCalc();
        });

        const recalcGcal = () => this._recalcGcal();
        this.dom.gcalRubPerGcal?.addEventListener('input', recalcGcal);
        this.dom.gcalNormGcal?.addEventListener('input', recalcGcal);
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

    _renderSchedulePreview() {
        const box = document.getElementById('schedulePreview');
        if (!box) return;
        const s = parseInt(document.getElementById('setStartDay').value);
        const e = parseInt(document.getElementById('setEndDay').value);
        if (isNaN(s) || isNaN(e)) { box.textContent = ''; return; }
        box.textContent = (s <= e)
            ? `📅 Приём показаний: с ${s} по ${e} число каждого месяца.`
            : `📅 Приём показаний: с ${s} числа по ${e} число СЛЕДУЮЩЕГО месяца (окно переходит через границу месяца).`;
    },

    async loadSchedule() {
        try {
            const data = await api.getCached('/settings/submission-period', { ttlSeconds: 600 });
            document.getElementById('setStartDay').value = data.start_day;
            document.getElementById('setEndDay').value = data.end_day;
            this._renderSchedulePreview();
            // Живой предпросмотр окна при изменении полей (вешаем один раз).
            ['setStartDay', 'setEndDay'].forEach(id => {
                const inp = document.getElementById(id);
                if (inp && !inp._previewBound) {
                    inp._previewBound = true;
                    inp.addEventListener('input', () => this._renderSchedulePreview());
                }
            });
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

        const now = new Date();
        this.tariffsList.forEach(t => {
            const opt = document.createElement('option');
            opt.value = t.id;

            // Префикс по типу: singles vs family.
            const isSingles = (t.tariff_type === 'singles');
            // Префикс по времени: запланированный (⏳) vs активный.
            const isScheduled = !!(t.effective_from && new Date(t.effective_from) > now);
            const typeIcon = isSingles ? '👤' : '🏠';
            const statusIcon = isScheduled ? ' ⏳' : '';
            const prefix = `${typeIcon}${statusIcon} `;

            // Показываем количество жильцов на тарифе.
            let countSuffix = '';
            if (t.user_count !== undefined && t.user_count > 0) {
                countSuffix = ` (${t.user_count} чел.)`;
            } else if (t.user_count !== undefined) {
                countSuffix = ' (нет жильцов)';
            }
            opt.textContent = `${prefix}${t.name}${countSuffix}`;

            // Подкраска option (Firefox-only — в Chrome визуально игнорится,
            // но эмодзи всё равно отличает).
            if (isScheduled) opt.style.color = '#b45309';
            else if (isSingles) opt.style.color = '#7c3aed';

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

        // Проходим по нашей карте и заполняем инпуты с ценами.
        // Нормативы потребления (Numeric(10,3)) — 3 знака; цены/ставки — 2 знака.
        // norm_coefficient — 2 знака (это не объём, а множитель типа 3.00).
        const NORM_KEYS = new Set(['hw_norm_per_capita', 'cw_norm_per_capita', 'el_norm_per_capita']);
        for (const [dbKey, htmlId] of Object.entries(this.MAPPING)) {
            const input = document.getElementById(htmlId);
            if (input && tariff[dbKey] !== undefined) {
                const decimals = NORM_KEYS.has(dbKey) ? 3 : 2;
                input.value = Number(tariff[dbKey]).toFixed(decimals);
            }
        }
        // norm_coefficient — если в тарифе ещё не задан (старый тариф до миграции),
        // ставим дефолт 3.00.
        const coefInp = document.getElementById('t_norm_coef');
        if (coefInp) {
            coefInp.value = (tariff.norm_coefficient !== undefined && tariff.norm_coefficient !== null)
                ? Number(tariff.norm_coefficient).toFixed(2)
                : '3.00';
        }

        // Тип тарифа: family/singles (см. tariffs_type_001_family_singles).
        // Старые тарифы без поля → 'family' по default.
        // Bug 29.05.2026 (Коммит 14, revert Коммита 13): нормативы нужны
        // ОБОИМ типам — у холостяков тоже могут не подать счётчики, тогда
        // AUTO_NORM прибавляет норматив к показаниям как и у семейных.
        // Скрытие секции нормативов для singles убрано.
        const ttype = tariff.tariff_type || 'family';
        const radioFam = document.getElementById('t_type_family');
        const radioSng = document.getElementById('t_type_singles');
        if (radioFam) radioFam.checked = (ttype === 'family');
        if (radioSng) radioSng.checked = (ttype === 'singles');

        // Сезонность per-tariff (heating + hw_heating). См. миграцию
        // tariffs_seasonal_002_per_tariff. heating_active=true + даты=null →
        // круглогодично. Даты приходят как "YYYY-MM-DD" из сервера.
        const setCb = (id, v) => {
            const el = document.getElementById(id);
            if (el) el.checked = (v === undefined || v === null) ? true : !!v;
        };
        const setDate = (id, v) => {
            const el = document.getElementById(id);
            if (el) el.value = v ? String(v).substring(0, 10) : '';
        };
        setCb('t_heating_active', tariff.heating_active);
        setDate('t_heating_start', tariff.heating_season_start);
        setDate('t_heating_end', tariff.heating_season_end);
        setCb('t_hw_heating_active', tariff.hw_heating_active);
        setDate('t_hw_heating_start', tariff.hw_heating_season_start);
        setDate('t_hw_heating_end', tariff.hw_heating_season_end);

        // Bug AS этап 3: skip-флаги для холостяцких квартир. Все default
        // false — для существующих тарифов чекбоксы пустые, поведение
        // не меняется (см. calculate_utilities этапа 4).
        const setCbStrict = (id, v) => {
            const el = document.getElementById(id);
            if (el) el.checked = !!v;
        };
        setCbStrict('t_singles_skip_maintenance', tariff.singles_skip_maintenance);
        setCbStrict('t_singles_skip_social_rent', tariff.singles_skip_social_rent);
        setCbStrict('t_singles_skip_heating', tariff.singles_skip_heating);
        setCbStrict('t_singles_skip_waste', tariff.singles_skip_waste);

        // Bug AT этап 2: charge_* — default true для существующих тарифов
        // (null/undefined → true), снимаем только если backend явно вернул false.
        const setCbDefaultTrue = (id, v) => {
            const el = document.getElementById(id);
            if (el) el.checked = (v === undefined || v === null) ? true : !!v;
        };
        setCbDefaultTrue('t_charge_hot_water', tariff.charge_hot_water);
        setCbDefaultTrue('t_charge_cold_water', tariff.charge_cold_water);
        setCbDefaultTrue('t_charge_sewage', tariff.charge_sewage);
        setCbDefaultTrue('t_charge_electricity', tariff.charge_electricity);
        setCbDefaultTrue('t_charge_maintenance', tariff.charge_maintenance);
        setCbDefaultTrue('t_charge_social_rent', tariff.charge_social_rent);
        setCbDefaultTrue('t_charge_heating', tariff.charge_heating);
        setCbDefaultTrue('t_charge_waste', tariff.charge_waste);

        // Базовый тариф (id=1) удалять нельзя, прячем кнопку
        if (this.dom.btnDelete) {
            this.dom.btnDelete.style.display = (tariff.id === 1) ? 'none' : 'block';
        }

        // Загружаем «Где применяется» + список общежитий + перерисовываем превью.
        this.loadUsage(tariff.id);
        this.loadDormitoriesForAssign();
        this.recalcPreview();
    },

    /** Bug AT: применяет пресет charge-флагов к 8 чекбоксам.
     *  Пресеты:
     *    'all'         — все галочки (Лидер, всё начисляется)
     *    'rent_only'   — только charge_social_rent (тариф «только наём»)
     *    'no_meters'   — все 4 meter-флага сняты, остальные стоят
     *  После применения пересчитывает превью. */
    _applyChargePreset(preset) {
        const all = {
            charge_hot_water: false, charge_cold_water: false,
            charge_sewage: false, charge_electricity: false,
            charge_maintenance: false, charge_social_rent: false,
            charge_heating: false, charge_waste: false,
        };
        let state;
        if (preset === 'all') {
            state = Object.fromEntries(Object.keys(all).map(k => [k, true]));
        } else if (preset === 'rent_only') {
            state = { ...all, charge_social_rent: true };
        } else if (preset === 'no_meters') {
            state = Object.fromEntries(Object.keys(all).map(k => [k, true]));
            state.charge_hot_water = false;
            state.charge_cold_water = false;
            state.charge_sewage = false;
            state.charge_electricity = false;
        } else {
            return;
        }
        Object.entries(state).forEach(([k, v]) => {
            const el = document.getElementById('t_' + k);
            if (el) el.checked = v;
        });
        // recalcPreview если он есть — пересчитать виджет внизу.
        if (typeof this.recalcPreview === 'function') this.recalcPreview();
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
        // norm_coefficient — дефолт 3.00 для нового тарифа.
        const coefInp = document.getElementById('t_norm_coef');
        if (coefInp) coefInp.value = '3.00';

        // Тип тарифа — default 'family' для нового.
        const radioFam = document.getElementById('t_type_family');
        const radioSng = document.getElementById('t_type_singles');
        if (radioFam) radioFam.checked = true;
        if (radioSng) radioSng.checked = false;

        // Сезонность по умолчанию — круглогодично активна.
        ['t_heating_active', 't_hw_heating_active'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.checked = true;
        });
        ['t_heating_start', 't_heating_end', 't_hw_heating_start', 't_hw_heating_end'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.value = '';
        });
        // Bug AS: skip-флаги default false — холостяки платят за всё, как все.
        ['t_singles_skip_maintenance', 't_singles_skip_social_rent',
         't_singles_skip_heating', 't_singles_skip_waste'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.checked = false;
        });
        // Bug AT: charge_* default true — новый тариф начисляет всё.
        ['t_charge_hot_water', 't_charge_cold_water', 't_charge_sewage',
         't_charge_electricity', 't_charge_maintenance', 't_charge_social_rent',
         't_charge_heating', 't_charge_waste'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.checked = true;
        });

        if (this.dom.btnDelete) this.dom.btnDelete.style.display = 'none';
        // Скрываем «Где применяется» — у нового тарифа ещё некого
        if (this.dom.usageBlock) this.dom.usageBlock.style.display = 'none';
        // Сбрасываем кеш usage — у нового тарифа привязок ещё нет.
        this._usageTariffId = null;
        this._usageData = null;
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
        this._usageTariffId = tariffId;
        this._usageData = null;
        this.dom.usageBlock.style.display = 'block';
        this.dom.usageStats.innerHTML = '<span style="color:var(--text-secondary);">Загрузка…</span>';
        this.dom.usageDetails.innerHTML = '';
        try {
            const data = await api.get(`/tariffs/${tariffId}/usage`);
            // Кешируем для модалки «Квартиры» — не дёргаем сервер повторно.
            this._usageData = data;
            this._renderUsage(data);
        } catch (e) {
            this.dom.usageStats.innerHTML = `<span style="color:var(--danger-color);">Ошибка: ${escapeHtml(e.message)}</span>`;
        }
    },

    _renderUsage(d) {
        // Счётчик комнат на кнопке «Квартиры (N)».
        if (this.dom.tariffRoomsBtnCount) {
            const n = d.by_room?.rooms_count ?? 0;
            this.dom.tariffRoomsBtnCount.textContent = `(${n})`;
        }
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

    // ====================================================================
    // МОДАЛКА «КВАРТИРЫ НА ТАРИФЕ» — углублённое управление привязками
    // ====================================================================
    /** Открывает модалку со списком комнат текущего тарифа. Данные берём из
     *  кеша loadUsage (this._usageData); если их нет — догружаем сами. */
    async openRoomsModal() {
        if (!this.dom.roomsModal) return;
        const tariffId = this._usageTariffId || parseInt(this.dom.inputId?.value);
        if (!tariffId) return toast('Сначала выберите тариф', 'warning');

        // Имя тарифа в шапку.
        const tariff = this.tariffsList.find(t => t.id === tariffId);
        if (this.dom.roomsName) this.dom.roomsName.textContent = tariff ? tariff.name : `#${tariffId}`;

        this.dom.roomsModal.classList.add('open');

        let data = this._usageData;
        if (!data) {
            this.dom.roomsTableWrap.innerHTML = '<div style="padding:20px; text-align:center; color:var(--text-secondary);"><i class="fa-solid fa-spinner fa-spin"></i> Загрузка…</div>';
            this.dom.roomsSummary.innerHTML = '';
            this.dom.roomsNote.innerHTML = '';
            try {
                data = await api.get(`/tariffs/${tariffId}/usage`);
                this._usageData = data;
            } catch (e) {
                this.dom.roomsTableWrap.innerHTML = `<div style="padding:16px; color:var(--danger-color);">Ошибка загрузки: ${escapeHtml(e.message)}</div>`;
                return;
            }
        }
        this._renderRoomsModal(data);
    },

    _renderRoomsModal(d) {
        const byRoom = d.by_room || {};
        const rooms = byRoom.rooms || [];

        // Сводка: комнат / жильцов + чипы по общежитиям.
        const dormChips = (byRoom.by_dormitory || []).map(g => `
            <span style="display:inline-block; margin:2px; padding:3px 8px; background:#dbeafe; color:#1e40af; border-radius:10px; font-size:11px; font-weight:600;">
                <i class="fa-solid fa-building"></i> ${escapeHtml(g.dormitory)} — ${g.rooms_count} комн.
            </span>`).join('');
        this.dom.roomsSummary.innerHTML = `
            <div style="display:flex; gap:18px; flex-wrap:wrap; margin-bottom:8px;">
                <div>
                    <div style="font-size:11px; color:var(--text-secondary); text-transform:uppercase;">Комнат</div>
                    <div style="font-size:20px; font-weight:700; color:#3b82f6;" id="tariffRoomsCount">${byRoom.rooms_count ?? 0}</div>
                </div>
                <div>
                    <div style="font-size:11px; color:var(--text-secondary); text-transform:uppercase;">Жильцов</div>
                    <div style="font-size:20px; font-weight:700; color:#10b981;">${byRoom.users_in_rooms ?? 0}</div>
                </div>
            </div>
            ${dormChips ? `<div>${dormChips}</div>` : ''}`;

        // Примечание о персональных привязках (устар.).
        const directCount = d.by_user_direct?.count ?? 0;
        this.dom.roomsNote.innerHTML = directCount > 0
            ? `<div style="font-size:12px; color:#92400e; background:#fef3c7; border:1px solid #fde68a; border-radius:6px; padding:8px 12px;">
                   <i class="fa-solid fa-triangle-exclamation"></i>
                   У ${directCount} жильцов тариф назначен напрямую (устар.).
               </div>`
            : '';

        this._renderRoomsTable(rooms);
    },

    /** Рисует таблицу комнат с кнопкой «Убрать» по каждой строке. */
    _renderRoomsTable(rooms) {
        if (!rooms.length) {
            this.dom.roomsTableWrap.innerHTML = `
                <div style="padding:20px; text-align:center; color:var(--text-secondary); font-style:italic;">
                    К этому тарифу не привязано ни одной комнаты.
                </div>`;
            return;
        }
        const rows = rooms.map(r => `
            <tr data-room-id="${r.id}">
                <td style="padding:8px 10px; border-bottom:1px solid var(--border-color);">${escapeHtml(r.dormitory || '—')}</td>
                <td style="padding:8px 10px; border-bottom:1px solid var(--border-color); font-weight:600;">${escapeHtml(r.number || '—')}</td>
                <td style="padding:8px 10px; border-bottom:1px solid var(--border-color); text-align:right;">
                    <button type="button" class="action-btn secondary-btn" data-remove-room="${r.id}" style="padding:5px 12px; font-size:12px; border-radius:6px;" title="Убрать комнату с этого тарифа — она вернётся на дефолтный">
                        <i class="fa-solid fa-link-slash"></i> Убрать
                    </button>
                </td>
            </tr>`).join('');
        this.dom.roomsTableWrap.innerHTML = `
            <table style="width:100%; border-collapse:collapse; font-size:13px;">
                <thead>
                    <tr style="text-align:left; color:var(--text-secondary); font-size:11px; text-transform:uppercase;">
                        <th style="padding:6px 10px; border-bottom:2px solid var(--border-color);">Общежитие</th>
                        <th style="padding:6px 10px; border-bottom:2px solid var(--border-color);">Номер</th>
                        <th style="padding:6px 10px; border-bottom:2px solid var(--border-color); text-align:right;">Действие</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>`;

        // Делегируем клик по «Убрать» на контейнер (вешаем один раз).
        if (!this._roomsTableBound) {
            this.dom.roomsTableWrap.addEventListener('click', (e) => {
                const btn = e.target.closest('[data-remove-room]');
                if (btn) this.removeRoomFromTariff(parseInt(btn.dataset.removeRoom), btn);
            });
            this._roomsTableBound = true;
        }
    },

    /** Убирает комнату с тарифа: PUT /rooms/{id} {tariff_id:null} → комната
     *  падает на дефолтный тариф. После успеха убираем строку, обновляем
     *  счётчики и перезагружаем список тарифов (this.load). */
    async removeRoomFromTariff(roomId, btn) {
        if (!roomId) return;
        if (!await showConfirm(
            'Убрать эту комнату с тарифа?\nОна вернётся на дефолтный тариф.',
            { confirmText: 'Убрать' }
        )) return;

        const original = btn ? btn.innerHTML : '';
        if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>'; }

        try {
            await api.put(`/rooms/${roomId}`, { tariff_id: null });
            api.invalidateCache('/tariffs');

            // Обновляем локальный кеш usage: убираем комнату + правим счётчики.
            if (this._usageData?.by_room) {
                const br = this._usageData.by_room;
                const removed = (br.rooms || []).find(r => r.id === roomId);
                br.rooms = (br.rooms || []).filter(r => r.id !== roomId);
                br.rooms_count = Math.max(0, (br.rooms_count ?? br.rooms.length) - 1);
                // Уменьшаем счётчик по общежитию убранной комнаты.
                if (removed && Array.isArray(br.by_dormitory)) {
                    const g = br.by_dormitory.find(x => x.dormitory === removed.dormitory);
                    if (g) {
                        g.rooms_count = Math.max(0, g.rooms_count - 1);
                        if (g.rooms_count === 0) {
                            br.by_dormitory = br.by_dormitory.filter(x => x !== g);
                        }
                    }
                }
            }

            // Убираем строку из таблицы (или перерисовываем пустое состояние).
            const tr = this.dom.roomsTableWrap.querySelector(`tr[data-room-id="${roomId}"]`);
            if (tr) tr.remove();
            const remaining = this._usageData?.by_room?.rooms || [];
            if (!remaining.length) this._renderRoomsTable(remaining);

            // Обновляем счётчик в модалке и кнопке.
            const cntEl = document.getElementById('tariffRoomsCount');
            const newCount = this._usageData?.by_room?.rooms_count ?? remaining.length;
            if (cntEl) cntEl.textContent = String(newCount);
            if (this.dom.tariffRoomsBtnCount) this.dom.tariffRoomsBtnCount.textContent = `(${newCount})`;

            toast('Комната убрана с тарифа', 'success');

            // Перезагружаем список тарифов (счётчики жильцов в селекторе) +
            // блок «Где применяется» под формой. Сохраняем текущий выбор.
            await this.load(this._usageTariffId);
        } catch (e) {
            toast('Ошибка: ' + e.message, 'error');
            if (btn) { btn.disabled = false; btn.innerHTML = original; }
        }
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
        if (!await showConfirm('Вы уверены — ' + action, { confirmText: 'Продолжить' })) return;

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

        // Сезонность per-tariff (см. миграцию tariffs_seasonal_002_per_tariff).
        // Пустая дата → null (круглогодично). Чекбоксы → bool.
        const cb = (id) => {
            const el = document.getElementById(id);
            return el ? !!el.checked : true;
        };
        const dt = (id) => {
            const el = document.getElementById(id);
            return (el && el.value) ? el.value : null;  // "YYYY-MM-DD" or null
        };
        data.heating_active = cb('t_heating_active');
        data.heating_season_start = dt('t_heating_start');
        data.heating_season_end = dt('t_heating_end');
        data.hw_heating_active = cb('t_hw_heating_active');
        data.hw_heating_season_start = dt('t_hw_heating_start');
        data.hw_heating_season_end = dt('t_hw_heating_end');

        // Bug AS этап 3: skip-флаги для холостяцких квартир.
        const cbStrict = (id) => !!document.getElementById(id)?.checked;
        data.singles_skip_maintenance = cbStrict('t_singles_skip_maintenance');
        data.singles_skip_social_rent = cbStrict('t_singles_skip_social_rent');
        data.singles_skip_heating = cbStrict('t_singles_skip_heating');
        data.singles_skip_waste = cbStrict('t_singles_skip_waste');

        // Bug AT этап 2: charge_* — что начисляет тариф.
        data.charge_hot_water = cbStrict('t_charge_hot_water');
        data.charge_cold_water = cbStrict('t_charge_cold_water');
        data.charge_sewage = cbStrict('t_charge_sewage');
        data.charge_electricity = cbStrict('t_charge_electricity');
        data.charge_maintenance = cbStrict('t_charge_maintenance');
        data.charge_social_rent = cbStrict('t_charge_social_rent');
        data.charge_heating = cbStrict('t_charge_heating');
        data.charge_waste = cbStrict('t_charge_waste');

        // Тип тарифа (radio: family / singles).
        const ttypeRadio = document.querySelector('input[name="t_type"]:checked');
        data.tariff_type = ttypeRadio ? ttypeRadio.value : 'family';

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

        if (!await showConfirm(confirmMsg, { danger: true, confirmText: 'Удалить' })) return;

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
    },

    // Дублирует выбранный тариф: дёргает /tariffs POST с полным набором
    // полей исходного тарифа (без id и effective_from), новое имя
    // запрашивает у админа. Удобно для соседнего общежития с похожими
    // ставками — копируешь и правишь 2-3 поля.
    async handleDuplicate() {
        const srcId = parseInt(this.dom.inputId?.value);
        if (!srcId || isNaN(srcId)) {
            toast('Сначала выберите тариф для копирования', 'warning');
            return;
        }
        const src = this.tariffsList.find(t => t.id === srcId);
        if (!src) {
            toast('Не нашёл исходный тариф', 'error');
            return;
        }

        const defaultName = `${src.name} (копия)`;
        const newName = await showPrompt(
            'Дублирование тарифа',
            `Создаём копию тарифа «${src.name}». Введите имя нового профиля:`,
            defaultName,
            'Например: ЦСООР Лидер — 2-й корпус'
        );
        if (newName === null) return;
        const trimmed = newName.trim();
        if (!trimmed) {
            toast('Имя не может быть пустым', 'error');
            return;
        }

        // Клон. id убираем чтобы это был POST-создание. effective_from
        // обнуляем — копия становится активной немедленно.
        // user_count / created_at / updated_at пришли из /with-stats —
        // сервер их игнорирует, но удалим для чистоты.
        const payload = { ...src };
        delete payload.id;
        delete payload.created_at;
        delete payload.updated_at;
        delete payload.user_count;
        payload.effective_from = null;
        payload.name = trimmed;

        const btn = this.dom.btnDuplicate;
        const originalHTML = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Копирование...';

        try {
            const saved = await api.post('/tariffs', payload);
            api.invalidateCache('/tariffs');
            sessionStorage.removeItem('tariffs_cache');
            toast(`Тариф «${saved.name}» создан как копия`, 'success');
            await this.load(saved.id);
            await this.loadScheduledTariffs();
        } catch (e) {
            toast('Ошибка копирования: ' + e.message, 'error');
        } finally {
            btn.disabled = false;
            btn.innerHTML = originalHTML;
        }
    },

    // ====================================================================
    // СЕЗОННЫЕ ПЕРЕКЛЮЧАТЕЛИ — отопление и подогрев ГВС
    // ====================================================================
    /** Загружает текущее состояние сезонных флагов из /settings/seasonal. */
    async loadSeasonal() {
        if (!this.dom.seasonalBody) return;
        try {
            const data = await api.get('/settings/seasonal');
            if (this.dom.seasonalHeating) {
                this.dom.seasonalHeating.checked = !!data.heating_season_active;
            }
            if (this.dom.seasonalHotWaterHeating) {
                this.dom.seasonalHotWaterHeating.checked = !!data.hot_water_heating_active;
            }
            this._renderSeasonalToggles();
            if (this.dom.seasonalLoading) this.dom.seasonalLoading.style.display = 'none';
            this.dom.seasonalBody.style.display = '';
        } catch (e) {
            console.warn('Не удалось загрузить сезонные настройки:', e.message);
            if (this.dom.seasonalLoading) {
                this.dom.seasonalLoading.innerHTML =
                    `<span style="color:var(--danger-color);">
                        <i class="fa-solid fa-triangle-exclamation"></i> Ошибка загрузки: ${escapeHtml(e.message)}
                    </span>`;
            }
        }
    },

    /** Перерисовывает визуал toggle-slider по текущему checked-состоянию.
     *  CSS-стейт зашит инлайн в HTML, поэтому проще тут JS обновить. */
    _renderSeasonalToggles() {
        const apply = (cb, label, slider) => {
            if (!cb || !label) return;
            const on = cb.checked;
            label.textContent = on ? 'Включено' : 'Выключено';
            label.style.color = on ? '#059669' : '#dc2626';
            if (slider) {
                slider.style.background = on ? '#10b981' : '#cbd5e1';
                const knob = slider.querySelector('span');
                if (knob) {
                    knob.style.transform = on ? 'translateX(24px)' : 'translateX(0)';
                }
            }
        };
        apply(
            this.dom.seasonalHeating,
            this.dom.seasonalHeatingLabel,
            document.querySelector('.toggle-slider[data-target="seasonalHeating"]'),
        );
        apply(
            this.dom.seasonalHotWaterHeating,
            this.dom.seasonalHotWaterHeatingLabel,
            document.querySelector('.toggle-slider[data-target="seasonalHotWaterHeating"]'),
        );
    },

    /** Сохраняет состояние сезонных флагов через PUT /settings/seasonal. */
    async saveSeasonal() {
        const btn = this.dom.btnSeasonalSave;
        if (!btn) return;
        const payload = {
            heating_season_active: !!this.dom.seasonalHeating?.checked,
            hot_water_heating_active: !!this.dom.seasonalHotWaterHeating?.checked,
        };
        setLoading(btn, true, 'Сохранение…');
        try {
            await api.put('/settings/seasonal', payload);
            toast('Сезонные настройки сохранены', 'success');
            await this.loadSeasonal();
        } catch (e) {
            toast('Ошибка сохранения: ' + e.message, 'error');
        } finally {
            setLoading(btn, false, '<i class="fa-solid fa-floppy-disk"></i> Применить');
        }
    },

    // ====================================================================
    // КАЛЬКУЛЯТОР ГКАЛ → ₽/м² (или ₽/м³)
    // ====================================================================
    /**
     * Открывает модалку калькулятора. target определяет какое поле тарифа
     * получит результат и какие единицы использовать в подписях:
     *   - 'heating'       → ₽/м² (отопление, площадь)
     *   - 'water_heating' → ₽/м³ (подогрев ГВС, объём)
     */
    openGcalCalc(target) {
        if (!this.dom.gcalModal) return;
        this._gcalTarget = target;
        const isHeat = target === 'heating';
        const unit = isHeat ? 'м²' : 'м³';
        if (this.dom.gcalUnitLabel) this.dom.gcalUnitLabel.textContent = '₽/' + unit;
        if (this.dom.gcalUnitTarget) this.dom.gcalUnitTarget.textContent = unit;
        if (this.dom.gcalNormUnit) this.dom.gcalNormUnit.textContent = unit;
        if (this.dom.gcalResultUnit) this.dom.gcalResultUnit.textContent = unit;
        // ВСЕГДА чистим оба поля при открытии. Раньше калькулятор «помнил»
        // значения между тарифами — открыл для тарифа A, ввёл 2150 / 0.0185,
        // закрыл; открыл для тарифа B — там же 2150 / 0.0185 (хотя у B
        // другой поставщик). Пользователь решал, что калькулятор «общий».
        // Чистим оба input'а и результат.
        if (this.dom.gcalRubPerGcal) this.dom.gcalRubPerGcal.value = '';
        if (this.dom.gcalNormGcal) this.dom.gcalNormGcal.value = '';
        if (this.dom.gcalResult) this.dom.gcalResult.textContent = '— ₽';
        // placeholder с типовым нормативом по региону подсказывает админу,
        // но НЕ подставляется автоматически (см. выше).
        if (this.dom.gcalNormGcal) {
            this.dom.gcalNormGcal.placeholder =
                isHeat ? 'Например: 0.0185' : 'Например: 0.0628';
        }
        this.dom.gcalModal.classList.add('open');
        // Фокус на первое поле — удобно для админа: открыл → сразу пишет.
        setTimeout(() => this.dom.gcalRubPerGcal?.focus(), 50);
    },

    closeGcalCalc() {
        if (this.dom.gcalModal) this.dom.gcalModal.classList.remove('open');
    },

    /** Пересчитывает результат при изменении любого из двух полей. */
    _recalcGcal() {
        if (!this.dom.gcalResult) return;
        const rub = parseFloat(this.dom.gcalRubPerGcal?.value) || 0;
        const norm = parseFloat(this.dom.gcalNormGcal?.value) || 0;
        const result = rub * norm;
        this.dom.gcalResult.textContent =
            result > 0
                ? result.toLocaleString('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 4 }) + ' ₽'
                : '— ₽';
    },

    /** Применяет результат в нужное поле тарифа и закрывает модалку. */
    applyGcal() {
        const rub = parseFloat(this.dom.gcalRubPerGcal?.value) || 0;
        const norm = parseFloat(this.dom.gcalNormGcal?.value) || 0;
        if (rub <= 0 || norm <= 0) {
            toast('Заполните оба поля — тариф ₽/Гкал и норматив Гкал/единицу', 'warning');
            return;
        }
        const result = rub * norm;
        const targetId = this._gcalTarget === 'heating' ? 't_heat' : 't_w_heat';
        const targetInput = document.getElementById(targetId);
        if (targetInput) {
            // 4 знака после точки — иначе при норматив 0.0185 округление до
            // 2 знаков может «съесть» половину копейки на квитанции.
            targetInput.value = result.toFixed(4);
            // Триггерим input — пересчитает превью тарифа.
            targetInput.dispatchEvent(new Event('input', { bubbles: true }));
            toast(`Применено: ${result.toFixed(2)} ₽`, 'success');
        }
        this.closeGcalCalc();
    },
};