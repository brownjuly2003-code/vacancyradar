from __future__ import annotations

from pathlib import Path

import pytest

from src.enrich.skills_match import _load_taxonomy, extract_skills


@pytest.fixture(autouse=True)
def _clear_cache():
    _load_taxonomy.cache_clear()
    yield
    _load_taxonomy.cache_clear()


def test_empty_input_returns_empty_list():
    assert extract_skills(None) == []
    assert extract_skills("") == []
    assert extract_skills("   ") == []


def test_canonical_match_case_insensitive():
    assert "Python" in extract_skills("опыт PYTHON 3")


def test_alias_match_returns_canonical():
    # «питон» → «Python», «кх» → «ClickHouse»
    skills = extract_skills("Знаем питон и кх")
    assert "Python" in skills
    assert "ClickHouse" in skills


def test_word_boundary_no_false_positive_inside_word():
    """«питон» не матчится внутри «питонщик» (кириллица + \\w lookaround)."""
    assert extract_skills("Питонщик с опытом") == []


def test_longer_alternative_wins_over_shorter():
    """«React Native» приоритетнее «React» благодаря sort by len desc."""
    skills = extract_skills("React Native + Redux")
    assert "React Native" in skills
    assert "Redux" in skills
    assert "React" not in skills  # «React Native» съел React


def test_special_chars_skills_match():
    skills = extract_skills("Опыт C++ и C# обязателен, .NET плюсом")
    assert "C++" in skills
    assert "C#" in skills
    assert ".NET" in skills


def test_dedup_and_sort():
    skills = extract_skills("python python PYTHON Python питон")
    assert skills == ["Python"]


def test_multi_skill_extract():
    skills = extract_skills("Python, Django, PostgreSQL, Redis, Docker, k8s, Grafana")
    assert set(skills) == {
        "Python", "Django", "PostgreSQL", "Redis",
        "Docker", "Kubernetes", "Grafana",
    }


def test_taxonomy_path_override(tmp_path: Path):
    custom = tmp_path / "tiny.yaml"
    custom.write_text(
        "- {canonical: FooLang, category: languages, aliases: [foo, фу]}\n",
        encoding="utf-8",
    )
    assert extract_skills("работа с FOO", taxonomy_path=custom) == ["FooLang"]
    assert extract_skills("используем фу", taxonomy_path=custom) == ["FooLang"]


def test_empty_taxonomy_returns_empty(tmp_path: Path):
    custom = tmp_path / "empty.yaml"
    custom.write_text("[]\n", encoding="utf-8")

    assert extract_skills("Python Django", taxonomy_path=custom) == []


def test_empty_alias_is_ignored(tmp_path: Path):
    custom = tmp_path / "empty_alias.yaml"
    custom.write_text(
        "- canonical: FooLang\n"
        "  category: languages\n"
        "  aliases:\n"
        "    - ''\n"
        "    - foo\n",
        encoding="utf-8",
    )

    assert extract_skills("foo", taxonomy_path=custom) == ["FooLang"]


# === Session 31: short-canonical guard (C / R false-positives) ===

def test_short_canonical_C_no_false_positive_on_C_level():
    """`C-level` (executives), `C` standalone в нормальном тексте не должен
    срабатывать как C-language. До v3 fire'ил (auto-add canonical-as-variant)."""
    assert "C" not in extract_skills("Работаем с C-level executives ежедневно")
    assert "C" not in extract_skills("Запуск проекта C, не путать с разработкой")

def test_short_canonical_C_still_matches_explicit_alias():
    """`C lang` и `Си` остаются в aliases — должны fire."""
    assert "C" in extract_skills("Опыт работы с C lang и системным программированием")
    assert "C" in extract_skills("Знание Си обязательно")

def test_short_canonical_R_no_false_positive_on_RnD_and_SAP():
    """`R&D`, `SAP R/3` не должны fire как R-language. Через `deny_after`
    (session 32) — bare `r` в aliases recovered, но `r&` и `r/3` отброшены."""
    assert "R" not in extract_skills("Опыт работы в отделе R&D")
    assert "R" not in extract_skills("Выгрузки из SAP R/3 и BW")
    # Whitespace-tolerant: "R & D" (с пробелами) тоже denied
    assert "R" not in extract_skills("AI R & D team")

