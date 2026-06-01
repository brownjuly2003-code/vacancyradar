"""TDD для regex field extraction (Phase 4 part 2)."""
from __future__ import annotations


from src.ingest.tg_parse import (
    SalaryParse,
    parse_city,
    parse_remote_type,
    parse_salary,
    parse_seniority,
)


class TestParseSalary:
    def test_empty_returns_none(self):
        assert parse_salary("") == SalaryParse(None, None, None, False)
        assert parse_salary(None) == SalaryParse(None, None, None, False)  # type: ignore[arg-type]

    def test_vague_text_no_numbers(self):
        r = parse_salary("Зарплата по результатам собеседования")
        assert r.min is None and r.max is None
        assert r.disclosed is False

    def test_min_only_with_modifier(self):
        r = parse_salary("ЗП от 250к")
        assert r.min == 250_000
        assert r.max is None
        assert r.currency == "RUR"
        assert r.disclosed is True

    def test_max_only(self):
        r = parse_salary("Зарплата до 500к")
        assert r.min is None
        assert r.max == 500_000
        assert r.disclosed is True

    def test_range_with_dash_and_modifier(self):
        r = parse_salary("Senior Python — 200-300к рублей")
        assert r.min == 200_000
        assert r.max == 300_000
        assert r.currency == "RUR"

    def test_range_with_t_rub_abbreviation(self):
        r = parse_salary("ЗП 300-500 т.р.")
        assert r.min == 300_000
        assert r.max == 500_000
        assert r.currency == "RUR"

    def test_min_max_with_tr_abbreviation(self):
        r_min = parse_salary("ЗП от 300тр")
        assert r_min.min == 300_000
        assert r_min.max is None
        assert r_min.currency == "RUR"

        r_max = parse_salary("ЗП до 500тр")
        assert r_max.min is None
        assert r_max.max == 500_000
        assert r_max.currency == "RUR"

    def test_tr_abbreviation_does_not_match_word_prefix(self):
        r = parse_salary("ЗП обсуждается, команда из 300 трейдеров")
        assert r.min is None
        assert r.max is None
        assert r.disclosed is False

    def test_tyr_slang_abbreviation(self):
        r = parse_salary("ЗП 300-500 тыр")
        assert r.min == 300_000
        assert r.max == 500_000
        assert r.currency == "RUR"

    def test_range_with_full_numbers_spaced(self):
        r = parse_salary("ЗП: 200 000 - 300 000 руб")
        assert r.min == 200_000
        assert r.max == 300_000
        assert r.currency == "RUR"

    def test_range_with_ruble_sign_before_each_number(self):
        r = parse_salary("Salary ₽300000 - ₽500000")
        assert r.min == 300_000
        assert r.max == 500_000
        assert r.currency == "RUR"

    def test_range_with_ruble_sign_after_each_number(self):
        r = parse_salary("Salary 300000₽ - 500000₽")
        assert r.min == 300_000
        assert r.max == 500_000
        assert r.currency == "RUR"

    def test_range_with_ruble_letter_after_each_number(self):
        r = parse_salary("ЗП 300000р - 500000р")
        assert r.min == 300_000
        assert r.max == 500_000
        assert r.currency == "RUR"

    def test_range_with_dotted_ruble_letter_after_each_number(self):
        r = parse_salary("ЗП 300000р. - 500000р.")
        assert r.min == 300_000
        assert r.max == 500_000
        assert r.currency == "RUR"

    def test_range_with_ruble_word_forms_after_each_number(self):
        r = parse_salary("ЗП 300000 рубля - 500000 рублей")
        assert r.min == 300_000
        assert r.max == 500_000
        assert r.currency == "RUR"

    def test_ruble_word_marker_does_not_match_word_prefix(self):
        r = parse_salary("ЗП обсуждается, 30000 рубрик в базе")
        assert r.min is None
        assert r.max is None
        assert r.disclosed is False

    def test_range_with_em_dash(self):
        r = parse_salary("250 000 — 350 000 ₽")
        assert r.min == 250_000
        assert r.max == 350_000
        assert r.currency == "RUR"

    def test_range_with_from_to_words(self):
        r = parse_salary("Зарплата: от 150 000 до 230 000")
        assert r.min == 150_000
        assert r.max == 230_000
        assert r.disclosed is True

    def test_usd_range(self):
        r = parse_salary("$3000-5000 в месяц")
        assert r.min == 3000
        assert r.max == 5000
        assert r.currency == "USD"

    def test_usd_k_range_with_repeated_symbol(self):
        r = parse_salary("Salary: $110k - $150k")
        assert r.min == 110_000
        assert r.max == 150_000
        assert r.currency == "USD"

    def test_range_with_currency_after_each_number(self):
        r = parse_salary("Salary: 3000 USD - 5000 USD gross")
        assert r.min == 3000
        assert r.max == 5000
        assert r.currency == "USD"

    def test_from_to_with_currency_after_each_number(self):
        r = parse_salary("Compensation from 3000 USD to 5000 USD")
        assert r.min == 3000
        assert r.max == 5000
        assert r.currency == "USD"

    def test_range_with_to_separator(self):
        r = parse_salary("Salary 3000 to 5000 USD")
        assert r.min == 3000
        assert r.max == 5000
        assert r.currency == "USD"

    def test_range_with_do_separator(self):
        r = parse_salary("ЗП 300000 до 500000 руб")
        assert r.min == 300_000
        assert r.max == 500_000
        assert r.currency == "RUR"

    def test_from_to_range_allows_dash_before_do(self):
        r = parse_salary("ЗП от 300000 — до 500000 руб")
        assert r.min == 300_000
        assert r.max == 500_000
        assert r.currency == "RUR"

    def test_range_allows_dash_before_up_to(self):
        r = parse_salary("Salary 3000 - up to 5000 USD")
        assert r.min == 3000
        assert r.max == 5000
        assert r.currency == "USD"

    def test_eur_min_only(self):
        r = parse_salary("от 2500 EUR")
        assert r.min == 2500
        assert r.currency == "EUR"

    def test_eur_word_full_form_range(self):
        r = parse_salary("Salary: 80-120k euro")
        assert r.min == 80_000
        assert r.max == 120_000
        assert r.currency == "EUR"

    def test_eur_word_plural_uppercase(self):
        r = parse_salary("Comp: 100k EUROS")
        assert r.min == 100_000
        assert r.currency == "EUR"

    def test_usd_word_dollar_singular(self):
        r = parse_salary("ЗП: от 4000 dollar")
        assert r.min == 4000
        assert r.currency == "USD"

    def test_usd_word_dollars_plural(self):
        r = parse_salary("до 5000 dollars")
        assert r.max == 5000
        assert r.currency == "USD"

    def test_single_value_no_modifier_treated_as_min(self):
        r = parse_salary("ЗП 250000 руб")
        assert r.min == 250_000
        assert r.max is None

    def test_ignores_years_and_phone_numbers_below_10k(self):
        r = parse_salary("Опыт 3+ года, телефон 8 (495) 123-45-67")
        assert r.min is None and r.max is None
        assert r.disclosed is False

    def test_million_modifier_yearly_normalizes_to_monthly(self):
        # "1 млн руб годовых" = 1M/year = ~83k/month, not 1M/month.
        # Pre-cadence-fix this returned 1_000_000 unchanged (real-world bug).
        r = parse_salary("от 1 млн руб годовых")
        assert r.min == 83_333
        assert r.currency == "RUR"

    def test_million_modifier_monthly(self):
        r = parse_salary("ЗП от 1 млн руб")
        assert r.min == 1_000_000
        assert r.currency == "RUR"

    def test_yearly_usd_per_year_normalizes(self):
        r = parse_salary("salary 60000 USD per year")
        assert r.min == 5000
        assert r.currency == "USD"

    def test_yearly_rub_v_god_range_normalizes(self):
        r = parse_salary("ЗП от 1 200 000 до 1 800 000 руб в год")
        assert r.min == 100_000
        assert r.max == 150_000
        assert r.currency == "RUR"

    def test_hourly_rate_dropped(self):
        r = parse_salary("оплата 350 руб/час")
        assert r.min is None
        assert r.max is None
        assert r.disclosed is False

    def test_hourly_keyword_pochasovaya_dropped(self):
        r = parse_salary("почасовая ставка 1000 руб")
        assert r.min is None
        assert r.max is None
        assert r.disclosed is False

    def test_year_word_alone_does_not_trigger_yearly(self):
        # "года" / "1 год опыта" must not be misread as yearly cadence.
        r = parse_salary("Опыт 1 год, ЗП 250000 руб")
        assert r.min == 250_000
        assert r.currency == "RUR"

    def test_currency_detection_default_rur_for_к_suffix(self):
        r = parse_salary("250к")
        assert r.currency == "RUR"

    def test_ignores_url_numbers(self):
        r = parse_salary("https://yandex.ru/jobs/vacancies/--24887")
        assert r.min is None
        assert r.max is None
        assert r.disclosed is False

    def test_url_before_real_salary_does_not_win(self):
        r = parse_salary("[КА Selecty](https://t.me/saba_hunter/207073), 350-550 тыс.₽")
        assert r.min == 350_000
        assert r.max == 550_000
        assert r.currency == "RUR"

    def test_plain_large_audience_number_is_not_salary(self):
        r = parse_salary("канал с аудиторией более 22 700 подписчиков")
        assert r.min is None
        assert r.max is None
        assert r.disclosed is False

    def test_plain_employee_count_is_not_salary(self):
        r = parse_salary("основана в 1997 году, работает более 13 000 человек")
        assert r.min is None
        assert r.max is None
        assert r.disclosed is False

    def test_plain_range_with_salary_context_still_parses(self):
        r = parse_salary("Заработная плата: 27 000 - 31 000 дирхамов ОАЭ")
        assert r.min == 27_000
        assert r.max == 31_000
        assert r.disclosed is True

    def test_dotnet_near_job_id_is_not_salary_context(self):
        r = parse_salary("[[job- 29291] Developer .NET Senior/Master, Brazil]")
        assert r.min is None
        assert r.max is None
        assert r.disclosed is False


