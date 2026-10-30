"""Country code mapping and lookup helpers."""

import pytest

from bot.utils.countries import (
    COUNTRIES,
    COUNTRY_CODES,
    _country_choices,
    resolve_country,
)


def test_every_country_has_a_unique_two_letter_code():
    assert len(COUNTRIES) == len(COUNTRY_CODES)
    assert all(len(code) == 2 and code.islower() for code in COUNTRY_CODES)


@pytest.mark.parametrize(
    "name, code",
    [
        ("Andorra", "ad"),
        ("Belgium", "be"),
        ("Croatia", "hr"),
        ("Botswana", "bw"),
        ("Colombia", "co"),
        ("Central African Republic", "cf"),
        ("Czech Republic", "cz"),
        ("Indonesia", "id"),
        ("Luxembourg", "lu"),
        ("Maldives", "mv"),
        ("Paraguay", "py"),
        ("Sierra Leone", "sl"),
        ("Antigua and Barbuda", "ag"),
        ("Denmark", "dk"),
    ],
)
def test_previously_duplicated_codes_are_correct(name, code):
    assert COUNTRIES[name] == code


def test_resolve_country_accepts_names_codes_and_case():
    assert resolve_country("Germany") == "de"
    assert resolve_country("germany") == "de"
    assert resolve_country("DE") == "de"
    assert resolve_country("  de  ") == "de"
    assert resolve_country("") is None
    assert resolve_country("Atlantis") is None


def test_country_choices_filter_by_name_and_code():
    by_name = _country_choices("Germ")
    assert any(c.value == "de" for c in by_name)
    assert all(len(c.name) <= 100 for c in by_name)

    by_code = _country_choices("de")
    assert any(c.name == "Germany" for c in by_code)
    assert len(_country_choices("")) <= 25


@pytest.mark.parametrize(
    "value, code",
    [
        ("Palestine", "ps"),
        ("Kosovo", "xk"),
        ("Ivory Coast", "ci"),
        ("South Sudan", "ss"),
        ("Eswatini", "sz"),
        ("Swaziland", "sz"),
        ("Macedonia", "mk"),
        ("DR Congo", "cd"),
    ],
)
def test_added_and_renamed_countries_resolve(value, code):
    assert resolve_country(value) == code


async def test_timezone_autocomplete_matches_country_names():
    from bot.cogs.scheduling.schedule_cog import timezone_autocomplete

    choices = await timezone_autocomplete(None, "Palestine")
    assert [c.value for c in choices] == ["Asia/Gaza", "Asia/Hebron"]
    assert choices[0].name == "Asia/Gaza (Palestine)"
    assert [c.value for c in await timezone_autocomplete(None, "kosovo")] == [
        "Europe/Belgrade"
    ]
    assert "America/New_York" in [
        c.value for c in await timezone_autocomplete(None, "new york")
    ]