def test_short_canonical_R_still_matches_explicit_alias():
    """`r-lang` alias остаётся."""
    assert "R" in extract_skills("Опыт с r-lang для статистических расчётов")

def test_short_canonical_R_recovered_in_language_list():
    """Session 32 recovery: bare `r` снова matches в нормальном language list
    контексте (`python, r, sas`, `python/r`, etc.), но не в R&D/R/3."""
    assert "R" in extract_skills("Опыт: python, java, sas, R или аналоги")
    assert "R" in extract_skills("scipy или R (tidyverse, data.table)")
    assert "R" in extract_skills("python/R для статистики")
    assert "R" in extract_skills("Знание Python и R обязательно.")
    assert "R" in extract_skills("владение языком R")

def test_R_deny_after_does_not_break_general_slash():
    """`r/python` (НЕ `R/3`) — должен match (right context starts с
    `python`, не digit-3). Deny pattern `/3` precision-targeted."""
    skills = extract_skills("python/r/sas")
    assert "R" in skills
    assert "Python" in skills

def test_short_canonical_Go_still_matches_via_explicit_alias():
    """`go` явно в aliases — fire не задеваем."""
    assert "Go" in extract_skills("опыт на go и rust")
    assert "Go" in extract_skills("Golang разработчик")


# === Session 33: C recall recovery via explicit phrasings + deny_after ===

def test_C_recovered_in_C_slash_Cpp_context():
    """«знание C/C++» — самая частая форма (108 occurrences в slim). Должны
    matched оба: C через `c/c++` alias + C++ через `c++` alias."""
    skills = extract_skills("Требуется знание C/C++ и опыт системного программирования")
    assert "C" in skills
    assert "C++" in skills

def test_C_phrasings_match_bare_C():
    """Explicit context-anchored phrasings («знание c», «опыт c», «язык c»,
    «c-developer», «c-программист») recover C signal без bare-c."""
    assert "C" in extract_skills("знание C и Assembler обязательно")
    assert "C" in extract_skills("Опыт C от 3 лет")
    assert "C" in extract_skills("Знание: язык C, ассемблер")
    assert "C" in extract_skills("Ищем C-developer для embedded")
    assert "C" in extract_skills("Вакансия C-программист в Москву")
    assert "C" in extract_skills("Требуется опыт C-разработчика")

def test_C_deny_after_excludes_Cpp_and_Csharp():
    """«знание C++» не должно tag C (только C++). Аналогично для C#.
    Deny patterns ["+","#"] фильтруют right context после bare-C phrasing."""
    plus = extract_skills("знание C++ обязательно")
    assert "C" not in plus
    assert "C++" in plus

    sharp = extract_skills("опыт C# и .NET")
    assert "C" not in sharp
    assert "C#" in sharp

    lang = extract_skills("язык C++ предпочтительнее Java")
    assert "C" not in lang
    assert "C++" in lang

def test_C_no_false_positive_on_bare_c_in_RU_prose():
    """Bare `c` в нормальном RU-тексте (Cyrillic preposition с typo, заголовки,
    инициалы) НЕ должен fire — short-canonical guard стоит, deny_after не
    активируется (нет соседства с C++/C# pattern)."""
    assert "C" not in extract_skills("Работа с C-level executives ежедневно")
    assert "C" not in extract_skills("Запуск проекта C, не путать с разработкой")
    assert "C" not in extract_skills("компания c хорошей репутацией")

def test_R_deny_after_blocks_R_Keeper_product():
    """R-Keeper — российский POS-софт для ресторанов, не R-language.
    Session 34: deny_after расширен `-keeper` (для дефис-формы) и `keeper`
    (для пробельной формы — leading space stripped via lstrip перед сверкой)."""
    assert "R" not in extract_skills("Поддержка серверов R-Keeper и SH5")
    assert "R" not in extract_skills("системы R Keeper в ресторане")
    assert "R" not in extract_skills("обслуживание систем R-Keeper, 1С")

def test_R_deny_after_blocks_R_Style_company():
    """R-Style Softlab — российская IT-компания, не R-language.
    Session 34: deny_after расширен `-style`."""
    assert "R" not in extract_skills("Системный аналитик R-Style Softlab")
    assert "R" not in extract_skills("опыт работы в R-Style")

