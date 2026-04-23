// static/js/modules/client-certificates.js
//
// Вкладка «Справки» клиентского портала. Персональные данные (паспорт,
// ФИО, регистрация) и семья теперь живут ВНУТРИ модалки заказа справки
// как сворачиваемые секции — жилец заполняет их один раз при первом
// заказе, после чего просто вводит «период + куда» и получает PDF.
//
// Секции в модалке заказа:
//   1. Предупреждение «нужно заполнить» / баннер «всё готово»
//   2. <details> «Мои данные» — паспорт, должность, регистрация
//   3. <details> «Состав семьи» — необязательно
//   4. Форма заказа — период + куда, кнопка активна только если профиль ОК

import { api } from '../core/api.js';
import { toast } from '../core/dom.js';

function esc(s) {
    if (s == null) return '';
    const d = document.createElement('div');
    d.textContent = String(s);
    return d.innerHTML;
}

function fmtDate(iso) {
    if (!iso) return '—';
    try { return new Date(iso).toLocaleDateString('ru-RU'); } catch { return iso; }
}

const ROLE_LABEL = {
    spouse: 'Супруг(а)',
    child: 'Ребёнок',
    parent: 'Родитель',
    other: 'Член семьи',
};

const STATUS_META = {
    pending:   { color: '#f59e0b', bg: '#fef3c7', label: 'В обработке' },
    generated: { color: '#10b981', bg: '#d1fae5', label: 'Готова' },
    delivered: { color: '#3b82f6', bg: '#dbeafe', label: 'Выдана' },
    rejected:  { color: '#ef4444', bg: '#fee2e2', label: 'Отклонена' },
};