class TestParseCity:
    def test_empty(self):
        assert parse_city("") is None
        assert parse_city(None) is None  # type: ignore[arg-type]

    def test_moscow_variants(self):
        assert parse_city("Москва, офис") == "Москва"
        assert parse_city("офис Moscow") == "Москва"

    def test_spb_variants(self):
        assert parse_city("работа в СПб") == "Санкт-Петербург"
        assert parse_city("Питер, удалёнка") == "Санкт-Петербург"
        assert parse_city("Санкт-Петербург") == "Санкт-Петербург"
        assert parse_city("Санкт Петербург, центр") == "Санкт-Петербург"

    def test_other_cities(self):
        assert parse_city("Екатеринбург") == "Екатеринбург"
        assert parse_city("Новосибирск, удалёнка") == "Новосибирск"
        assert parse_city("Нижний Новгород") == "Нижний Новгород"
        assert parse_city("работаем в Казани") == "Казань"

    def test_unknown_city_returns_none(self):
        assert parse_city("Удалёнка из любой точки") is None


class TestParseRemoteType:
    def test_empty_unknown(self):
        assert parse_remote_type("") == "unknown"

    def test_remote_variants(self):
        assert parse_remote_type("Удалёнка") == "remote"
        assert parse_remote_type("полностью удаленно") == "remote"
        assert parse_remote_type("100% remote") == "remote"
        assert parse_remote_type("дистанционно") == "remote"

    def test_hybrid_takes_priority(self):
        assert parse_remote_type("Гибрид: 2 дня офис, 3 удалёнка") == "hybrid"
        assert parse_remote_type("hybrid mode") == "hybrid"

    def test_office(self):
        assert parse_remote_type("офис в центре Москвы") == "office"
        assert parse_remote_type("работа на месте") == "office"

    def test_unknown(self):
        assert parse_remote_type("Senior Python Developer, Acme Corp") == "unknown"


