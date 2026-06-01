"""Regex-based field extraction для Telegram-сообщений вакансий.

Сложные кейсы (vague salary, неоднозначный город) уходят в LLM fallback в Phase 5.
Здесь — детерминированный baseline покрывающий ~80% обычных постов в RU TG-каналах.

Поля по `slim-active-v1`:
- salary_rub_min/max + currency + disclosed
- city
- remote_type ∈ {office,hybrid,remote,unknown}
- seniority ∈ {intern,junior,middle,senior,lead,principal,unknown}
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class SalaryParse:
    min: int | None
    max: int | None
    currency: str | None  # 'RUR' | 'USD' | 'EUR' | None
    disclosed: bool


_NUM = r"\d{1,3}(?:[\s ]\d{3})+|\d+"
_MOD = r"к|k|тыс\.?|т\.?\s*р\.?(?=\W|$)|тр(?=\W|$)|тыр(?=\W|$)|тысяч[аиу]?|млн\.?|миллион[аов]*"
_CUR_MARKER = (
    r"(?:[$€₽]|\busd\b|\bdollars?\b|\beur\b|\beuros?\b|"
    r"\brur\b|\brub\b|руб(?:\.|л(?:ь|я|ей))?(?=\W|$)|р\.?(?=\W|$))"
)

_NUMBER_RE = re.compile(
    rf"(?P<num>{_NUM})\s*(?P<mod>{_MOD})?",
    re.IGNORECASE,
)
_RANGE_RE = re.compile(
    rf"(?:{_CUR_MARKER}\s*)?(?P<n1>{_NUM})\s*(?P<m1>{_MOD})?(?:\s*{_CUR_MARKER})?\s*(?:[-—–]\s*(?:(?:up\s+)?to|до)?|\b(?:to|до)\b)\s*"
    rf"(?:{_CUR_MARKER}\s*)?(?P<n2>{_NUM})\s*(?P<m2>{_MOD})?(?:\s*{_CUR_MARKER})?",
    re.IGNORECASE,
)
_FROM_TO_RE = re.compile(
    rf"(?:от|from)\s*(?:{_CUR_MARKER}\s*)?(?P<n1>{_NUM})\s*(?P<m1>{_MOD})?(?:\s*{_CUR_MARKER})?\s*"
    rf"(?:[-—–]\s*)?(?:до|to)\s*(?:{_CUR_MARKER}\s*)?(?P<n2>{_NUM})\s*(?P<m2>{_MOD})?(?:\s*{_CUR_MARKER})?",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_RUB_WORD_PREFIX_RE = re.compile(
    r"\s*руб(?!\.?(?:\W|$)|л(?:ь|я|ей)(?:\W|$))\w+",
    re.IGNORECASE,
)
_SALARY_CONTEXT_RE = re.compile(
    r"зарплат|заработн|зп\b|оклад|доход|компенсац|вилка|salary|compensation|"
    r"gross|(?<!\.)\bnet\b|"
    r"на\s+руки|до\s+вычета|после\s+вычета",
    re.IGNORECASE,
)
# Cadence markers — strong, salary-attached. Hourly cases are dropped because
# we don't know hours/month; yearly cases are divided by 12 before clamp.
_HOURLY_CADENCE_RE = re.compile(
    r"(?:/\s*(?:час|hour|hr)\b|per\s+hour|почасов\w*|"
    r"\bв\s+час\b|за\s+час\b|руб\.?\s*/\s*час|₽\s*/\s*час)",
    re.IGNORECASE,
)
_YEARLY_CADENCE_RE = re.compile(
    r"(?:/\s*(?:year|yr)\b|per\s+year|annually|"
    r"годов(?:ая|ой|ых|ого|ому|ое|ую)\b|\bв\s+год\b)",
    re.IGNORECASE,
)

_CITY_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:москв|moscow)", re.IGNORECASE), "Москва"),
    (re.compile(r"\b(?:санкт[\s\-]?петербург|спб|питер)", re.IGNORECASE), "Санкт-Петербург"),
    (re.compile(r"\bновосибирск", re.IGNORECASE), "Новосибирск"),
    (re.compile(r"\bекатеринбург", re.IGNORECASE), "Екатеринбург"),
    (re.compile(r"\bказан", re.IGNORECASE), "Казань"),
    (re.compile(r"\bнижн[еёийя][^\s]*[\s\-]+новгород", re.IGNORECASE), "Нижний Новгород"),
    (re.compile(r"\bкраснодар", re.IGNORECASE), "Краснодар"),
    (re.compile(r"\bсамар", re.IGNORECASE), "Самара"),
    (re.compile(r"\bуф[аы]", re.IGNORECASE), "Уфа"),
    (re.compile(r"\bперм[ьи]", re.IGNORECASE), "Пермь"),
    (re.compile(r"\bволгоград", re.IGNORECASE), "Волгоград"),
    (re.compile(r"\bворонеж", re.IGNORECASE), "Воронеж"),
    (re.compile(r"\bростов[\s\-]?на[\s\-]?дону", re.IGNORECASE), "Ростов-на-Дону"),
)

# Patterns split by scope (session 30):
#   - _TITLE_ONLY_PATTERNS — position-title markers (Ведущий/Главный/
#     Руководитель/Помощник). В RU-вакансиях эти слова canonical signal
#     ТОЛЬКО когда в title роли. В body они часто ссылаются на нанимающего
#     менеджера или контекст («руководитель отдела ищет аналитика»), а не
#     на уровень самой роли. Session 28 scanned body для recall, и эти
#     position-markers начали false-positively повышать роли до lead.
#   - _SENIORITY_PATTERNS — typed-token markers (Senior/Middle/Lead/etc).
#     Эти токены unambiguous: где бы ни встретились, они описывают
#     уровень роли. Безопасно scanить в body.
#
# parse_seniority(title, body=...) делает:
#   1) Title pass: TITLE_ONLY ∪ SENIORITY (всё применимо к title).
#   2) Body pass: только SENIORITY (typed tokens). Position-markers
#      намеренно НЕ scanятся в body.
#   3) experience-years fallback на body.
#
# Внутри title pass HR-tier `Ведущий/Главный + специалист/менеджер/
# координатор/инспектор/консультант` идёт ПЕРВЫМ среди TitleOnly и
# демотится в `middle` (salary distribution на our HH corpus подтверждает:
# «Ведущий специалист» p50=120k = middle-tier, не technical lead).
_TITLE_ONLY_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # HR-tier "Ведущий/Главный + специалист/менеджер..." → middle (не lead).
    # Должно идти ПЕРЕД generic Ведущий/Главный pattern.
    (re.compile(
        r"(?:^|[\s/(])(?:ведущ\w+|главн\w+)\s+"
        r"(?:специалист|менеджер|координатор|инспектор|консультант|"
        r"бухгалтер|экономист|юрист|документовед)\b",
        re.IGNORECASE,
    ), "middle"),
    # Russian title-position markers (Ведущий/Главный/Руководитель/
    # Начальник/Head of). Title-only — не fire в body.
    (re.compile(
        r"(?:^|[\s/(])(?:ведущ(?:ий|ая|его|ему)|главн(?:ый|ая|ого|ому))\b",
        re.IGNORECASE,
    ), "lead"),
    (re.compile(
        r"(?:^|[\s/(])(?:руководител[ьяюие]|нач(?:альник|альница|альник[ау])|"
        r"head\s+of|зам(?:еститель)?\s+руковод)",
        re.IGNORECASE,
    ), "lead"),
    # помощник/ассистент — title-position only
    (re.compile(
        r"(?:^|[\s/(])(?:помощник|ассистент|assistant)\b", re.IGNORECASE
    ), "junior"),
    # Russian noun «лидер» — title-only (session 36). Audit на 6 009 HH-lead
    # post-s35: 400+ body-fired «лидер рынка/российского/отрасли/...» — pure
    # marketing copy. Real «лидер команды» в body lost ≈10 cases. Trade-off
    # justified (40:1 precision lift). TG-slang `лид` остаётся в body-firing.
    (re.compile(
        r"(?:^|[\s/(])(?:лидер(?:а|у|ом|е)?)\b", re.IGNORECASE
    ), "lead"),
    # Principal-tier — duplicated в TITLE_ONLY (session 38) чтобы fire
    # ПЕРЕД director/architect: «Principal Architect»/«Engineering Manager
    # Architect» должен остаться principal, а не promoted к lead через
    # post-s38 architect TITLE_ONLY pattern. Body-firing уже covered тем же
    # pattern в _SENIORITY_PATTERNS.
    (re.compile(
        r"\b(?:principal|стафф|staff(?!\s*(?:member|of|количество))|"
        r"engineering\s+manager|инженерный\s+менеджер)\b",
        re.IGNORECASE,
    ), "principal"),
    # «директор» — title-only recall expansion (session 38). 377 HH unknown
    # cases с «директор» в title (Технический/Финансовый/Арт-/Зам директора,
    # «Director of X») были unclassified — все clearly lead-tier roles.
    # `[\s/(\-]` prefix ловит «Арт-директор», «IT-директор». В body может
    # ссылаться на нанимающего/компанию — title-only режим избегает FPs.
    (re.compile(
        r"(?:^|[\s/(\-])директор\w*|\bdirector\b", re.IGNORECASE
    ), "lead"),
    # «архитектор/architect» — title-only (session 38). Post-s37 audit
    # 8 body-fired HH-lead cases: «(заказчик, архитектор, разработчик,
    # тестировщик)» (stakeholder list), «образование (архитектор, дизайнер)»
    # (education backgrounds), «Команда: Архитектор ИБ, DevSecOps» (team
    # composition), «1С-Архитектор бизнеса» (company name) — 7/8 clean FPs.
    # Move в title-only сохраняет real «Архитектор данных/ПО» titles, drops
    # body-side noise.
    (re.compile(
        r"(?:^|[\s/(\-])(?:архитектор|architect)\w*", re.IGNORECASE
    ), "lead"),
    # Generic «lead/лид» declensions — title-only (session 39). Audit 21
    # title-neutral HH-lead body-fires выявил ~75% FPs:
    #  - English verb «to lead» / «to lead customer/development/positioning/
    #    in-field/the team», «and lead», «will lead»
    #  - «Lead generation» (sales/marketing term, не seniority)
    #  - Team composition («Команда Team lead, Backend developers»)
    #  - Career-trajectory («вырасти до лида», «рост до Quant Lead»)
    #  - Interaction («совместно с техническим лидом»)
    # Body-firing для tech_lead/team_lead/тимлид/техлид сохраняется через
    # отдельные patterns в _SENIORITY_PATTERNS (более-специфичные первыми).
    # `\bлид\b` exact match остаётся в SENIORITY ниже — TG-hashtag `#лид`.
    (re.compile(
        r"(?:^|[\s/(\-])(?:lead|лид(?:а|у|ом|е))\b", re.IGNORECASE
    ), "lead"),
    # C-suite executives → lead (session 41). 14 HH-unknown titles в audit:
    # CTO/CIO/CDO/CPO/CEO/COO acronyms + «Chief X Officer» + «Chief Engineer» +
    # «Chief product owner». All C-suite = top management tier; mapped к lead
    # (same bucket as director). Title-only — в body «CEO компании Y» — это
    # mention нанимающего, не роль вакансии.
    (re.compile(
        r"\b(?:cto|cio|cdo|cpo|ceo|coo|"
        r"chief\s+\S+\s+officer|chief\s+engineer|chief\s+product\s+owner)\b",
        re.IGNORECASE,
    ), "lead"),
    # «начинающий» → junior (session 41). 99 HH-unknown titles: «Начинающий
    # аналитик/специалист/программист/бизнес-аналитик/консультант/...» — clean
    # entry-level signal. Title-only — body часто описывает «программу для
    # начинающих специалистов» (onboarding text), не роль самой вакансии.
    (re.compile(
        r"\bначинающ(?:ий|ая|его|ему|их|ие)\b", re.IGNORECASE
    ), "junior"),
    # «эксперт/expert» → senior (session 42, tightened к singular в s43).
    # Audit на 261 HH-unknown titles: «Эксперт PostgreSQL/ИБ/направления/
    # планирования/...», «AI Expert», «Solution Expert» — все singular.
    # В RU corporate hierarchy «эксперт» — senior-tier (5-10+ лет опыта).
    # Размещение ПОСЛЕ Ведущий/Главный preserves «Главный эксперт» / «Ведущий
    # эксперт» → lead via existing patterns.
    #
    # Singular declensions only (а|у|ом|е) — plural forms (ы|ов|ам|ами|ах)
    # dropped в s43 после TG body-fire audit: 177/413 body-fires были plural
    # team-brag («команда экспертов», «300 экспертов работают», «топовых
    # экспертов», «с экспертами», «находить экспертов»). Real plural role
    # mentions редки. `\bэксперт(?:а|у|ом|е)?\b` тоже rejects adj
    # «экспертный/экспертная» и noun «экспертиза/экспертно» (no \b после
    # эксперт+suffix).
    (re.compile(
        r"\b(?:эксперт(?:а|у|ом|е)?|expert)\b", re.IGNORECASE
    ), "senior"),
)

_SENIORITY_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # tech / team / staff — most-specific first
    (re.compile(r"\b(?:tech\s*lead|техлид\w*)\b", re.IGNORECASE), "lead"),
    (re.compile(r"\b(?:team\s*lead|тимлид\w*|\bтл\b)", re.IGNORECASE), "lead"),
    (re.compile(
        r"\b(?:principal|стафф|staff(?!\s*(?:member|of|количество))|"
        r"engineering\s+manager|инженерный\s+менеджер)\b",
        re.IGNORECASE,
    ), "principal"),
    # architect — moved to _TITLE_ONLY_PATTERNS в session 38 (body-fired
    # cases overwhelmingly stakeholder/team/education refs, не role).
    # «лид» exact bare — TG-hashtag preservation (`#лид`, `#вакансия #лид`).
    # Generic «lead» + declensions «лида/лиду/лидом/лиде» moved в TITLE_ONLY
    # (session 39, body-fired FPs ~75%: verbs, lead-generation, team
    # composition, career trajectories).
    (re.compile(r"\bлид\b", re.IGNORECASE), "lead"),
    # senior
    (re.compile(
        r"\b(?:senior|сеньор\w*|старш(?:ий|ая|его|ему)|sr\.?)\b", re.IGNORECASE
    ), "senior"),
    # middle
    (re.compile(
        r"\b(?:middle|миддл\w*|мидл\w*|mid[- ]?level)\b", re.IGNORECASE
    ), "middle"),
    # junior
    (re.compile(
        r"\b(?:junior|джун\w*|младш(?:ий|ая|его|ему)|jr\.?)\b", re.IGNORECASE
    ), "junior"),
    # intern + studentов/выпускников (entry-level)
    (re.compile(
        r"\b(?:intern\w*|стаж[её]р\w*|стажир\w*|стажировк\w*)\b", re.IGNORECASE
    ), "intern"),
    (re.compile(r"\bбез\s+опыт", re.IGNORECASE), "junior"),
    (re.compile(
        r"\b(?:выпускник\w*|студент\w*|graduate)\b", re.IGNORECASE
    ), "junior"),
    # `#lead` hashtag — TG-tag last-resort (session 40). Side-effect of
    # s39 generic-lead→TITLE_ONLY: 408 pure `#lead` + ~20 `#lead-X` tokens
    # stopped firing in body pass. Placed AFTER senior/middle/junior/intern
    # so multi-tag digests (`#senior #lead`, `#грейд #junior #lead`) keep
    # their first-seniority signal — `#lead` only fires when no other
    # typed token is present. Verb-safe (`#` required), word-extension-
    # safe (`\b` rejects `#leadership` / `#leadgen` / `#leaddevops`).
    (re.compile(r"#lead\b", re.IGNORECASE), "lead"),
)

# Experience-years fallback — used only when no explicit seniority marker fires.
# Buckets calibrated against RU IT market norms (см. salary distribution
# на known-seniority subset): 7+ → lead, 4-6 → senior, 2-3 → middle.
# 1 год не помечаем — слишком много false-positives на «1 год опыта в X» внутри
# benefits-блока («мы на рынке 1 год»).
#
# Каждый pattern помечен `_anchored=True` если он содержит контекстное слово
# (опыт/experience) встроенным; иначе требует наличия anchor-слова в окне
# ±70 chars вокруг матча — иначе «Более 20 лет на рынке» (company brag)
# давал ложный lead. Session 37 (2026-05-20).
_EXPERIENCE_PATTERNS: tuple[tuple[re.Pattern[str], bool], ...] = (
    (re.compile(r"(?:от|более|свыше)\s*(\d{1,2})\s*(?:лет|года|year)", re.IGNORECASE),
     False),
    (re.compile(r"(\d{1,2})\s*\+\s*(?:лет|года|year)", re.IGNORECASE), False),
    (re.compile(
        r"опыт[а]?\s+(?:работы\s+)?(?:от\s+)?(\d{1,2})\s*(?:лет|года|year)",
        re.IGNORECASE,
    ), True),
)

_EXPERIENCE_ANCHOR_RE = re.compile(
    r"опыт|стаж|experience|requirement|требу|лет\s+работ|years\s+work|years\s+of",
    re.IGNORECASE,
)


def _detect_currency(text_lc: str) -> str | None:
    if "$" in text_lc or re.search(r"\busd\b|\bdollars?\b|долл", text_lc):
        return "USD"
    if "€" in text_lc or re.search(r"\beur\b|\beuros?\b|евро", text_lc):
        return "EUR"
    if re.search(r"₽|руб(?:\.|л(?:ь|я|ей))?(?=\W|$)|\brur\b|\brub\b|\d\s*(?:к|k|тыс|млн)\b|\d\s*т\.?\s*р\.?|\d\s*тр\b|\d\s*тыр\b|\d\s*р\.?(?=\W|$)", text_lc):
        return "RUR"
    return None


def _expand(num: int, mod: str | None) -> int:
    if not mod:
        return num
    m = mod.lower().rstrip(".")
    if m.replace(" ", "").replace(".", "") in {"тр", "тыр"}:
        return num * 1000
    if m in {"к", "k", "тыс", "тысяч", "тысячи", "тысячу", "тысяча"}:
        return num * 1000
    if m in {"млн", "миллион", "миллионов", "миллиона"}:
        return num * 1_000_000
    return num


def _to_int(raw: str) -> int:
    return int(raw.replace(" ", "").replace(" ", ""))


def _in_sensible_range(value: int, currency: str | None) -> bool:
    if currency in (None, "RUR"):
        return 10_000 <= value <= 10_000_000
    return 500 <= value <= 200_000


def _has_salary_context(text_lc: str, start: int, end: int) -> bool:
    window = text_lc[max(0, start - 60) : min(len(text_lc), end + 60)]
    return bool(_SALARY_CONTEXT_RE.search(window))


def _detect_cadence(text_lc: str, start: int, end: int) -> str | None:
    """Return 'hour' / 'year' / None inferred from markers near salary match.

    Window ±40 chars vs the typical "350 руб/час" / "60k usd/year" attachment.
    Hour markers take precedence — if both present (rare), drop is safer.
    """
    window = text_lc[max(0, start - 40) : min(len(text_lc), end + 40)]
    if _HOURLY_CADENCE_RE.search(window):
        return "hour"
    if _YEARLY_CADENCE_RE.search(window):
        return "year"
    return None


def _apply_cadence(value: int, cadence: str | None) -> int | None:
    """Convert non-monthly cadence to monthly-equivalent or drop hourly."""
    if cadence == "hour":
        return None
    if cadence == "year":
        return value // 12
    return value


def parse_salary(text: str) -> SalaryParse:
    """Извлечь min/max/currency из свободного текста сообщения.

    Поддерживается: "от 200к", "200-300к" (модификатор от второго числа
    распространяется на первое), "до 500к", "$3000-5000", "ЗП: 250 000 ₽".
    Vague ("хорошая зарплата") → all None, disclosed=False.
    """
    if not text:
        return SalaryParse(None, None, None, False)

    text_lc = _URL_RE.sub(" ", text.lower())
    currency = _detect_currency(text_lc)

    from_to_match = _FROM_TO_RE.search(text_lc)
    if from_to_match:
        try:
            v1 = _to_int(from_to_match.group("n1"))
            v2 = _to_int(from_to_match.group("n2"))
        except ValueError:
            v1 = v2 = 0
        m1, m2 = from_to_match.group("m1"), from_to_match.group("m2")
        propagated_mod = m1 or m2
        v1 = _expand(v1, m1 or propagated_mod)
        v2 = _expand(v2, m2 or propagated_mod)
        if currency is None and not _has_salary_context(text_lc, from_to_match.start(), from_to_match.end()):
            return SalaryParse(None, None, currency, False)
        cadence = _detect_cadence(text_lc, from_to_match.start(), from_to_match.end())
        if cadence == "hour":
            return SalaryParse(None, None, currency, False)
        v1_norm = _apply_cadence(v1, cadence)
        v2_norm = _apply_cadence(v2, cadence)
        if v1_norm is None or v2_norm is None:
            return SalaryParse(None, None, currency, False)
        if _in_sensible_range(v1_norm, currency) and _in_sensible_range(v2_norm, currency):
            lo, hi = sorted([v1_norm, v2_norm])
            return SalaryParse(min=lo, max=hi, currency=currency, disclosed=True)

    # Range: "200к-300к" / "200-300к" / "$3000-5000". Модификатор пробрасывается
    # на пропущенное второе/первое число (типичный shorthand).
    range_match = _RANGE_RE.search(text_lc)
    if range_match:
        try:
            v1 = _to_int(range_match.group("n1"))
            v2 = _to_int(range_match.group("n2"))
        except ValueError:
            v1 = v2 = 0
        m1, m2 = range_match.group("m1"), range_match.group("m2")
        propagated_mod = m1 or m2
        v1 = _expand(v1, m1 or propagated_mod)
        v2 = _expand(v2, m2 or propagated_mod)
        if currency is None and not _has_salary_context(text_lc, range_match.start(), range_match.end()):
            return SalaryParse(None, None, currency, False)
        cadence = _detect_cadence(text_lc, range_match.start(), range_match.end())
        if cadence == "hour":
            return SalaryParse(None, None, currency, False)
        v1_norm = _apply_cadence(v1, cadence)
        v2_norm = _apply_cadence(v2, cadence)
        if v1_norm is None or v2_norm is None:
            return SalaryParse(None, None, currency, False)
        if _in_sensible_range(v1_norm, currency) and _in_sensible_range(v2_norm, currency):
            lo, hi = sorted([v1_norm, v2_norm])
            return SalaryParse(min=lo, max=hi, currency=currency, disclosed=True)

    tokens: list[tuple[int, int]] = []
    for m in _NUMBER_RE.finditer(text_lc):
        try:
            num = _to_int(m.group("num"))
        except ValueError:
            continue
        if _RUB_WORD_PREFIX_RE.match(text_lc[m.end() :]):
            continue
        num = _expand(num, m.group("mod"))
        if currency is None and not _has_salary_context(text_lc, m.start(), m.end()):
            continue
        cadence = _detect_cadence(text_lc, m.start(), m.end())
        if cadence == "hour":
            continue
        adjusted = _apply_cadence(num, cadence)
        if adjusted is None:
            continue
        if _in_sensible_range(adjusted, currency):
            tokens.append((m.start(), adjusted))

    if not tokens:
        return SalaryParse(None, None, currency, False)

    first_pos, first_val = tokens[0]
    before = text_lc[max(0, first_pos - 20) : first_pos]
    if re.search(r"\bот\s|\bfrom\s", before):
        return SalaryParse(min=first_val, max=None, currency=currency, disclosed=True)
    if re.search(r"\bдо\s|\bup\s+to\s", before):
        return SalaryParse(min=None, max=first_val, currency=currency, disclosed=True)

    # одиночное число с явной валютой → min (типичный TG-формат "ЗП 250к")
    return SalaryParse(min=first_val, max=None, currency=currency, disclosed=True)


def parse_city(text: str) -> str | None:
    if not text:
        return None
    for pat, canonical in _CITY_PATTERNS:
        if pat.search(text):
            return canonical
    return None


def parse_remote_type(text: str) -> str:
    """office/hybrid/remote/unknown. Гибрид имеет приоритет над remote/office."""
    if not text:
        return "unknown"
    t = text.lower()
    if re.search(r"\bгибрид|hybrid", t):
        return "hybrid"
    if re.search(r"\bудал[её]н|\bremote|\bдистанц", t):
        return "remote"
    if re.search(r"\bофис(?!н)|\boffice|\bна\s+месте", t):
        return "office"
    return "unknown"


def parse_seniority(text: str, body: str = "") -> str:
    """Extract seniority с title-priority.

    Single-arg mode (TG path, `parse_seniority(message_text)`):
        Сканит TitleOnly ∪ SENIORITY patterns по всему тексту. Fallback на
        experience-years. Position-markers (Ведущий/Главный/Руководитель)
        могут fire где угодно — для TG это OK, разделить title vs body
        в message-формате трудно.

    Two-arg mode (HH path, `parse_seniority(title, body=teaser+fts)`):
        1. Title pass — TitleOnly ∪ SENIORITY по `text`. Первый матч побеждает.
        2. Body pass — только SENIORITY (typed tokens) по `body`. Position-
           markers (Ведущий/Руководитель/Помощник) НЕ scанятся в body —
           они в body обычно ссылаются на нанимающего, не на роль.
        3. experience-years fallback на body.

    Returns level ∈ {intern,junior,middle,senior,lead,principal} или "unknown".
    """
    if not text and not body:
        return "unknown"

    # Title pass — TitleOnly first (most-specific), then SENIORITY.
    # HR-tier «Ведущий специалист» демотится в middle через TitleOnly.
    if text:
        for pat, level in _TITLE_ONLY_PATTERNS:
            if pat.search(text):
                return level
        for pat, level in _SENIORITY_PATTERNS:
            if pat.search(text):
                return level

    # Body pass — only SENIORITY (typed tokens), no position-markers.
    if body:
        for pat, level in _SENIORITY_PATTERNS:
            if pat.search(body):
                return level
        return _seniority_from_experience(body) or "unknown"

    return _seniority_from_experience(text) or "unknown"


def _seniority_from_experience(text: str) -> str | None:
    if not text:
        return None
    max_years = 0
    for pat, anchored in _EXPERIENCE_PATTERNS:
        for m in pat.finditer(text):
            if not anchored:
                ctx_start = max(0, m.start() - 70)
                ctx_end = min(len(text), m.end() + 50)
                if not _EXPERIENCE_ANCHOR_RE.search(text[ctx_start:ctx_end]):
                    continue
            try:
                n = int(m.group(1))
            except (ValueError, IndexError):
                continue
            if 1 <= n <= 20 and n > max_years:
                max_years = n
    if max_years == 0:
        return None
    if max_years >= 7:
        return "lead"
    if max_years >= 4:
        return "senior"
    if max_years >= 2:
        return "middle"
    return None