export const ClientCertificates = {
    isInitialized: false,
    profile: null,
    family: [],
    contracts: [],
    certs: [],
    certsLoaded: false,

    init() {
        if (this.isInitialized) return;
        this.cacheDOM();
        if (!this.dom.certTab) {
            // Старая версия HTML без вкладки «Справки» — молчим.
            return;
        }
        this.bindEvents();
        this.isInitialized = true;

        // Профиль грузим ЛЕНИВО — только при открытии модалки заказа
        // или при первом клике на вкладку «Справки». Раньше он тянулся
        // сразу на старте и давал 500 если таблица users ещё без новых
        // колонок.
        if (document.getElementById('tab-certificates')?.classList.contains('active')) {
            this.loadCerts();
        }
    },

    cacheDOM() {
        this.dom = {
            certTab: document.getElementById('tab-certificates'),
            certTabBtn: document.querySelector('.tab-btn[data-tab="tab-certificates"]'),
            catalog: document.getElementById('certCatalog'),
            certsList: document.getElementById('certsList'),
            btnRefreshCerts: document.getElementById('btnRefreshCerts'),

            // Модалка ФЛС
            flcModal: document.getElementById('certFlcModal'),
            flcPurpose: document.getElementById('certPurpose'),
            flcFrom: document.getElementById('certPeriodFrom'),
            flcTo: document.getElementById('certPeriodTo'),
            flcContractBlock: document.getElementById('certContractBlock'),
            flcContractInfo: document.getElementById('certContractInfo'),
            flcWarn: document.getElementById('certProfileWarn'),
            flcOk: document.getElementById('certProfileOk'),
            flcSubmit: document.getElementById('btnSubmitCertOrder'),

            // Секция «Мои данные» — inline-форма профиля
            profileSection: document.getElementById('certProfileSection'),
            profileHint: document.getElementById('certProfileHint'),
            profileForm: document.getElementById('certProfileForm'),
            profFullName: document.getElementById('profFullName'),
            profPosition: document.getElementById('profPosition'),
            profPassSeries: document.getElementById('profPassSeries'),
            profPassNumber: document.getElementById('profPassNumber'),
            profPassIssuedBy: document.getElementById('profPassIssuedBy'),
            profPassIssuedAt: document.getElementById('profPassIssuedAt'),
            profRegDate: document.getElementById('profRegDate'),
            profRegAddress: document.getElementById('profRegAddress'),
            profLivesAlone: document.getElementById('profLivesAlone'),

            // Секция «Семья»
            familySection: document.getElementById('certFamilySection'),
            familyBadge: document.getElementById('certFamilyBadge'),
            familyHintText: document.getElementById('familyHintText'),
            familyList: document.getElementById('familyList'),
            familyCount: document.getElementById('certFamilyCount'),
            btnAddFamily: document.getElementById('btnAddFamilyMember'),
            familyModal: document.getElementById('familyMemberModal'),
            familyForm: document.getElementById('familyMemberForm'),
            familyModalTitle: document.getElementById('familyModalTitle'),
            familyId: document.getElementById('familyMemberId'),
            familyRole: document.getElementById('familyRole'),
            familyFullName: document.getElementById('familyFullName'),
            familyBirthDate: document.getElementById('familyBirthDate'),
            familyArrivalDate: document.getElementById('familyArrivalDate'),
            familyRegType: document.getElementById('familyRegType'),
            familyRelationHead: document.getElementById('familyRelationHead'),
            familyPassSeries: document.getElementById('familyPassSeries'),
            familyPassNumber: document.getElementById('familyPassNumber'),
            familyRegDate: document.getElementById('familyRegDate'),
        };
    },

    bindEvents() {
        // Ленивая загрузка списка при переключении на вкладку
        this.dom.certTabBtn?.addEventListener('click', () => {
            if (!this.certsLoaded) this.loadCerts();
        });

        // Открытие заказа из каталога
        this.dom.catalog?.addEventListener('click', (e) => {
            const card = e.target.closest('.cert-card[data-cert-type]');
            if (!card) return;
            if (card.dataset.certType === 'flc') this.openFlcModal();
        });

        // Закрытие модалки ФЛС
        this.dom.flcModal?.addEventListener('click', (e) => {
            if (e.target.closest('[data-cert-close]') || e.target === this.dom.flcModal) {
                this.dom.flcModal.classList.remove('open');
            }
        });

        // Кнопки в модалке
        this.dom.flcSubmit?.addEventListener('click', () => this.submitFlcOrder());
        this.dom.profileForm?.addEventListener('submit', (e) => this.saveProfileInline(e));
        // «Проживаю один» — автосохранение флага и обновление баннеров.
        this.dom.profLivesAlone?.addEventListener('change', () => this.toggleLivesAlone());
        this.dom.btnRefreshCerts?.addEventListener('click', () => this.loadCerts());
        this.dom.certsList?.addEventListener('click', (e) => {
            const btn = e.target.closest('[data-cert-download]');
            if (btn) this.downloadCert(Number(btn.dataset.certDownload));
        });

        // Семья
        this.dom.btnAddFamily?.addEventListener('click', () => this.openFamilyModal(null));
        this.dom.familyModal?.addEventListener('click', (e) => {
            if (e.target.closest('[data-family-close]') || e.target === this.dom.familyModal) {
                this.dom.familyModal.classList.remove('open');
            }
        });
        this.dom.familyForm?.addEventListener('submit', (e) => this.submitFamily(e));
        this.dom.familyList?.addEventListener('click', (e) => {
            const edit = e.target.closest('[data-family-edit]');
            const del = e.target.closest('[data-family-delete]');
            if (edit) {
                const m = this.family.find(f => f.id === Number(edit.dataset.familyEdit));
                if (m) this.openFamilyModal(m);
            }
            if (del) this.deleteFamily(Number(del.dataset.familyDelete));
        });
    },

    // =====================================================
    // PROFILE (ленивая загрузка)
    // =====================================================
    async loadProfile() {
        try {
            this.profile = await api.get('/me/profile');
        } catch (e) {
            // Нет профиля / 500 — не блокируем вкладку, просто покажем форму.
            this.profile = null;
            console.warn('Profile load failed:', e.message);
        }
    },

    populateProfileForm() {
        const p = this.profile || {};
        const d = this.dom;
        if (d.profFullName)    d.profFullName.value    = p.full_name || '';
        if (d.profPosition)    d.profPosition.value    = p.position || '';
        if (d.profPassSeries)  d.profPassSeries.value  = p.passport_series || '';
        if (d.profPassNumber)  d.profPassNumber.value  = p.passport_number || '';
        if (d.profPassIssuedBy)d.profPassIssuedBy.value= p.passport_issued_by || '';
        if (d.profPassIssuedAt)d.profPassIssuedAt.value= p.passport_issued_at || '';
        if (d.profRegDate)     d.profRegDate.value     = p.registration_date || '';
        if (d.profRegAddress)  d.profRegAddress.value  = p.registration_address || '';
        if (d.profLivesAlone)  d.profLivesAlone.checked = !!p.lives_alone;
    },

    async saveProfileInline(e) {
        e.preventDefault();
        const payload = {
            full_name: this.dom.profFullName.value.trim() || null,
            position: this.dom.profPosition.value.trim() || null,
            passport_series: this.dom.profPassSeries.value.trim() || null,
            passport_number: this.dom.profPassNumber.value.trim() || null,
            passport_issued_by: this.dom.profPassIssuedBy.value.trim() || null,
            passport_issued_at: this.dom.profPassIssuedAt.value || null,
            registration_date: this.dom.profRegDate.value || null,
            registration_address: this.dom.profRegAddress?.value.trim() || null,
        };
        const btn = this.dom.profileForm.querySelector('button[type="submit"]');
        const orig = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Сохранение…';
        try {
            this.profile = await api.put('/me/profile', payload);
            toast('Данные сохранены', 'success');
            // После сохранения обновляем баннер + скрываем секцию
            this.updateModalState();
        } catch (err) {
            toast('Ошибка сохранения: ' + err.message, 'error');
        } finally {
            btn.disabled = false;
            btn.innerHTML = orig;
        }
    },

    // Какие поля FamilyMember обязаны быть заполнены для справки
    _familyMemberProblems(m) {
        const problems = [];
        if (!(m.full_name || '').trim()) problems.push('ФИО');
        if (!m.birth_date) problems.push('дата рождения');
        if (!m.arrival_date) problems.push('дата прибытия');
        if (!m.registration_type) problems.push('тип регистрации');
        if (!(m.relation_to_head || '').trim()) problems.push('отношение');
        return problems;
    },

    // Ищет активный договор (или самый свежий) с обязательными полями.
    // Жилец не может его создать сам — этим занимается админ. Если договора
    // нет или нет номера/даты — UI показывает «обратитесь к администратору».
    _activeContract() {
        if (!Array.isArray(this.contracts) || !this.contracts.length) return null;
        const active = this.contracts.find(c => c.is_active);
        const chosen = active || this.contracts[0];
        if (!chosen || !chosen.number || !chosen.signed_date) return null;
        return chosen;
    },

    // Проверка «всё ли готово для заказа». Повторяет серверную валидацию,
    // чтобы UI не гонял запросы за каждым кликом.
    _missingFields() {
        const p = this.profile || {};
        const missing = [];
        if (!(p.full_name || p.username)) missing.push('ФИО');
        if (!p.passport_series || !p.passport_number) missing.push('паспорт (серия и номер)');
        if (!p.registration_date) missing.push('дата регистрации');
        if (!(p.registration_address || '').trim()) missing.push('адрес прописки по паспорту');

        // Договор найма — обязателен, создаёт админ.
        if (!this._activeContract()) {
            missing.push('договор найма (№ и дата) — оформит администратор');
        }

        // Семья: если не «проживаю один» — должна быть непустой и полной.
        if (!p.lives_alone) {
            if (!this.family.length) {
                missing.push('состав семьи или отметка «проживаю один»');
            } else {
                const bad = this.family
                    .map(m => ({ name: m.full_name || '(без ФИО)', problems: this._familyMemberProblems(m) }))
                    .filter(x => x.problems.length > 0);
                if (bad.length) {
                    missing.push(
                        'данные членов семьи: ' +
                        bad.map(x => `${x.name} — ${x.problems.join(', ')}`).join('; ')
                    );
                }
            }
        }
        return missing;
    },

    updateModalState() {
        const missing = this._missingFields();
        const ready = missing.length === 0;
        const p = this.profile || {};

        // Баннеры
        if (this.dom.flcWarn) {
            if (ready) {
                this.dom.flcWarn.style.display = 'none';
            } else {
                this.dom.flcWarn.style.display = 'block';
                this.dom.flcWarn.innerHTML = `
                    <b><i class="fa-solid fa-circle-exclamation"></i> Заполните данные для заказа</b>
                    <div style="margin-top:6px;">
                        ${missing.map(m => `• ${esc(m)}`).join('<br>')}
                    </div>
                `;
            }
        }
        if (this.dom.flcOk) {
            this.dom.flcOk.style.display = ready ? 'block' : 'none';
        }

        // Секция профиля: если в профиле чего-то не хватает — раскрыта.
        const profileMissing =
            !(p.full_name || p.username) ||
            !p.passport_series || !p.passport_number ||
            !p.registration_date ||
            !(p.registration_address || '').trim();
        if (this.dom.profileSection) {
            this.dom.profileSection.open = profileMissing;
        }
        if (this.dom.profileHint) {
            this.dom.profileHint.textContent = profileMissing
                ? '— заполняется один раз'
                : '— сохранено, можно менять';
        }

        // Секция семьи: если lives_alone — свёрнута, бейдж «не требуется»;
        // иначе — если нет/неполная семья — открыта, бейдж «обязательно».
        const familyMissing = !p.lives_alone && (
            !this.family.length ||
            this.family.some(m => this._familyMemberProblems(m).length > 0)
        );
        if (this.dom.familySection) {
            this.dom.familySection.open = familyMissing;
        }
        if (this.dom.familyBadge) {
            if (p.lives_alone) {
                this.dom.familyBadge.textContent = '— «проживаю один»';
                this.dom.familyBadge.style.color = 'var(--success-color)';
            } else if (familyMissing) {
                this.dom.familyBadge.textContent = '— обязательно';
                this.dom.familyBadge.style.color = 'var(--danger-color)';
            } else {
                this.dom.familyBadge.textContent = '— заполнено';
                this.dom.familyBadge.style.color = 'var(--success-color)';
            }
        }
        if (this.dom.familyHintText) {
            this.dom.familyHintText.style.display = p.lives_alone ? 'none' : '';
        }

        // Кнопка заказа
        if (this.dom.flcSubmit) {
            this.dom.flcSubmit.disabled = !ready;
            this.dom.flcSubmit.style.opacity = ready ? '1' : '0.5';
            this.dom.flcSubmit.style.cursor = ready ? 'pointer' : 'not-allowed';
        }
    },

    async toggleLivesAlone() {
        const checked = !!this.dom.profLivesAlone?.checked;
        try {
            this.profile = await api.put('/me/profile', { lives_alone: checked });
            this.updateModalState();
            toast(checked ? 'Отмечено: проживаю один' : 'Отметка снята', 'success');
        } catch (err) {
            this.dom.profLivesAlone.checked = !checked;
            toast('Ошибка: ' + err.message, 'error');
        }
    },

    // =====================================================
    // FAMILY (ленивая, внутри модалки)
    // =====================================================
    async loadFamily() {
        if (!this.dom.familyList) return;
        try {
            this.family = await api.get('/me/family');
            this.renderFamily();
        } catch (e) {
            this.dom.familyList.innerHTML =
                `<div style="padding:10px; color:var(--danger-color); font-size:12px;">Ошибка: ${esc(e.message)}</div>`;
        }
    },

    renderFamily() {
        if (this.dom.familyCount) {
            this.dom.familyCount.textContent = `— (${this.family.length})`;
        }
        if (!this.family.length) {
            this.dom.familyList.innerHTML = `
                <div style="padding:10px; color:var(--text-secondary); font-size:12px; font-style:italic;">
                    Пока никого не добавлено.
                </div>`;
            return;
        }
        const REG_TYPE_LABEL = { permanent: 'По месту жительства', temporary: 'По месту пребывания' };
        this.dom.familyList.innerHTML = `
            <div style="display:flex; flex-direction:column; gap:6px;">
                ${this.family.map(m => {
                    const problems = this._familyMemberProblems(m);
                    const incomplete = problems.length > 0;
                    const relation = (m.relation_to_head || '').trim() || ROLE_LABEL[m.role] || m.role;
                    return `
                    <div style="display:flex; align-items:flex-start; gap:10px; padding:8px 10px;
                                background:${incomplete ? '#fef2f2' : 'var(--bg-page)'};
                                border-radius:6px;
                                border:1px solid ${incomplete ? '#fecaca' : 'var(--border-color)'};">
                        <i class="fa-solid fa-user" style="color:var(--primary-color); font-size:14px; margin-top:3px;"></i>
                        <div style="flex:1; min-width:0;">
                            <div style="font-weight:600; font-size:13px;">
                                ${esc(m.full_name || '(без ФИО)')}
                                <span style="font-weight:normal; color:var(--text-secondary); font-size:11px;">
                                    — ${esc(relation)}
                                </span>
                            </div>
                            <div style="font-size:11px; color:var(--text-secondary); margin-top:2px;">
                                ${m.birth_date ? 'Рожд. ' + esc(fmtDate(m.birth_date)) : '<span style="color:#ef4444;">нет даты рожд.</span>'}
                                · ${m.arrival_date ? 'Прибытие ' + esc(fmtDate(m.arrival_date)) : '<span style="color:#ef4444;">нет даты прибытия</span>'}
                                · ${m.registration_type ? esc(REG_TYPE_LABEL[m.registration_type] || m.registration_type) : '<span style="color:#ef4444;">нет типа рег.</span>'}
                            </div>
                            ${incomplete ? `
                                <div style="margin-top:4px; font-size:11px; color:#991b1b;">
                                    <i class="fa-solid fa-triangle-exclamation"></i> Не хватает: ${esc(problems.join(', '))}
                                </div>` : ''}
                        </div>
                        <button class="icon-btn" data-family-edit="${m.id}" title="Редактировать" style="padding:4px;">
                            <i class="fa-solid fa-pen"></i>
                        </button>
                        <button class="icon-btn" data-family-delete="${m.id}" title="Удалить"
                                style="padding:4px; color:var(--danger-color);">
                            <i class="fa-solid fa-trash"></i>
                        </button>
                    </div>
                `;
                }).join('')}
            </div>
        `;
    },

    openFamilyModal(member) {
        const d = this.dom;
        d.familyForm.reset();
        if (member) {
            d.familyModalTitle.textContent = 'Редактировать';
            d.familyId.value = String(member.id);
            d.familyRole.value = member.role;
            d.familyFullName.value = member.full_name || '';
            d.familyBirthDate.value = member.birth_date || '';
            if (d.familyArrivalDate) d.familyArrivalDate.value = member.arrival_date || '';
            if (d.familyRegType) d.familyRegType.value = member.registration_type || '';
            if (d.familyRelationHead) d.familyRelationHead.value = member.relation_to_head || '';
            if (d.familyPassSeries) d.familyPassSeries.value = member.passport_series || '';
            if (d.familyPassNumber) d.familyPassNumber.value = member.passport_number || '';
            if (d.familyRegDate) d.familyRegDate.value = member.registration_date || '';
        } else {
            d.familyModalTitle.textContent = 'Добавить члена семьи';
            d.familyId.value = '';
        }
        d.familyModal.classList.add('open');
    },

    async submitFamily(e) {
        e.preventDefault();
        const id = this.dom.familyId.value;
        const payload = {
            role: this.dom.familyRole.value,
            full_name: this.dom.familyFullName.value.trim(),
            birth_date: this.dom.familyBirthDate.value || null,
            arrival_date: this.dom.familyArrivalDate?.value || null,
            registration_type: this.dom.familyRegType?.value || null,
            relation_to_head: this.dom.familyRelationHead?.value.trim() || null,
            passport_series: this.dom.familyPassSeries?.value.trim() || null,
            passport_number: this.dom.familyPassNumber?.value.trim() || null,
            registration_date: this.dom.familyRegDate?.value || null,
        };
        const btn = this.dom.familyForm.querySelector('button[type="submit"]');
        const orig = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';
        try {
            if (id) await api.put(`/me/family/${id}`, payload);
            else await api.post('/me/family', payload);
            toast('Сохранено', 'success');
            this.dom.familyModal.classList.remove('open');
            await this.loadFamily();
            this.updateModalState();
        } catch (err) {
            toast('Ошибка: ' + err.message, 'error');
        } finally {
            btn.disabled = false;
            btn.innerHTML = orig;
        }
    },

    async deleteFamily(id) {
        if (!confirm('Удалить запись?')) return;
        try {
            await api.delete(`/me/family/${id}`);
            toast('Удалено', 'success');
            await this.loadFamily();
            this.updateModalState();
        } catch (e) {
            toast('Ошибка: ' + e.message, 'error');
        }
    },

    // =====================================================
    // CERTIFICATES
    // =====================================================
    async loadCerts() {
        if (!this.dom.certsList) return;
        this.certsLoaded = true;
        this.dom.certsList.innerHTML = `
            <div style="padding:20px; text-align:center; color:var(--text-secondary);">
                <i class="fa-solid fa-spinner fa-spin"></i> Загрузка…
            </div>`;
        try {
            this.certs = await api.get('/me/certificates');
            this.renderCerts();
        } catch (e) {
            this.dom.certsList.innerHTML =
                `<div style="padding:16px; color:var(--danger-color);">Ошибка: ${esc(e.message)}</div>`;
        }
    },

    renderCerts() {
        if (!this.certs.length) {
            this.dom.certsList.innerHTML = `
                <div style="padding:30px; text-align:center; color:var(--text-secondary); font-size:13px;">
                    Пока нет заявок. Выберите справку выше и закажите — PDF будет готов за пару секунд.
                </div>`;
            return;
        }
        const TYPE_LABEL = { flc: 'Выписка из ФЛС' };
        this.dom.certsList.innerHTML = `
            <div style="display:flex; flex-direction:column; gap:8px;">
                ${this.certs.map(c => {
                    const meta = STATUS_META[c.status] || { color: '#6b7280', bg: '#f3f4f6', label: c.status };
                    return `
                        <div style="display:flex; align-items:center; gap:12px; padding:12px 14px;
                                    background:var(--bg-card); border-radius:8px; border:1px solid var(--border-color);">
                            <div style="width:40px; height:40px; border-radius:8px; background:${meta.bg};
                                        display:flex; align-items:center; justify-content:center; flex-shrink:0;">
                                <i class="fa-solid fa-file-pdf" style="color:${meta.color};"></i>
                            </div>
                            <div style="flex:1; min-width:0;">
                                <div style="font-weight:600;">${esc(TYPE_LABEL[c.type] || c.type)}</div>
                                <div style="font-size:12px; color:var(--text-secondary); margin-top:2px;">
                                    №${c.id} · ${esc(fmtDate(c.created_at))}
                                    ${c.data?.purpose ? ' · для: ' + esc(c.data.purpose) : ''}
                                </div>
                            </div>
                            <span style="padding:3px 10px; border-radius:12px; background:${meta.bg}; color:${meta.color}; font-size:11px; font-weight:600; white-space:nowrap;">
                                ${esc(meta.label)}
                            </span>
                            ${c.has_pdf ? `
                                <button class="action-btn primary-btn" data-cert-download="${c.id}"
                                        style="padding:6px 12px; font-size:12px; white-space:nowrap;">
                                    <i class="fa-solid fa-download"></i> Скачать
                                </button>` : ''}
                        </div>
                    `;
                }).join('')}
            </div>
        `;
    },

    async downloadCert(id) {
        try {
            await api.download(`/me/certificates/${id}/download`, `Zayavlenie_FLS_${id}.pdf`);
        } catch (e) {
            toast('Ошибка скачивания: ' + e.message, 'error');
        }
    },

    // =====================================================
    // МОДАЛКА ЗАКАЗА — теперь включает inline-профиль/семью
    // =====================================================
    async openFlcModal() {
        // Открываем модалку сразу, грузим данные параллельно
        this.dom.flcModal.classList.add('open');
        this.dom.flcPurpose.value = '';
        this.dom.flcFrom.value = '';
        this.dom.flcTo.value = '';

        // Грузим профиль + семью + договор параллельно
        const tasks = [this.loadProfile(), this.loadFamily()];
        try {
            tasks.push(api.get('/me/rental-contracts').then(r => { this.contracts = r; }).catch(() => {}));
        } catch {}
        await Promise.all(tasks);

        this.populateProfileForm();
        this.updateModalState();

        // Договор найма — обязателен для заказа. Если нет активного с номером
        // и датой — предупреждаем жильца что заказ заблокирован и надо
        // обратиться к администратору.
        const active = this._activeContract();
        this.dom.flcContractBlock.style.display = 'block';
        if (active) {
            this.dom.flcContractInfo.style.background = '#ecfdf5';
            this.dom.flcContractInfo.style.color = '#065f46';
            this.dom.flcContractInfo.innerHTML = `
                <b>№ ${esc(active.number)}</b>
                · от ${esc(fmtDate(active.signed_date))}
                ${active.valid_until ? ' · действует до ' + esc(fmtDate(active.valid_until)) : ''}
            `;
        } else {
            this.dom.flcContractInfo.style.background = '#fef2f2';
            this.dom.flcContractInfo.style.color = '#991b1b';
            this.dom.flcContractInfo.innerHTML = `
                <b><i class="fa-solid fa-triangle-exclamation"></i> Договор найма не оформлен.</b>
                Обратитесь к администратору — он внесёт номер и дату договора, после чего
                вы сможете заказать справку.
            `;
        }
    },

    async submitFlcOrder() {
        // Финальная проверка
        if (this._missingFields().length > 0) {
            toast('Сначала заполните «Мои данные»', 'warning');
            this.updateModalState();
            this.dom.profileSection.open = true;
            return;
        }

        const purpose = this.dom.flcPurpose.value.trim();
        if (!purpose) return toast('Укажите куда предоставить справку', 'error');

        const payload = {
            type: 'flc',
            period_from: this.dom.flcFrom.value || null,
            period_to: this.dom.flcTo.value || null,
            purpose,
        };
        const btn = this.dom.flcSubmit;
        const orig = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Генерация PDF…';
        try {
            const cert = await api.post('/me/certificates', payload);
            this.dom.flcModal.classList.remove('open');
            toast('Заявка принята — PDF готов', 'success');
            await this.loadCerts();
            if (cert.has_pdf) this.downloadCert(cert.id);
        } catch (err) {
            const data = err.data;
            if (data?.detail?.missing_fields) {
                toast('Не заполнено: ' + data.detail.missing_fields.join(', '), 'error');
                this.updateModalState();
                this.dom.profileSection.open = true;
            } else {
                toast('Ошибка: ' + err.message, 'error');
            }
        } finally {
            btn.disabled = false;
            btn.innerHTML = orig;
        }
    },
};