def test_R_deny_after_blocks_R_Vision_product():
    """R-Vision — ИБ vendor/product, не R-language."""
    assert "R" not in extract_skills("Бизнес-аналитик R-Vision")
    assert "R" not in extract_skills("SIEM R-Vision, EDR и DLP")

def test_R_deny_after_blocks_Day_R_Survival_title():
    """Day R Survival — game title, не R-language."""
    assert "R" not in extract_skills("проект Day R Survival превышает 30 млн установок")

def test_R_deny_after_blocks_email_localpart_initial():
    """email `name.r@domain` не должен fire bare R."""
    assert "R" not in extract_skills("send cv to pavithra.r@doodleblue.com")

def test_R_deny_after_blocks_letter_spaced_brand():
    """letter-spaced brand FABRICA не должен fire R-language."""
    assert "R" not in extract_skills("компания «F A B R I C A» производит мебель")

def test_R_deny_after_v2_preserves_real_signal():
    """Новые deny patterns не задевают реальный R-language signal."""
    assert "R" in extract_skills("Python или R для аналитики данных")
    assert "R" in extract_skills("владение языком R")
    assert "R" in extract_skills("scipy или R (tidyverse)")
    # R + не-keeper/style контекст
    assert "R" in extract_skills("R разработчик для статистики")

def test_R_markdown_split_words_do_not_match():
    """Markdown-emphasis inside ordinary words must not create bare R."""
    assert "R" not in extract_skills("Senior Full Stack Web3 Enginee**r")
    assert "R" not in extract_skills("AI product for r**eplacing sensitive data")
    assert "R" not in extract_skills("Delivery Manage**r / Hypercell Games")

def test_R_deny_after_blocks_observed_non_language_contexts():
    """Observed product/company/link contexts are not R-language mentions."""
    assert "R" not in extract_skills("R-Admin для удаленного доступа")
    assert "R" not in extract_skills("Оптимизации Dijkstra, R-tree и архитектура")
    assert "R" not in extract_skills("Level Designer (Unreal Engine) R-GAMES")
    assert "R" not in extract_skills("[R](https://kwork.ru/projects/3025989)eact для верстки")
    assert "R" not in extract_skills("retail, Ho R E C A под нашим брендом")

def test_R_fp_filters_preserve_real_signal():
    assert "R" in extract_skills("**R** developer для статистики")
    assert "R" in extract_skills("Python/R для статистики")
    assert "R" in extract_skills("R разработчик для статистики")

def test_C_alias_in_URL_slug_is_ignored():
    """URL-slugs типа `wantapply.com/backend-c-developer-at-nexters` НЕ должны
    fire `c-developer` alias. Session 33: extract_skills pre-strips http(s) URLs
    перед AC-сканом. C# Nexters tg-row был самым ярким FP (62/299 = 21%)."""
    text = (
        "Backend C# Developer / Nexters. Strong knowledge of .NET, C#, ASP.NET Core. "
        "Apply: https://wantapply.com/backend-c-developer-at-nexters and ping us."
    )
    skills = extract_skills(text)
    assert "C" not in skills
    assert "C#" in skills

def test_url_strip_does_not_block_real_signal_outside_URL():
    """URL-strip удаляет только URL-substring; alias в прозе сохраняется."""
    text = (
        "Опыт C/C++ обязательно. Ссылка на тестовое: https://example.com/c-developer-test."
    )
    skills = extract_skills(text)
    # alias `c/c++` в прозе — C tagged; URL-only c-developer не должно double-fire.
    assert "C" in skills
    assert "C++" in skills

def test_C_phrasing_in_skills_list_with_Cpp_neighbor():
    """«C, C++, Rust» — comma-separated list. Right context после bare-C
    phrasing — запятая, не `+`/`#` → C tagged + C++ tagged."""
    skills = extract_skills("Знание C, C++, Rust — обязательно")
    assert "C" in skills
    assert "C++" in skills
    assert "Rust" in skills

def test_computer_vision_does_not_match_resume_cv():
    """`CV` в TG часто значит resume, не Computer Vision."""
    assert "Computer Vision" not in extract_skills("#resume #cv #резюме")
    assert "Computer Vision" not in extract_skills("send cv to recruiter@example.com")

def test_computer_vision_matches_explicit_context():
    assert "Computer Vision" in extract_skills("Senior Computer Vision Engineer")
    assert "Computer Vision" in extract_skills("ML/CV Engineer")