class TestParseSeniority:
    def test_empty_unknown(self):
        assert parse_seniority("") == "unknown"

    def test_lead(self):
        assert parse_seniority("Tech Lead Python") == "lead"
        assert parse_seniority("ищем лида в команду") == "lead"
        assert parse_seniority("Team Lead") == "lead"
        assert parse_seniority("техлид backend") == "lead"

    def test_principal(self):
        assert parse_seniority("Principal Engineer") == "principal"
        assert parse_seniority("Staff Engineer") == "principal"

    def test_senior(self):
        assert parse_seniority("Senior Python Developer") == "senior"
        assert parse_seniority("Старший разработчик") == "senior"
        assert parse_seniority("Sr. Engineer") == "senior"

    def test_middle(self):
        assert parse_seniority("Middle Backend Developer") == "middle"
        assert parse_seniority("Миддл Python") == "middle"

    def test_junior(self):
        assert parse_seniority("Junior Developer") == "junior"
        assert parse_seniority("Джуниор Python") == "junior"
        assert parse_seniority("Младший аналитик") == "junior"

    def test_intern(self):
        assert parse_seniority("Стажёр в команду") == "intern"
        assert parse_seniority("Intern Python") == "intern"

    def test_unknown(self):
        assert parse_seniority("Python Developer") == "unknown"
        assert parse_seniority("Курьер Яндекс.Еда") == "unknown"

    def test_lead_priority_over_senior(self):
        """Если в тексте и senior и lead — приоритет lead (более старший)."""
        assert parse_seniority("Senior Tech Lead") == "lead"

    def test_russian_title_lead_markers(self):
        """Ведущий/Главный в title-позиции → lead. До v3 пропускались."""
        assert parse_seniority("Ведущий программист") == "lead"
        assert parse_seniority("Главный инженер по защите информации") == "lead"
        assert parse_seniority("Ведущая аналитик 1С") == "lead"

    def test_managerial_lead_markers(self):
        """Руководитель/Начальник/Head of в title — lead-tier."""
        assert parse_seniority("Руководитель отдела разработки") == "lead"
        assert parse_seniority("Начальник IT-отдела") == "lead"
        assert parse_seniority("Head of Engineering") == "lead"
        assert parse_seniority("Заместитель руководителя проекта") == "lead"

    def test_architect_is_lead(self):
        assert parse_seniority("Архитектор данных") == "lead"
        assert parse_seniority("Solution Architect") == "lead"

    def test_assistant_helper_is_junior(self):
        """Помощник/Ассистент в начале title → junior-tier."""
        assert parse_seniority("Помощник аналитика") == "junior"
        assert parse_seniority("Ассистент менеджера продукта") == "junior"
        assert parse_seniority("Assistant Product Manager") == "junior"

    def test_no_experience_is_junior(self):
        """«без опыта» в тексте — junior. Студенты/выпускники тоже."""
        assert parse_seniority("Менеджер по продажам без опыта") == "junior"
        assert parse_seniority("приглашаем студентов на постоянную работу") == "junior"
        assert parse_seniority("Программа для выпускников вузов") == "junior"

    def test_internship_word_is_intern(self):
        assert parse_seniority("Программа стажировки в IT") == "intern"
        assert parse_seniority("стажируем разработчиков 3 месяца") == "intern"

    def test_experience_fallback_lead(self):
        """Опыт 7+ лет без explicit маркера → lead via bucket."""
        assert parse_seniority("Разработчик, опыт от 8 лет") == "lead"
        assert parse_seniority("Backend, 10+ лет опыта") == "lead"

    def test_experience_fallback_senior(self):
        assert parse_seniority("Аналитик, опыт от 5 лет") == "senior"
        assert parse_seniority("инженер с опытом более 4 лет") == "senior"

    def test_experience_fallback_middle(self):
        assert parse_seniority("разработчик, опыт от 2 лет") == "middle"
        assert parse_seniority("System engineer, опыт работы 3 года") == "middle"

    def test_experience_one_year_stays_unknown(self):
        """1 год слишком шумный — не помечаем (часто «компания 1 год на рынке»)."""
        assert parse_seniority("инженер, опыт от 1 года") == "unknown"

    def test_explicit_marker_wins_over_experience(self):
        """Если есть junior/senior — игнорируем experience bucket."""
        # Junior с большим опытом (бывают такие постановки) — приоритет explicit.
        assert parse_seniority("Junior разработчик, опыт от 5 лет") == "junior"

    def test_лидер_not_lost(self):
        """`лидер команды` — это lead. v2 случайно исключал `(?!ер)`."""
        assert parse_seniority("Лидер команды backend") == "lead"
        assert parse_seniority("ищем лидера направления") == "lead"

    def test_staff_member_not_principal(self):
        """`staff member` ≠ Staff Engineer. Negative lookahead не ловит."""
        assert parse_seniority("staff member of operations") == "unknown"
        # `staffing` тоже не должен матчиться (другая семантика)
        assert parse_seniority("Senior Staff Engineer") == "principal"

    # === Session 30: title-priority + HR-title demotion ===

    def test_hr_tier_vedushchii_specialist_is_middle(self):
        """`Ведущий специалист` / `Главный специалист` — HR-tier title,
        salary p50=120k (= middle), не technical lead. Должен demote."""
        assert parse_seniority("Ведущий специалист по документообороту") == "middle"
        assert parse_seniority("Главный специалист отдела аналитики") == "middle"
        assert parse_seniority("Ведущий менеджер по продажам") == "middle"
        assert parse_seniority("Главный координатор проектов") == "middle"

    def test_hr_demotion_does_not_break_tech_lead(self):
        """`Ведущий разработчик/архитектор/инженер` остаются lead — HR-pattern
        не должен ловить tech-роли."""
        assert parse_seniority("Ведущий разработчик Python") == "lead"
        assert parse_seniority("Главный инженер DevOps") == "lead"
        assert parse_seniority("Ведущий аналитик данных") == "lead"

    def test_title_priority_over_body(self):
        """Session 28 bug: body «руководитель отдела» перебивал title «младший».
        Теперь title pass идёт первым, body не trigger'ит position-markers."""
        # Body contains «руководитель отдела ищет младшего аналитика» — раньше
        # концат давал lead. Теперь title=`Младший аналитик` → junior.
        assert parse_seniority(
            "Младший аналитик",
            body="руководитель отдела ищет специалиста для работы с данными",
        ) == "junior"

    def test_body_typed_token_still_fires(self):
        """Body pass scанит typed tokens (Senior/Middle/Lead): если title
        без marker'а, body должен дать recall."""
        assert parse_seniority(
            "Аналитик данных",
            body="ищем Senior с опытом построения хранилищ",
        ) == "senior"

    def test_body_position_marker_does_not_fire(self):
        """Title без marker, body содержит «руководитель отдела» — должно
        остаться unknown (position-marker не fire в body)."""
        assert parse_seniority(
            "Аналитик данных",
            body="команду возглавит руководитель отдела с опытом",
        ) == "unknown"

    def test_body_experience_years_fallback(self):
        """Опыт от N лет работает на body pass когда title пустой по markers."""
        assert parse_seniority(
            "Разработчик Python",
            body="требования: опыт от 5 лет коммерческой разработки",
        ) == "senior"

    def test_body_only_lead_token_fires(self):
        """Title без marker, body содержит explicit «Tech Lead» — fire lead."""
        assert parse_seniority(
            "Python Developer",
            body="ищем Tech Lead в команду платформы",
        ) == "lead"

    def test_empty_title_body_only_mode(self):
        """Title пустой, body содержит signal — должен fire без title pass.
        Покрывает branch `if text:` False path."""
        assert parse_seniority("", body="Senior разработчик") == "senior"

    # === Session 35: tighten Russian лид-regex to exclude marketing copy ===

    def test_marketing_лидеры_does_not_fire_lead(self):
        """«лидеры/лидеров/лидерами» (Russian plurals) — marketing-copy
        о компании, не seniority. Session 35: regex `лидер(?:а|у|ом|е)?`
        не матчит plurals."""
        assert parse_seniority(
            "BI-аналитик",
            body="Компания — один из лидеров на рынке России",
        ) == "unknown"
        assert parse_seniority(
            "Разработчик Power BI",
            body="входим в ТОП лидеров среди розничных Компаний",
        ) == "unknown"
        assert parse_seniority(
            "Аналитик-исследователь",
            body="Поработать с лидерами 20+ отраслей",
        ) == "unknown"

    def test_marketing_лидирующая_does_not_fire_lead(self):
        """«лидирующая/лидирующий/лидирующих» (adjective) — marketing copy."""
        assert parse_seniority(
            "Художник ювелирных изделий",
            body="завод занимает лидирующие позиции на рынке",
        ) == "unknown"

    def test_marketing_лидерство_does_not_fire_lead(self):
        """«лидерство/лидерства» (abstract noun) — corporate values copy."""
        assert parse_seniority(
            "Менеджер по продажам",
            body="наши ценности: ответственность, лидерство, командная работа",
        ) == "unknown"

    def test_singular_лидер_still_fires_lead(self):
        """Singular «лидер/лидера/лидером/лидеру» — реальный lead-role,
        сохраняется (regression guard: ранее тест `test_лидер_not_lost`)."""
        assert parse_seniority("Лидер команды backend") == "lead"
        assert parse_seniority("ищем лидера направления") == "lead"
        assert parse_seniority("работаем с лидером команды разработки",
                               body="") == "lead"

    def test_tg_slang_лид_still_fires_lead(self):
        """`#лид` TG-slang token — сохраняется через exact `лид` alt."""
        assert parse_seniority("#вакансия #удалёнка #лид") == "lead"
        assert parse_seniority("ищем лид разработки") == "lead"

    # === Session 36: «лидер» moved to _TITLE_ONLY (marketing-copy in body) ===

    def test_лидер_in_title_still_fires_lead(self):
        """Title «Лидер команды/направления» — реальная позиция, должен
        fire через _TITLE_ONLY_PATTERNS."""
        assert parse_seniority("Лидер команды backend") == "lead"
        assert parse_seniority("Лидер направления разработки") == "lead"
        # two-arg mode тоже работает (title pass)
        assert parse_seniority("Лидер группы аналитики", body="") == "lead"

    def test_лидер_in_body_does_not_fire_lead(self):
        """Body «лидер рынка/российского/отрасли» — marketing-copy, не
        должно fire (audit 400+ HH-lead FPs). Real «лидер команды» в body
        accepted loss (≈10 cases, 40:1 trade)."""
        assert parse_seniority(
            "Разработчик RPA",
            body="Компания — один из лидеров на рынке России",
        ) == "unknown"
        assert parse_seniority(
            "Аналитик данных",
            body="Familia — лидер российского off-price ритейла",
        ) == "unknown"
        assert parse_seniority(
            "BI-аналитик",
            body="мы лидер по продажам в нашей категории",
        ) == "unknown"
        # Even real-looking «лидер команды» в body не fire (accepted loss)
        assert parse_seniority(
            "Junior разработчик",
            body="в команде будет лидер команды и senior",
        ) == "junior"  # junior wins via title

    def test_tg_slang_лид_in_body_still_fires(self):
        """`лид` exact в body (TG hashtag form) — сохраняется."""
        assert parse_seniority(
            "Разработчик",
            body="#вакансия #python #удалёнка #лид",
        ) == "lead"
        assert parse_seniority("", body="опыт от 5 лет") == "senior"
        assert parse_seniority("", body="") == "unknown"

    def test_experience_fallback_requires_anchor_context(self):
        """Session 37: «Более N лет» без 'опыт/стаж/experience' anchor — не fire.

        Company-tenure brag («Компания более 20 лет на рынке», «Уже более 10
        лет создаём») давал ложный lead через unanchored `(?:от|более|свыше)
        N лет` pattern.
        """
        # Company-tenure FPs — no anchor, демотируем в unknown
        assert parse_seniority(
            "Программист 1С",
            body="Наша компания более 20 лет предоставляет услуги клиентам",
        ) == "unknown"
        assert parse_seniority(
            "Аналитик",
            body="Уже более 10 лет мы развиваем продукт и команду",
        ) == "unknown"
        assert parse_seniority(
            "Дизайнер",
            body="Берем строго от 18 лет на эту позицию",
        ) == "unknown"  # age limit, not experience

    def test_director_title_fires_lead(self):
        """Session 38: «директор» в title → lead (recall expansion).

        377 HH-unknown титулов с «директор» (Технический/IT/Арт-/Зам)
        были unclassified. Все clearly lead-tier. Pattern ловит prefix
        `[\\s/(\\-]` для «Арт-директор», «IT-директор».
        """
        assert parse_seniority("Технический директор") == "lead"
        assert parse_seniority("Директор по информационной безопасности") == "lead"
        assert parse_seniority("Арт-директор") == "lead"
        assert parse_seniority("Заместитель директора по IT") == "lead"
        assert parse_seniority("IT директор в производственную компанию") == "lead"
        assert parse_seniority("Director of Engineering") == "lead"
        # Помощник директора — junior pattern fires first (TITLE_ONLY order)
        assert parse_seniority("Помощник директора") == "junior"

    def test_director_body_does_not_fire(self):
        """Session 38: «директор» только в body (HH two-arg) — не fire lead.

        Title-only pattern избегает FPs типа «директор сказал», «наш
        директор», «архив директоров» в body.
        """
        assert parse_seniority(
            "Программист 1С",
            body="Наш генеральный директор поддерживает инициативы",
        ) == "unknown"
        assert parse_seniority(
            "Менеджер по продажам",
            body="Команда: директор по продажам, маркетолог, копирайтер",
        ) == "unknown"

    def test_lead_verb_body_does_not_fire(self):
        """Session 39: generic «lead/лид» declensions moved из SENIORITY
        в TITLE_ONLY. HH body verb-context («to lead customer», «and lead
        positioning»), lead-generation, career-trajectory («рост до лида»),
        interaction («с лидом») больше не fire'ят lead.

        Title-fires preserved. tech_lead/team_lead/тимлид/техлид body-firing
        сохранён через отдельные SENIORITY patterns. `\\bлид\\b` exact (TG
        hashtag) preserved в SENIORITY.
        """
        # Title fires (preserved)
        assert parse_seniority("Lead Developer") == "lead"
        assert parse_seniority("Quant Lead") == "lead"
        assert parse_seniority("Лид разработки") == "lead"  # bare лид via SENIORITY
        # HH body verb «to lead» — НЕ fires
        assert parse_seniority(
            "CRM Manager",
            body="CRM Manager to lead customer retention initiatives",
        ) == "unknown"
        assert parse_seniority(
            "Product Manager",
            body="Product Marketing Manager to lead positioning and go-to-market",
        ) == "unknown"
        # HH body lead-generation — НЕ fires
        assert parse_seniority(
            "Sales Analyst",
            body="Lead generation manager who finds clients",
        ) == "unknown"
        # HH body career-trajectory — НЕ fires (junior wins via title)
        assert parse_seniority(
            "Game Designer",
            body="Работать в связке с тех лидом и продюсером",
        ) == "unknown"
        # tech_lead/team_lead body still fires (separate SENIORITY pattern)
        assert parse_seniority(
            "Python разработчик",
            body="Ищем tech lead для команды",
        ) == "lead"

    def test_lid_hashtag_preserved_in_tg(self):
        """Session 39 regression: bare `\\bлид\\b` preserved в SENIORITY
        для TG-hashtag format (`#лид`, «лид-менеджер»).
        """
        # TG-style title-neutral with hashtag
        assert parse_seniority(
            "Разработчик",
            body="#вакансия #python #удалёнка #лид",
        ) == "lead"
        # «лид-менеджер» (sales-lead manager) — `\\bлид\\b` fires (FP-acceptable,
        # consistent с post-s35 behavior; sales-lead overlaps с TG-slang)
        assert parse_seniority("лид-менеджер продаж") == "lead"

    def test_lead_hashtag_fires_in_tg_body(self):
        """Session 40: `#lead` hashtag fires lead as last-resort SENIORITY.
        Side-effect of s39 generic-lead→TITLE_ONLY: `#lead` token had `#`
        prefix outside `[\\s/(\\-]` allowed set, breaking ~430 hashtag-tagged
        TG vacancies. Pattern placed AFTER senior/middle/junior/intern so
        multi-tag digests (`#senior #lead`) keep their first-seniority
        signal. Verb-safe (`#` required), word-extension-safe (`\\b` rejects
        `#leadership` / `#leadgen` / `#leaddevops`).
        """
        # Pure `#lead` hashtag — title-neutral body fires lead.
        assert parse_seniority(
            "QA Engineer",
            body="#вакансия #удаленка #lead",
        ) == "lead"
        # `#lead` at line start.
        assert parse_seniority(
            "Head of Compliance",
            body="#lead\n#удаленка",
        ) == "lead"
        # `#lead-deliverability` — boundary fires on `-`, lead recovered.
        assert parse_seniority(
            "Sales Manager",
            body="#lead-deliverability #remote",
        ) == "lead"
        # English verb «to lead» (no `#`) — must NOT fire (s39 FP class).
        assert parse_seniority(
            "Marketing Manager",
            body="responsible to lead customer retention",
        ) == "unknown"
        # `#leadership` / `#leadgen` — word continues past `lead`, no `\\b`,
        # no match (avoid skill-tag FPs).
        assert parse_seniority(
            "Sales Manager",
            body="#leadership skills required",
        ) == "unknown"
        assert parse_seniority(
            "Sales Specialist",
            body="#leadgen specialist",
        ) == "unknown"
        # Multi-tag digest: `#senior #lead` keeps senior (first explicit
        # typed token wins; `#lead` only as last resort).
        assert parse_seniority(
            "Frontend Developer",
            body="#вакансия #удаленка #senior #lead",
        ) == "senior"
        # `#junior #lead` digest preserves junior.
        assert parse_seniority(
            "Backend Developer",
            body="#удаленка #junior #lead",
        ) == "junior"

    def test_nachinayuschiy_title_fires_junior(self):
        """Session 41: «начинающий» → junior. Title-only — body часто
        описывает «программу для начинающих специалистов» (onboarding),
        не роль вакансии.

        Audit на 17 749 HH-unknown rows выявил 99 «Начинающий ...»
        titles (Начинающий аналитик/специалист/программист/бизнес-аналитик/
        веб-дизайнер/...) — clean entry-level signal.
        """
        # Title fires
        assert parse_seniority("Начинающий аналитик 1С") == "junior"
        assert parse_seniority("Начинающий специалист") == "junior"
        assert parse_seniority("Начинающий программист") == "junior"
        assert parse_seniority("Начинающий бизнес-аналитик 1С (ERP)") == "junior"
        assert parse_seniority("Графический дизайнер начинающий") == "junior"
        # Declensions
        assert parse_seniority("Ищем начинающего графического дизайнера") == "junior"
        # HH two-arg: body-only «начинающих» НЕ fires (onboarding text)
        assert parse_seniority(
            "DevOps инженер",
            body="У нас выстроена программа адаптации для начинающих специалистов",
        ) == "unknown"
        # Senior title beats начинающий (first-match-wins: senior pattern earlier in SENIORITY,
        # но `начинающий` в TITLE_ONLY — TITLE_ONLY fires first. Check it doesn't override
        # explicit senior typed-token from TITLE_ONLY itself: «Senior начинающий» absurd
        # case, skip; instead check HR-tier wins)
        assert parse_seniority("Ведущий специалист по работе с начинающими") == "middle"

    def test_expert_title_fires_senior(self):
        """Session 42: «эксперт/expert» → senior. 261 HH-unknown titles
        в audit: «Эксперт PostgreSQL/ИБ/направления/планирования/...»,
        «AI Expert», «Solution Expert». В RU corporate hierarchy
        «эксперт» — устоявшийся senior-tier (5-10+ лет опыта).

        Размещение ПОСЛЕ Ведущий/Главный preserves «Главный эксперт» /
        «Ведущий эксперт» → lead via existing patterns.
        """
        # Bare title fires senior
        assert parse_seniority("Эксперт PostgreSQL") == "senior"
        assert parse_seniority("Эксперт по СУБД (DBA PostgreSQL)") == "senior"
        assert parse_seniority("AI Expert") == "senior"
        assert parse_seniority("Эксперт ИБ базовой ИТ-инфраструктуры") == "senior"
        assert parse_seniority("Solution Expert") == "senior"
        # Compound с дефисом
        assert parse_seniority("CX-эксперт в Дивизион Транзакционный бизнес") == "senior"
        # Singular declensions
        assert parse_seniority("Беседа с экспертом по PostgreSQL") == "senior"
        # Plural declensions DROPPED (s43): «экспертов/экспертами/экспертах»
        # — TG team-brag «команда экспертов», «300 экспертов работают»,
        # «топовых экспертов», «с экспертами», «находить экспертов».
        assert parse_seniority("Команда экспертов по DevOps") == "unknown"
        assert parse_seniority("300 экспертов работают в нашей команде") == "unknown"
        assert parse_seniority("Работа с экспертами разных направлений") == "unknown"
        # Ведущий/Главный wins (existing patterns fire first)
        assert parse_seniority("Главный эксперт по аналитике") == "lead"
        assert parse_seniority("Ведущий эксперт-аналитик") == "lead"
        # HR-tier wins over generic Главный
        assert parse_seniority("Главный специалист по экспертизе") == "middle"
        # Negatives — adj form «экспертная/экспертиза» не fires (no \b after
        # эксперт + decline suffix)
        assert parse_seniority("Экспертная оценка проектов") == "unknown"
        assert parse_seniority("Экспертиза кода") == "unknown"
        # HH two-arg: body «300 экспертов работают» = team brag, не роль —
        # TITLE_ONLY не сканит body, поэтому unknown sustains
        assert parse_seniority(
            "Аналитик данных",
            body="Более 300 экспертов ежедневно работают над тем, чтобы",
        ) == "unknown"

    def test_chief_cto_title_fires_lead(self):
        """Session 41: C-suite executives → lead. 14 HH-unknown audit:
        CTO/CIO/CDO/CPO/CEO/COO acronyms + «Chief X Officer» + «Chief
        Engineer» + «Chief product owner». All C-suite = top management
        tier, mapped в lead (same bucket as director).
        """
        # Acronyms
        assert parse_seniority("CTO") == "lead"
        assert parse_seniority("Digital CTO") == "lead"
        assert parse_seniority("CPO / Chief Product Officer (МФО)") == "lead"
        # «Chief X Officer»
        assert parse_seniority("Chief Data Officer (CDO)") == "lead"
        assert parse_seniority("Chief Technical Officer (CTO) Gamedev") == "lead"
        assert parse_seniority("Chief Technology Officer (CTO)") == "lead"
        # «Chief Engineer» / «Chief product owner»
        assert parse_seniority("Chief Engineer") == "lead"
        assert parse_seniority("Chief product owner") == "lead"
        # «CTO / Chief Engineer / технический сооснователь» — first match wins (cto)
        assert parse_seniority(
            "CTO / Chief Engineer / технический сооснователь deeptech-компании"
        ) == "lead"
        # Negative — `Acto` doesn't match cto (no \b before c)
        assert parse_seniority("Acto solutions") == "unknown"
        # Negative — bare «chief» without officer/engineer/owner shouldn't fire
        assert parse_seniority("Chief enthusiast") == "unknown"
        # Principal-regression-guard wins: «Engineering Manager» stays principal
        assert parse_seniority("Engineering Manager") == "principal"

    def test_principal_outranks_architect_director(self):
        """Session 38: «Principal Architect» / «Engineering Manager»
        должны остаться principal, не promoted к lead через post-s38
        architect/director TITLE_ONLY patterns.

        Принципал-tier дублирован в TITLE_ONLY перед architect/director,
        чтобы fire первым.
        """
        assert parse_seniority("Principal Engineer") == "principal"
        assert parse_seniority("Principal Software Architect") == "principal"
        assert parse_seniority("Engineering Manager") == "principal"
        assert parse_seniority("Staff Engineer") == "principal"
        assert parse_seniority("Staff Architect") == "principal"

    def test_architect_title_only_in_hh_mode(self):
        """Session 38: «архитектор/architect» moved to TITLE_ONLY.

        Audit 8 body-fired HH-lead cases: stakeholder/team/education
        refs («(заказчик, архитектор, разработчик, тестировщик)»,
        «Команда: Архитектор ИБ, DevSecOps», «1С-Архитектор бизнеса»
        company name) — 7/8 FPs. Title fires preserved.
        """
        # Title fires → lead
        assert parse_seniority("Архитектор данных") == "lead"
        assert parse_seniority("Software Architect") == "lead"
        assert parse_seniority("Функциональный архитектор 1C:ERP") == "lead"
        # Body only — does NOT fire (HH two-arg mode)
        assert parse_seniority(
            "Бизнес-аналитик",
            body="Согласование ТЗ с заинтересованными лицами (заказчик, архитектор, разработчик)",
        ) == "unknown"
        assert parse_seniority(
            "Программист 1С",
            body="В команде «1С-Архитектор бизнеса» ты сможешь",
        ) == "unknown"
        # TG single-arg — `architect` в любой части scans (TITLE_ONLY
        # охватывает full text в single-arg mode)
        assert parse_seniority(
            "Ищем architect в команду продукта"
        ) == "lead"

    def test_experience_fallback_anchored_still_fires(self):
        """Session 37: anchored phrasings continue to fire.

        «Опыт работы от N лет», «N+ лет опыта», «Requirements: N+ years
        experience», «лет работы», «требуется N лет» — anchor presence
        protects real-experience signal.
        """
        # Direct опыт-anchored (pattern 3, anchored=True)
        assert parse_seniority(
            "Разработчик",
            body="Опыт работы от 7 лет с микросервисами",
        ) == "lead"
        # Pattern 2 N+ лет с anchor "опыта" сразу после
        assert parse_seniority(
            "Разработчик",
            body="5+ лет опыта на Python и Django",
        ) == "senior"
        # English Requirements / experience anchor
        assert parse_seniority(
            "Developer",
            body="Requirements:\n- 8+ years programming experience",
        ) == "lead"
        # "лет работы" anchor
        assert parse_seniority(
            "Инженер",
            body="От 4 лет работы с распределёнными системами",
        ) == "senior"
