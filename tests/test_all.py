# tests/test_all.py
"""
Объединённый тестовый модуль для системы парсинга навыков hh.ru.
Покрывает все компоненты текущей Selenium-версии: конфигурация,
Selenium-парсер, извлечение навыков, хранилище, генератор отчётов.
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

# Добавляем корень проекта в путь
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import ParserConfig, load_skills

# Путь к базовому IT-словарю для тестов извлечения
_IT_DICT = str(Path(__file__).parent.parent / "dictionary" / "skills_python_dev.yaml")
from hh_core import (
    Vacancy,
    HHSeleniumScraper,
    SkillExtractor,
    Storage,
    VacancyModel,
    SkillModel,
    Base,
)
from hh_reports import ReportGenerator
from hh_pipeline import run_parsing, recalc_skills


# =============================================================================
# ТЕСТЫ КОНФИГУРАЦИИ (config.py)
# =============================================================================


class TestParserConfig:
    """Тесты модуля конфигурации"""

    def test_default_config(self):
        """Тест создания конфигурации с дефолтными значениями"""
        config = ParserConfig()

        # Числа-дефолты настраиваемы (правятся в config.py), поэтому
        # проверяем разумность, а не конкретные значения
        assert config.area_id >= 1
        assert config.max_pages >= 1
        assert config.request_delay >= 0
        assert config.db_path
        assert config.reports_dir
        assert (
            config.max_details_per_profession is None
            or config.max_details_per_profession >= 1
        )
        # Профессии и словари задаются только в dictionary/config.yaml или -p;
        # дефолты в config.py — пустые
        assert config.professions == []
        assert config.skills_files == {}
        assert config.parsing_hh is True

    def test_custom_config(self):
        """Тест создания конфигурации с пользовательскими значениями"""
        config = ParserConfig(
            professions=["Data Scientist"],
            area_id=2,
            max_pages=5,
            request_delay=1.0,
        )

        assert config.professions == ["Data Scientist"]
        assert config.area_id == 2
        assert config.max_pages == 5
        assert config.request_delay == 1.0

    def test_from_yaml(self):
        """Тест загрузки конфигурации из YAML"""
        yaml_content = """
professions:
  - "Go Developer"
  - "DevOps Engineer"
area_id: 2
max_pages: 10
request_delay: 0.3
db_path: "test.db"
reports_dir: "test_reports"
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            f.write(yaml_content)
            f.flush()

            config = ParserConfig.from_yaml(f.name)

            assert config.professions == ["Go Developer", "DevOps Engineer"]
            assert config.area_id == 2
            assert config.max_pages == 10
            assert config.db_path == "test.db"

        os.unlink(f.name)

    def test_to_yaml(self):
        """Тест сохранения конфигурации в YAML"""
        config = ParserConfig(professions=["Test Dev"])

        with tempfile.NamedTemporaryFile(
            suffix=".yaml", delete=False
        ) as f:
            config.to_yaml(f.name)

            # Загружаем обратно и проверяем
            loaded = ParserConfig.from_yaml(f.name)
            assert loaded.professions == ["Test Dev"]

        os.unlink(f.name)

    def test_ensure_dirs(self):
        """Тест создания директорий"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = ParserConfig(reports_dir=os.path.join(tmpdir, "new_reports"))
            config.ensure_dirs()

            assert Path(config.reports_dir).exists()

    def test_str_representation(self):
        """Тест строкового представления"""
        config = ParserConfig()
        str_repr = str(config)

        assert "ParserConfig" in str_repr
        assert "professions" in str_repr


# =============================================================================
# ТЕСТЫ SELENIUM-ПАРСЕРА (HHSeleniumScraper)
# =============================================================================


class TestHHSeleniumScraper:
    """Тесты Selenium-парсера (с мок-драйвером, без сети)"""

    def test_scraper_init(self):
        """Тест инициализации парсера"""
        config = ParserConfig()
        scraper = HHSeleniumScraper(config)

        assert scraper.config == config
        assert scraper.driver is None

    def test_parse_page_mock(self):
        """Разбор карточек выдачи: название, ссылка, организация"""
        config = ParserConfig(max_pages=1)
        scraper = HHSeleniumScraper(config)

        def make_card(href, title, employer):
            title_el = MagicMock()
            title_el.text = title
            title_el.get_attribute.return_value = href
            emp_el = MagicMock()
            emp_el.text = employer

            def find_element(by, selector):
                if "employer" in selector:
                    if employer is None:
                        raise Exception("нет организации")
                    return emp_el
                return title_el

            card = MagicMock()
            card.find_element.side_effect = find_element
            return card

        cards = [
            make_card("https://hh.ru/vacancy/123?query=1", "Python Developer", "ООО Ромашка"),
            make_card("https://hh.ru/vacancy/124", "Backend Dev", None),
        ]

        driver = MagicMock()
        driver.find_elements.return_value = cards
        scraper.driver = driver

        vacancies = scraper._parse_page()

        assert len(vacancies) == 2
        assert vacancies[0].id == "123"
        assert vacancies[0].name == "Python Developer"
        assert vacancies[0].employer == "ООО Ромашка"
        assert vacancies[0].url == "https://hh.ru/vacancy/123"
        assert vacancies[1].employer is None

    def test_parse_page_fallback_links(self):
        """Без карточек — запасной разбор по ссылкам на вакансии"""
        config = ParserConfig(max_pages=1)
        scraper = HHSeleniumScraper(config)

        link = MagicMock()
        link.text = "Python Developer"
        link.get_attribute.return_value = "https://hh.ru/vacancy/123?query=1"

        map_link = MagicMock()
        map_link.text = "Вакансии на карте"
        map_link.get_attribute.return_value = "https://hh.ru/search/vacancy/map?area=1"

        driver = MagicMock()

        def find_elements(by, selector):
            if "vacancy-serp__vacancy" in selector:
                return []  # карточек нет — старая вёрстка
            return [link, map_link]

        driver.find_elements.side_effect = find_elements
        scraper.driver = driver

        vacancies = scraper._parse_page()

        assert len(vacancies) == 1
        assert vacancies[0].id == "123"
        assert vacancies[0].name == "Python Developer"
        assert vacancies[0].url == "https://hh.ru/vacancy/123"

    def test_get_vacancy_details_mock(self):
        """Тест загрузки деталей вакансии (описание + ключевые навыки)"""
        config = ParserConfig(request_delay=0)
        scraper = HHSeleniumScraper(config)

        driver = MagicMock()
        desc_el = MagicMock()
        desc_el.text = "Требуется Python и Docker"
        driver.find_element.return_value = desc_el

        tag1, tag2 = MagicMock(), MagicMock()
        tag1.text, tag2.text = "Python", "Docker"
        driver.find_elements.return_value = [tag1, tag2]
        scraper.driver = driver

        vacancy = Vacancy(
            id="1",
            name="Dev",
            description="",
            skills=[],
            url="https://hh.ru/vacancy/1",
        )
        result = scraper.get_vacancy_details(vacancy)

        assert result.description == "Требуется Python и Docker"
        assert "Python" in result.skills
        assert "Docker" in result.skills
        driver.get.assert_called_once_with("https://hh.ru/vacancy/1")

    def test_get_vacancy_details_no_url(self):
        """Без URL детали не загружаются"""
        scraper = HHSeleniumScraper(ParserConfig())
        vacancy = Vacancy(id="1", name="Dev", description="", skills=[])
        result = scraper.get_vacancy_details(vacancy)

        assert result.description == ""
        assert result.skills == []


# =============================================================================
# ТЕСТЫ ИЗВЛЕЧЕНИЯ НАВЫКОВ (SkillExtractor)
# =============================================================================


class TestSkillExtractor:
    """Тесты извлечения навыков"""

    def test_clean_html(self):
        """Тест очистки HTML"""
        extractor = SkillExtractor()

        html_text = "<p>Требуется <b>Python</b> и <i>SQL</i></p>"
        cleaned = extractor.clean_html(html_text)

        assert "<" not in cleaned
        assert "python" in cleaned
        assert "sql" in cleaned

    def test_clean_html_entities(self):
        """Тест очистки HTML-сущностей"""
        extractor = SkillExtractor()

        text = "Опыт работы с &quot;Docker&quot; и &amp; Kubernetes"
        cleaned = extractor.clean_html(text)

        assert "docker" in cleaned
        assert "kubernetes" in cleaned

    def test_extract_from_description(self):
        """Тест извлечения навыков из описания"""
        extractor = SkillExtractor(load_skills(_IT_DICT))

        description = """
        <p>Мы ищем разработчика со знанием:</p>
        <ul>
            <li>Python и Django</li>
            <li>PostgreSQL и Redis</li>
            <li>Docker и Kubernetes</li>
            <li>Git и CI/CD</li>
        </ul>
        """

        skills = extractor.extract_from_description(description)

        assert "Python" in skills
        assert "Django" in skills
        assert "PostgreSQL" in skills
        assert "Redis" in skills
        assert "Docker" in skills
        assert "Kubernetes" in skills
        assert "Git" in skills
        assert "CI/CD" in skills

    def test_extract_from_description_no_false_positives(self):
        """Тест отсутствия ложных срабатываний"""
        extractor = SkillExtractor(load_skills(_IT_DICT))

        # "go" не должно срабатывать на "going" или "something"
        description = "We are going to build something amazing"
        skills = extractor.extract_from_description(description)

        # "go" не должно быть найдено (оно внутри "going")
        assert "Go" not in skills

    def test_extract_from_key_skills(self):
        """Тест нормализации ключевых навыков"""
        extractor = SkillExtractor()

        key_skills = ["Python", "SQL", "", "  ", "Docker"]
        skills = extractor.extract_from_key_skills(key_skills)

        assert "Python" in skills
        assert "SQL" in skills
        assert "Docker" in skills
        assert "" not in skills
        assert "  " not in skills

    def test_extract_all(self):
        """Тест полного извлечения из вакансии"""
        extractor = SkillExtractor(load_skills(_IT_DICT))

        vacancy = Vacancy(
            id="1",
            name="Python Developer",
            description="<p>Нужен Python, Django, PostgreSQL</p>",
            skills=["Docker", "Kubernetes"],
        )

        skills = extractor.extract_all(vacancy)

        # Должны быть и из key_skills, и из описания
        assert "Docker" in skills
        assert "Kubernetes" in skills
        assert "Python" in skills
        assert "Django" in skills
        assert "PostgreSQL" in skills

    def test_aggregate_skills(self):
        """Тест агрегации навыков"""
        extractor = SkillExtractor(load_skills(_IT_DICT))

        vacancies = [
            Vacancy(
                id="1",
                name="Dev 1",
                description="<p>Python SQL Docker</p>",
                skills=["Python", "SQL"],
            ),
            Vacancy(
                id="2",
                name="Dev 2",
                description="<p>Python Django Redis</p>",
                skills=["Python", "Docker"],
            ),
            Vacancy(
                id="3",
                name="Dev 3",
                description="<p>Python JavaScript React</p>",
                skills=["SQL"],
            ),
        ]

        report = extractor.aggregate_skills(vacancies)

        # Python должен быть на первом месте (3 упоминания)
        assert report[0]["Навык"] == "Python"
        assert report[0]["Упоминаний"] == 3

        # SQL — 2 упоминания
        sql_entry = next(r for r in report if r["Навык"] == "SQL")
        assert sql_entry["Упоминаний"] == 2

        # Docker — 2 упоминания
        docker_entry = next(r for r in report if r["Навык"] == "Docker")
        assert docker_entry["Упоминаний"] == 2

    def test_aggregate_skills_sorted_desc(self):
        """Тест сортировки по убыванию"""
        extractor = SkillExtractor(load_skills(_IT_DICT))

        vacancies = [
            Vacancy(id="1", name="V1", description="<p>Python</p>", skills=["Python", "SQL"]),
            Vacancy(id="2", name="V2", description="<p>Python</p>", skills=["Python"]),
            Vacancy(id="3", name="V3", description="<p>SQL Docker</p>", skills=["SQL", "Docker"]),
        ]

        report = extractor.aggregate_skills(vacancies)

        # Проверяем что отсортировано по убыванию
        counts = [r["Упоминаний"] for r in report]
        assert counts == sorted(counts, reverse=True)

    def test_aggregate_skills_empty(self):
        """Тест агрегации при пустом списке вакансий"""
        extractor = SkillExtractor()
        report = extractor.aggregate_skills([])
        assert report == []

    def test_custom_skills(self):
        """Тест добавления пользовательских навыков"""
        custom = {"myframework": "MyFramework", "customdb": "CustomDB"}
        extractor = SkillExtractor(custom)

        description = "Требуется знание myframework и customdb"
        skills = extractor.extract_from_description(description)

        assert "MyFramework" in skills
        assert "CustomDB" in skills

    def test_percent_calculation(self):
        """Тест расчёта процента вакансий"""
        extractor = SkillExtractor()

        vacancies = [
            Vacancy(id="1", name="V1", description="", skills=["Python"]),
            Vacancy(id="2", name="V2", description="", skills=["Python"]),
            Vacancy(id="3", name="V3", description="", skills=["SQL"]),
            Vacancy(id="4", name="V4", description="", skills=["Docker"]),
        ]

        report = extractor.aggregate_skills(vacancies)

        python_entry = next(r for r in report if r["Навык"] == "Python")
        assert python_entry["% вакансий"] == 50.0  # 2 из 4

        sql_entry = next(r for r in report if r["Навык"] == "SQL")
        assert sql_entry["% вакансий"] == 25.0  # 1 из 4


# =============================================================================
# ТЕСТЫ ХРАНИЛИЩА (Storage)
# =============================================================================


class TestStorage:
    """Тесты хранилища данных"""

    @pytest.fixture
    def temp_storage(self):
        """Создание временного хранилища"""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_url = f"sqlite:///{f.name}"
            storage = Storage(db_url)
            yield storage
            os.unlink(f.name)

    def test_storage_init(self, temp_storage):
        """Тест инициализации хранилища"""
        assert temp_storage.engine is not None

    def test_save_vacancies(self, temp_storage):
        """Тест сохранения вакансий"""
        vacancies = [
            Vacancy(
                id="1",
                name="Python Dev",
                description="Python Django",
                skills=["Python", "Django"],
                employer="Test Corp",
            ),
            Vacancy(
                id="2",
                name="Data Engineer",
                description="SQL Spark",
                skills=["SQL", "Spark"],
                employer="Data Inc",
            ),
        ]

        saved = temp_storage.save_vacancies(vacancies)
        assert saved == 2

    def test_save_vacancies_no_duplicates(self, temp_storage):
        """Тест что дубликаты не сохраняются"""
        vacancy = Vacancy(
            id="1",
            name="Python Dev",
            description="Python",
            skills=["Python"],
        )

        temp_storage.save_vacancies([vacancy])
        saved = temp_storage.save_vacancies([vacancy])

        assert saved == 0  # Дубликат не сохранён
        assert temp_storage.get_vacancies_count() == 1

    def test_save_vacancies_employer_and_published_at(self, temp_storage):
        """Организация и дата публикации сохраняются; дубликаты дополняются"""
        from sqlalchemy.orm import Session

        temp_storage.save_vacancies([
            Vacancy(
                id="1", name="V", description="", skills=[],
                employer="ООО Ромашка", published_at="30 октября 2026",
            ),
        ])
        with Session(temp_storage.engine) as session:
            vm = session.get(VacancyModel, "1")
            assert vm.employer == "ООО Ромашка"
            assert vm.published_at == "30 октября 2026"

        # Дубликат без организации не затирает существующую
        temp_storage.save_vacancies([
            Vacancy(id="1", name="V", description="", skills=[]),
        ])
        with Session(temp_storage.engine) as session:
            vm = session.get(VacancyModel, "1")
            assert vm.employer == "ООО Ромашка"

        # Дубликат с организацией заполняет пустое поле
        temp_storage.save_vacancies([
            Vacancy(id="2", name="V2", description="", skills=[]),
        ])
        temp_storage.save_vacancies([
            Vacancy(
                id="2", name="V2", description="", skills=[],
                employer="ООО Лютик", published_at="1 ноября 2026",
            ),
        ])
        with Session(temp_storage.engine) as session:
            vm = session.get(VacancyModel, "2")
            assert vm.employer == "ООО Лютик"
            assert vm.published_at == "1 ноября 2026"

    def test_migrate_adds_published_at(self, tmp_path):
        """Миграция добавляет published_at в старую БД"""
        import sqlite3

        db = tmp_path / "old.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE vacancies ("
            "id VARCHAR NOT NULL, name VARCHAR NOT NULL, description TEXT, "
            "employer VARCHAR, area VARCHAR, url VARCHAR, parsed_at DATETIME, "
            "PRIMARY KEY (id))"
        )
        conn.commit()
        conn.close()

        storage = Storage(f"sqlite:///{db}")
        with storage.engine.begin() as conn:
            cols = {
                row[1] for row in conn.exec_driver_sql(
                    "PRAGMA table_info(vacancies)"
                ).fetchall()
            }
        assert {"published_at", "salary_min", "salary_max", "profession"} <= cols

    def test_get_skills_report(self, temp_storage):
        """Тест получения отчёта из БД"""
        vacancies = [
            Vacancy(id="1", name="V1", description="", skills=["Python", "SQL"]),
            Vacancy(id="2", name="V2", description="", skills=["Python", "Docker"]),
            Vacancy(id="3", name="V3", description="", skills=["SQL"]),
        ]

        temp_storage.save_vacancies(vacancies)
        report = temp_storage.get_skills_report()

        assert len(report) > 0
        # Python должен быть первым (2 упоминания)
        assert report[0]["Навык"] == "Python"
        assert report[0]["Упоминаний"] == 2

    def test_get_vacancies_count(self, temp_storage):
        """Тест подсчёта вакансий"""
        assert temp_storage.get_vacancies_count() == 0

        vacancies = [
            Vacancy(id="1", name="V1", description="", skills=[]),
            Vacancy(id="2", name="V2", description="", skills=[]),
        ]
        temp_storage.save_vacancies(vacancies)

        assert temp_storage.get_vacancies_count() == 2


# =============================================================================
# ТЕСТЫ ГЕНЕРАТОРА ОТЧЁТОВ (ReportGenerator)
# =============================================================================


class TestReportGenerator:
    """Тесты генератора отчётов"""

    @pytest.fixture
    def sample_data(self):
        return [
            {"Навык": "Python", "Упоминаний": 100, "% вакансий": 80.0},
            {"Навык": "SQL", "Упоминаний": 75, "% вакансий": 60.0},
            {"Навык": "Docker", "Упоминаний": 50, "% вакансий": 40.0},
            {"Навык": "Git", "Упоминаний": 45, "% вакансий": 36.0},
        ]

    def test_report_init(self, sample_data):
        """Тест инициализации генератора"""
        reporter = ReportGenerator(sample_data)
        assert len(reporter.df) == 4

    def test_to_console(self, sample_data, capsys):
        """Тест вывода в консоль"""
        reporter = ReportGenerator(sample_data)
        reporter.to_console()

        captured = capsys.readouterr()
        assert "Python" in captured.out
        assert "SQL" in captured.out
        assert "ОТЧЁТ ПО НАВЫКАМ" in captured.out

    def test_to_excel(self, sample_data):
        """Тест экспорта в Excel"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test_report.xlsx")
            reporter = ReportGenerator(sample_data)
            reporter.to_excel(path)

            assert Path(path).exists()

            # Проверяем содержимое
            df = pd.read_excel(path)
            assert len(df) == 4
            assert "Навык" in df.columns

    def test_to_csv(self, sample_data):
        """Тест экспорта в CSV"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test_report.csv")
            reporter = ReportGenerator(sample_data)
            reporter.to_csv(path)

            assert Path(path).exists()

            df = pd.read_csv(path)
            assert len(df) == 4

    def test_to_chart(self, sample_data):
        """Тест генерации графика"""
        import matplotlib
        matplotlib.use("Agg")  # Non-interactive backend

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test_chart.png")
            reporter = ReportGenerator(sample_data)
            reporter.to_chart(path, top_n=3)

            assert Path(path).exists()

    def test_empty_report(self):
        """Тест отчёта с пустыми данными"""
        reporter = ReportGenerator([])
        assert len(reporter.df) == 0

        # Не должно падать
        reporter.to_console()


# =============================================================================
# ИНТЕГРАЦИОННЫЕ ТЕСТЫ
# =============================================================================


class TestIntegration:
    """Интеграционные тесты"""

    def test_full_pipeline_mock(self):
        """Тест полного пайплайна с мок-данными"""
        # Имитируем собранные вакансии
        vacancies = [
            Vacancy(
                id="1",
                name="Python Developer",
                description="<p>Требуется Python, Django, PostgreSQL, Docker</p>",
                skills=["Python", "Django", "PostgreSQL", "Docker"],
                employer="Tech Corp",
                area="Москва",
            ),
            Vacancy(
                id="2",
                name="Senior Python Developer",
                description="<p>Нужен опыт с Python, FastAPI, Redis, Kubernetes</p>",
                skills=["Python", "FastAPI", "Redis", "Kubernetes"],
                employer="Big Tech",
                area="Москва",
            ),
            Vacancy(
                id="3",
                name="Backend Developer",
                description="<p>Ищем разработчика: Python, SQL, Git, Linux</p>",
                skills=["Python", "SQL", "Git", "Linux"],
                employer="Startup Inc",
                area="Санкт-Петербург",
            ),
        ]

        # Извлечение навыков
        extractor = SkillExtractor()
        report = extractor.aggregate_skills(vacancies)

        # Проверки
        assert len(report) > 0

        # Python должен быть самым частым (3 из 3)
        assert report[0]["Навык"] == "Python"
        assert report[0]["Упоминаний"] == 3
        assert report[0]["% вакансий"] == 100.0

        # Отсортировано по убыванию
        counts = [r["Упоминаний"] for r in report]
        assert counts == sorted(counts, reverse=True)

        # Генерация отчёта
        with tempfile.TemporaryDirectory() as tmpdir:
            reporter = ReportGenerator(report)

            # Excel
            excel_path = os.path.join(tmpdir, "report.xlsx")
            reporter.to_excel(excel_path)
            assert Path(excel_path).exists()

            # CSV
            csv_path = os.path.join(tmpdir, "report.csv")
            reporter.to_csv(csv_path)
            assert Path(csv_path).exists()

    def test_storage_and_report_integration(self):
        """Тест интеграции хранилища и отчётов"""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_url = f"sqlite:///{f.name}"
            storage = Storage(db_url)

            vacancies = [
                Vacancy(id="1", name="V1", description="", skills=["Python", "SQL"]),
                Vacancy(id="2", name="V2", description="", skills=["Python", "Docker"]),
            ]

            storage.save_vacancies(vacancies)
            report = storage.get_skills_report()

            assert len(report) > 0
            assert report[0]["Навык"] == "Python"

            os.unlink(f.name)


# =============================================================================
# ТЕСТЫ ОРКЕСТРАЦИИ (run_parsing)
# =============================================================================


class TestRunParsing:
    """Тесты run_parsing с мок-скрапером и мок-хранилищем"""

    def _install_fakes(self, monkeypatch, ids_by_profession):
        """Подмена Selenium-скрапера и хранилища; возвращает счётчик деталей"""
        import matplotlib

        matplotlib.use("Agg")
        import hh_pipeline as hp

        calls = {"details": 0}

        class FakeScraper:
            def __init__(self, config, headless=True):
                self.config = config

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def search_vacancies(self, profession):
                return [
                    Vacancy(
                        id=vid,
                        name=f"V {vid}",
                        description="",
                        skills=[],
                        url=f"https://hh.ru/vacancy/{vid}",
                    )
                    for vid in ids_by_profession[profession]
                ]

            def get_vacancy_details(self, vacancy):
                calls["details"] += 1
                vacancy.description = "<p>Python и Docker</p>"
                return vacancy

        class FakeStorage:
            def __init__(self, db_url):
                pass

            def save_vacancies(self, vacancies):
                return len(vacancies)

            def get_salary_range(self, profession):
                return None, None

            def get_skills_report(self):
                return [{"Навык": "Python", "Упоминаний": 2, "% вакансий": 100.0}]

        monkeypatch.setattr(hp, "HHSeleniumScraper", FakeScraper)
        monkeypatch.setattr(hp, "Storage", FakeStorage)
        return calls

    def _make_config(self, tmp_path, **kwargs):
        defaults = dict(
            db_path=str(tmp_path / "t.db"),
            reports_dir=str(tmp_path / "reports"),
            report_excel=str(tmp_path / "skills_report.xlsx"),
            report_csv=str(tmp_path / "skills_report.csv"),
            report_chart=str(tmp_path / "skills_chart.png"),
        )
        defaults.update(kwargs)
        return ParserConfig(**defaults)

    def test_run_parsing_without_professions(self, tmp_path, caplog):
        """Без профессий парсинг не запускается, ошибка в логе"""
        config = self._make_config(tmp_path)  # professions=[] по умолчанию
        assert config.professions == []
        run_parsing(config)
        assert "Профессии не заданы" in caplog.text

    def test_max_details_cap(self, monkeypatch, tmp_path):
        """Лимит max_details_per_profession ограничивает загрузку деталей"""
        calls = self._install_fakes(
            monkeypatch,
            {"A": ["a1", "a2", "a3", "a4", "a5"], "B": ["b1", "b2", "b3", "b4", "b5"]},
        )
        config = self._make_config(
            tmp_path, professions=["A", "B"], max_details_per_profession=3
        )
        run_parsing(config)

        # 2 профессии × лимит 3 = 6 загрузок деталей
        assert calls["details"] == 6

        # PDF-отчёт по профессиям создан (имя YYYY-MM-DD-hh-parser.pdf)
        pdf = tmp_path / "reports" / f"{datetime.now():%Y-%m-%d}-hh-parser.pdf"
        assert pdf.exists()

    def test_duplicates_not_refetched(self, monkeypatch, tmp_path):
        """Дубликаты из разных запросов не обрабатываются повторно"""
        calls = self._install_fakes(
            monkeypatch,
            {"A": ["v1", "v2", "v3"], "B": ["v1", "v2", "v3"]},
        )
        config = self._make_config(tmp_path, professions=["A", "B"])
        run_parsing(config)

        # v1..v3 детализированы один раз, вторая профессия пропущена
        assert calls["details"] == 3


# =============================================================================
# ТЕСТЫ PDF-ОТЧЁТА (PdfReportGenerator)
# =============================================================================


class TestPdfReportGenerator:
    """Тесты многостраничного PDF-отчёта"""

    def _sections(self):
        return [
            {
                "profession": "Python Developer",
                "report": [
                    {"Навык": "Python", "Упоминаний": 10, "% вакансий": 100.0},
                    {"Навык": "SQL", "Упоминаний": 5, "% вакансий": 50.0},
                    {"Навык": "Docker", "Упоминаний": 3, "% вакансий": 30.0},
                ],
                "vacancies": 10,
            },
            {
                "profession": "Data Engineer",
                "report": [],
                "vacancies": 0,
            },
        ]

    def test_title_page_region(self):
        """Титул содержит регион; известные id — по имени, остальные — номером"""
        import matplotlib

        matplotlib.use("Agg")

        from hh_reports import PdfReportGenerator, _area_name

        assert _area_name(1) == "Москва"
        assert _area_name(2019) == "Московская область"
        assert _area_name(2090) == "г. Королев"
        assert _area_name(113) == "Россия"
        assert _area_name(999) == "регион hh.ru №999"
        assert _area_name(None) is None

        gen = PdfReportGenerator([], datetime.now(), area_id=2)
        fig = gen._title_page()
        texts = [t.get_text() for t in fig.texts]
        assert any("Регион: Санкт-Петербург" in t for t in texts)

    def test_vacancies_page_lines(self, tmp_path):
        """Страница вакансий: строка на вакансию, ссылки полные и кликабельные"""
        import matplotlib

        matplotlib.use("Agg")

        import pikepdf

        from hh_reports import PdfReportGenerator

        vacancies = [
            Vacancy(
                id=str(i),
                name=f"Вакансия {i}",
                description="",
                skills=[],
                employer=f"Компания {i}",
                url=f"https://korolev.hh.ru/vacancy/{1000 + i}",
                published_at="Вакансия опубликована 30 октября",
            )
            for i in range(1, 31)
        ]
        section = {
            "profession": "Тест",
            "report": [{"Навык": "Python", "Упоминаний": 5, "% вакансий": 100.0}],
            "vacancies": 30,
            "vacancies_list": vacancies,
        }
        gen = PdfReportGenerator([section], datetime.now())
        path = tmp_path / "vac.pdf"
        gen.generate(str(path))

        data = path.read_bytes()
        assert data[:5] == b"%PDF-"

        with pikepdf.open(str(path)) as pdf:
            assert len(pdf.pages) == 4  # титул + график + таблица + вакансии
            page = pdf.pages[3]
            annots = page.get("/Annots")
            assert annots is not None and len(annots) == 30
            uri = str(annots[0].A.URI)
            assert uri == "https://hh.ru/vacancy/1001"

    def test_vacancies_pages_continuation(self, tmp_path):
        """Не более 50 вакансий; длинные названия укорачиваются до 50 символов;
        строки не помещаются на лист — продолжение на новой странице без
        заголовка; дата в формате дд.мм.гггг"""
        import matplotlib

        matplotlib.use("Agg")

        import pikepdf

        from hh_reports import PdfReportGenerator

        long_name = ("Очень длинное название вакансии номер {i} с уточнением "
                     "специализации, подразделения и уровня грейда")
        long_emp = "Очень длинное название организации номер {i}"
        vacancies = [
            Vacancy(
                id=str(i),
                name=long_name.format(i=i),
                description="",
                skills=[],
                employer=long_emp.format(i=i),
                url=f"https://hh.ru/vacancy/{2000 + i}",
                published_at="30 октября",
                parsed_at=datetime(2026, 10, 30),
            )
            for i in range(1, 61)
        ]
        section = {
            "profession": "Тест",
            "report": [{"Навык": "Python", "Упоминаний": 5, "% вакансий": 100.0}],
            "vacancies": 60,
            "vacancies_list": vacancies,
        }
        gen = PdfReportGenerator([section], datetime.now())
        path = tmp_path / "vac2.pdf"
        gen.generate(str(path))

        with pikepdf.open(str(path)) as pdf:
            assert len(pdf.pages) >= 4  # титул + график + таблица + страницы ссылок
            uris = []
            for page in pdf.pages[3:]:
                annots = page.get("/Annots")
                if annots is not None:
                    uris.extend(str(a.A.URI) for a in annots)
            assert len(uris) == 50  # не более 50 последних вакансий
            assert len(set(uris)) == 50
            assert f"https://hh.ru/vacancy/{2050}" in uris
            assert f"https://hh.ru/vacancy/{2060}" not in uris

        # Строка с самой длинной ссылкой не вылезает за правый край листа
        for page_no, bbox, dpi, url in gen._page_links:
            assert bbox.x1 * 72 / dpi <= PdfReportGenerator.PAGE_SIZE[0] * 72 * 0.99

        figures = gen._vacancies_pages(section)
        texts = "\n".join(t.get_text() for fig in figures for t in fig.texts)
        # Длинные названия укорочены до 50 символов с многоточием
        from hh_reports import _shorten
        assert _shorten(long_emp.format(i=1), 50) in texts
        assert _shorten(long_name.format(i=1), 50) in texts
        assert long_name.format(i=1) not in texts
        assert "30.10.2026" in texts

        # На страницах продолжения нет заголовка профессии
        for fig in figures[1:]:
            headers = [t for t in fig.texts if t.get_fontsize() == 17]
            assert not headers

    def test_vacancies_page_salary_in_header(self):
        """Зарплата — в строке вакансии (после названия, перед ссылкой), без неё — пусто;
        в заголовках со 2 страницы зарплаты нет"""
        import matplotlib

        matplotlib.use("Agg")

        from hh_reports import PdfReportGenerator

        def make(vid, salary_min, salary_max):
            return Vacancy(
                id=str(vid), name=f"Вакансия {vid}", description="", skills=[],
                employer="ООО Ромашка", url=f"https://hh.ru/vacancy/{vid}",
                published_at="30 октября 2026",
                salary_min=salary_min, salary_max=salary_max,
            )

        vacancies = [make(1, 100000, 150000), make(2, None, None), make(3, 80000, None)]
        section = {
            "profession": "Системный администратор",
            "report": [{"Навык": "Python", "Упоминаний": 1, "% вакансий": 100.0}],
            "vacancies": 3,
            "vacancies_list": vacancies,
            "salary_min": 80000,
            "salary_max": 150000,
        }
        gen = PdfReportGenerator([section], datetime.now())
        fig = gen._vacancies_pages(section)[0]
        title = fig.texts[0].get_text()
        assert title == "Системный администратор, вакансии"  # зарплаты в заголовке нет
        texts = [t.get_text() for t in fig.texts]
        assert any("от 100 000 до 150 000 ₽," in t for t in texts)
        assert any(", ," in t for t in texts)  # зарплата не указана — пустое место
        assert any("от 80 000 ₽," in t for t in texts)

    def test_vacancy_helpers(self):
        from hh_reports import _shorten, _vacancy_date, _vacancy_url_full

        v = Vacancy(
            id="1", name="V", description="", skills=[],
            url="https://korolev.hh.ru/vacancy/123456",
            published_at="Вакансия опубликована 30 октября",
            parsed_at=datetime(2026, 10, 30),
        )
        assert _vacancy_date(v) == "30.10.2026"
        assert _vacancy_url_full(v) == "https://hh.ru/vacancy/123456"
        v2 = Vacancy(id="2", name="V2", description="", skills=[], parsed_at=datetime(2026, 11, 1))
        assert _vacancy_date(v2) == "01.11.2026"
        assert _vacancy_url_full(v2) == ""
        assert _shorten("0123456789", 6) == "01234…"
        assert _shorten(" короткая ", 20) == "короткая"

    def test_generate_pdf(self, tmp_path):
        """PDF создаётся с корректным заголовком и не пустой"""
        import matplotlib

        matplotlib.use("Agg")

        from hh_reports import PdfReportGenerator

        path = tmp_path / "2026-11-01-hh-parser.pdf"
        PdfReportGenerator(
            self._sections(), datetime(2026, 11, 1, 12, 30)
        ).generate(str(path))

        data = path.read_bytes()
        assert data[:5] == b"%PDF-"
        assert len(data) > 2000

        # титул + график + таблица (1-я профессия) + страница «нет данных» (2-я);
        # /Type /Pages — корневой узел дерева, не страница
        assert data.count(b"/Type /Page") - data.count(b"/Type /Pages") == 4

    def test_single_profession(self, tmp_path):
        """Одна профессия — тоже работает (титул + страница профессии)"""
        import matplotlib

        matplotlib.use("Agg")

        from hh_reports import PdfReportGenerator

        path = tmp_path / "report.pdf"
        PdfReportGenerator(self._sections()[:1], datetime.now()).generate(str(path))

        assert path.read_bytes()[:5] == b"%PDF-"


# =============================================================================
# ТЕСТЫ ЗАРПЛАТ И СЛОВАРЕЙ
# =============================================================================


class TestParseSalary:
    """Тесты разбора строки зарплаты"""

    def test_range(self):
        from hh_core import _parse_salary
        assert _parse_salary("от 100 000 до 150 000 ₽") == (100000, 150000)

    def test_from_only(self):
        from hh_core import _parse_salary
        assert _parse_salary("от 80 000 руб.") == (80000, None)

    def test_to_only(self):
        from hh_core import _parse_salary
        assert _parse_salary("до 200 000 ₽") == (None, 200000)

    def test_not_specified(self):
        from hh_core import _parse_salary
        assert _parse_salary("зарплата не указана") == (None, None)
        assert _parse_salary("") == (None, None)

    def test_dash_range(self):
        from hh_core import _parse_salary
        assert _parse_salary("200 000 – 250 000 руб.") == (200000, 250000)

    def test_from_with_ndfl(self):
        from hh_core import _parse_salary
        assert _parse_salary("от 100 000 руб. до вычета НДФЛ") == (100000, None)

    def test_domain_dictionaries(self):
        """Все доменные словари из dictionary/ читаются и не пустые"""
        base = Path(__file__).parent.parent / "dictionary"
        for name, min_keys in (
            ("skills_python_dev.yaml", 70),
            ("skills_accounting.yaml", 90),
            ("skills_neural.yaml", 15),
            ("skills_ai_engineer.yaml", 25),
            ("skills_sysadmin.yaml", 50),
            ("skills_techwriter.yaml", 15),
            ("skills_content_manager.yaml", 25),
        ):
            skills = load_skills(str(base / name))
            assert len(skills) >= min_keys, f"{name}: {len(skills)} < {min_keys}"

    def test_default_config_file(self):
        """dictionary/config.yaml существует, читается; у каждой профессии есть словарь"""
        from config import ParserConfig

        path = Path(__file__).parent.parent / "dictionary" / "config.yaml"
        config = ParserConfig.from_yaml(str(path))
        assert config.professions  # список профессий не пустой
        for profession in config.professions:
            assert profession in config.skills_files, profession
        assert "Технический писатель" in config.skills_files
        assert "Контент менеджер" in config.skills_files

    def test_default_skills_files(self):
        """Дефолтов словарей в config.py нет — источник dictionary/config.yaml"""
        config = ParserConfig()
        assert config.professions == []
        assert config.skills_files == {}

    def test_sysadmin_dictionary(self):
        from config import load_skills
        yaml_path = Path(__file__).parent.parent / "dictionary" / "skills_sysadmin.yaml"
        skills = load_skills(str(yaml_path))
        assert len(skills) >= 40
        assert skills["active directory"] == "Active Directory"


# =============================================================================
# ТЕСТЫ ХРАНЕНИЯ ЗАРПЛАТ И МИГРАЦИИ
# =============================================================================


class TestSalaryStorage:
    """Тесты хранения зарплат, профессии и миграции схемы"""

    def test_migration_adds_columns(self, tmp_path):
        """Storage добавляет новые столбцы в старую БД"""
        import sqlite3

        db = tmp_path / "old.db"
        con = sqlite3.connect(db)
        con.execute(
            "CREATE TABLE vacancies (id VARCHAR PRIMARY KEY, name VARCHAR NOT NULL,"
            " description TEXT, employer VARCHAR, area VARCHAR, url VARCHAR,"
            " parsed_at DATETIME)"
        )
        con.commit()
        con.close()

        Storage(f"sqlite:///{db}")
        cols = {
            row[1]
            for row in sqlite3.connect(db).execute("PRAGMA table_info(vacancies)")
        }
        assert {"salary_min", "salary_max", "profession"} <= cols

    def test_save_salary_and_profession(self, tmp_path):
        db = tmp_path / "t.db"
        storage = Storage(f"sqlite:///{db}")
        storage.save_vacancies([
            Vacancy(id="1", name="V", description="", skills=[],
                    profession="Бухгалтер", salary_min=80000, salary_max=120000),
        ])
        assert storage.get_salary_range("Бухгалтер") == (80000, 120000)

    def test_duplicate_updates_salary(self, tmp_path):
        """Дубликат дополняется зарплатой, количество вакансий не растёт"""
        db = tmp_path / "t.db"
        storage = Storage(f"sqlite:///{db}")
        storage.save_vacancies([
            Vacancy(id="1", name="V", description="", skills=[],
                    profession="Нейросети", salary_min=100000),
        ])
        storage.save_vacancies([
            Vacancy(id="1", name="V", description="", skills=[],
                    profession="Нейросети", salary_max=200000),
        ])
        assert storage.get_salary_range("Нейросети") == (100000, 200000)
        assert storage.get_vacancies_count() == 1

    def test_range_empty(self, tmp_path):
        db = tmp_path / "t.db"
        storage = Storage(f"sqlite:///{db}")
        assert storage.get_salary_range("Нет такой") == (None, None)


# =============================================================================
# ТЕСТЫ ПОДКЛЮЧАЕМОГО СЛОВАРЯ (skills_file)
# =============================================================================


class TestSkillsFile:
    """Тесты загрузки и подключения словаря навыков"""

    def test_load_skills_flat(self, tmp_path):
        from config import load_skills

        f = tmp_path / "skills.yaml"
        f.write_text('"1с": "1С"\n"мсфо": "МСФО"\n', encoding="utf-8")
        assert load_skills(str(f)) == {"1с": "1С", "мсфо": "МСФО"}

    def test_load_skills_nested(self, tmp_path):
        from config import load_skills

        f = tmp_path / "skills.yaml"
        f.write_text(
            'Учёт:\n  "1с": "1С"\nИнструменты:\n  "excel": "Excel"\n',
            encoding="utf-8",
        )
        assert load_skills(str(f)) == {"1с": "1С", "excel": "Excel"}

    def test_skills_files_mapping_roundtrip(self, tmp_path):
        from config import ParserConfig

        config = ParserConfig(skills_files={"Бухгалтер": "a.yaml", "Нейросети": "b.yaml"})
        f = tmp_path / "c.yaml"
        config.to_yaml(str(f))
        loaded = ParserConfig.from_yaml(str(f))
        assert loaded.skills_files == {"Бухгалтер": "a.yaml", "Нейросети": "b.yaml"}

    def test_domain_dictionaries(self):
        """Все доменные словари из dictionary/ читаются и не пустые"""
        from config import load_skills

        base = Path(__file__).parent.parent / "dictionary"
        for name, min_keys in (
            ("skills_python_dev.yaml", 70),
            ("skills_accounting.yaml", 90),
            ("skills_neural.yaml", 15),
            ("skills_ai_engineer.yaml", 25),
            ("skills_sysadmin.yaml", 50),
        ):
            skills = load_skills(str(base / name))
            assert len(skills) >= min_keys, f"{name}: {len(skills)} < {min_keys}"

    def test_extractor_given_dictionary_only(self):
        from hh_core import SkillExtractor

        ex = SkillExtractor({"1с": "1С"})
        assert "python" not in ex.skills
        assert ex.extract_from_description("<p>1С и Python</p>") == ["1С"]

    def test_extractor_empty_without_dictionary(self):
        from hh_core import SkillExtractor

        ex = SkillExtractor()
        assert ex.skills == {}
        assert ex.extract_from_description("<p>Python</p>") == []

    def test_domain_dictionaries(self):
        """Все доменные словари из dictionary/ читаются и не пустые"""
        base = Path(__file__).parent.parent / "dictionary"
        for name, min_keys in (
            ("skills_python_dev.yaml", 70),
            ("skills_accounting.yaml", 90),
            ("skills_neural.yaml", 15),
            ("skills_ai_engineer.yaml", 25),
            ("skills_sysadmin.yaml", 50),
        ):
            skills = load_skills(str(base / name))
            assert len(skills) >= min_keys, f"{name}: {len(skills)} < {min_keys}"


# =============================================================================
# ТЕСТЫ ПЕРЕСЧЁТА НАВЫКОВ (recalc_skills)
# =============================================================================


class TestRecalcSkills:
    """Тесты пересчёта навыков по БД"""

    def test_recalc_adds_new_dictionary_skills(self, tmp_path):
        """Пересчёт добавляет навыки из нового словаря к существующим вакансиям"""
        import matplotlib

        matplotlib.use("Agg")

        db = tmp_path / "t.db"
        storage = Storage(f"sqlite:///{db}")
        storage.save_vacancies([
            Vacancy(id="1", name="Бухгалтер", description="Нужен 1С и ЭДО", skills=[]),
        ])

        yaml_file = tmp_path / "acc.yaml"
        yaml_file.write_text('"1с": "1С"\n"эдо": "ЭДО"\n', encoding="utf-8")

        config = ParserConfig(
            db_path=str(db),
            reports_dir=str(tmp_path / "reports"),
            report_excel=str(tmp_path / "r.xlsx"),
            report_csv=str(tmp_path / "r.csv"),
            report_chart=str(tmp_path / "r.png"),
        )
        recalc_skills(config, [str(yaml_file)])

        names = {r["Навык"] for r in storage.get_skills_report()}
        assert {"1С", "ЭДО"} <= names

    def test_recalc_keeps_existing_skills(self, tmp_path):
        """Пересчёт не теряет уже сохранённые навыки"""
        import matplotlib

        matplotlib.use("Agg")

        db = tmp_path / "t.db"
        storage = Storage(f"sqlite:///{db}")
        storage.save_vacancies([
            Vacancy(id="1", name="V", description="", skills=["Docker"]),
        ])

        yaml_file = tmp_path / "acc.yaml"
        yaml_file.write_text('"1с": "1С"\n', encoding="utf-8")

        config = ParserConfig(
            db_path=str(db),
            reports_dir=str(tmp_path / "reports"),
            report_excel=str(tmp_path / "r.xlsx"),
            report_csv=str(tmp_path / "r.csv"),
            report_chart=str(tmp_path / "r.png"),
        )
        recalc_skills(config, [str(yaml_file)])

        names = {r["Навык"] for r in storage.get_skills_report()}
        assert "Docker" in names


# =============================================================================
# ТЕСТЫ ЗАПОЛНЕНИЯ ОРГАНИЗАЦИЙ (fill_employers)
# =============================================================================


class TestFillEmployers:
    """Тесты дозаполнения организации и даты публикации по БД"""

    SAMPLE_HTML = (
        '<span class="vacancy-company-name">'
        '<a data-qa="vacancy-company-name" href="/employer/955?dpt=955">'
        '<span class="magritte-text">СОГАЗ:&nbsp;г. Москва</span></a></span>'
        '<time dateTime="2026-10-26T06:48:02.753Z">26\xa0октября\xa02026</time>'
    )

    def test_parse_employer_html(self):
        """Разбор организации и даты из HTML страницы вакансии"""
        from hh_core import _parse_employer_html

        employer, published = _parse_employer_html(self.SAMPLE_HTML)
        assert employer == "СОГАЗ: г. Москва"
        assert published == "26 октября 2026"
        assert _parse_employer_html("<html></html>") == (None, None)

    def test_parse_employer_html_confidential(self):
        """Скрытый работодатель: имя без ссылки тоже извлекается"""
        from hh_core import _parse_employer_html

        html = (
            '<span class="vacancy-company-name">'
            '<span class="magritte-text">Медицинская компания</span>'
            "</span>"
        )
        employer, _ = _parse_employer_html(html)
        assert employer == "Медицинская компания"

    def test_fill_employers_updates_db(self, tmp_path, monkeypatch):
        """fill_employers заполняет employer и published_at в БД"""
        from sqlalchemy.orm import Session

        from hh_pipeline import fill_employers

        db = tmp_path / "t.db"
        storage = Storage(f"sqlite:///{db}")
        storage.save_vacancies([
            Vacancy(id="100", name="V", description="", skills=[],
                    url="https://hh.ru/vacancy/100"),
            Vacancy(id="101", name="V2", description="", skills=[],
                    url="https://hh.ru/vacancy/101", employer="Уже Есть"),
        ])

        monkeypatch.setattr("hh_pipeline.time.sleep", lambda s: None)
        monkeypatch.setattr(
            "hh_pipeline._fetch_page_html", lambda url, timeout=30: self.SAMPLE_HTML
        )

        fill_employers(ParserConfig(db_path=str(db)))

        with Session(storage.engine) as session:
            vm = session.get(VacancyModel, "100")
            assert vm.employer == "СОГАЗ: г. Москва"
            assert vm.published_at == "26 октября 2026"
            # у уже заполненной вакансии организация не перезаписывается,
            # но добавляется отсутствовавшая дата
            vm2 = session.get(VacancyModel, "101")
            assert vm2.employer == "Уже Есть"
            assert vm2.published_at == "26 октября 2026"

    def test_fill_employers_tolerates_errors(self, tmp_path, monkeypatch):
        """Недоступная страница вакансии не прерывает заполнение"""
        from sqlalchemy.orm import Session

        from hh_pipeline import fill_employers

        db = tmp_path / "t.db"
        storage = Storage(f"sqlite:///{db}")
        storage.save_vacancies([
            Vacancy(id="200", name="V", description="", skills=[],
                    url="https://hh.ru/vacancy/200"),
            Vacancy(id="201", name="V2", description="", skills=[],
                    url="https://hh.ru/vacancy/201"),
        ])

        pages = {
            "https://hh.ru/vacancy/200": None,  # вакансия снята
            "https://hh.ru/vacancy/201": self.SAMPLE_HTML,
        }
        monkeypatch.setattr("hh_pipeline.time.sleep", lambda s: None)
        monkeypatch.setattr(
            "hh_pipeline._fetch_page_html", lambda url, timeout=30: pages[url]
        )

        fill_employers(ParserConfig(db_path=str(db)))

        with Session(storage.engine) as session:
            assert session.get(VacancyModel, "200").employer in (None, "")
            assert session.get(VacancyModel, "201").employer == "СОГАЗ: г. Москва"


# =============================================================================
# ТЕСТЫ ОТЧЁТОВ ИЗ БД (report_only)
# =============================================================================


class TestReportOnly:
    """Тесты перегенерации отчётов из БД"""

    def _config(self, tmp_path):
        return ParserConfig(
            professions=["Python Developer"],
            db_path=str(tmp_path / "t.db"),
            reports_dir=str(tmp_path / "reports"),
            report_excel=str(tmp_path / "r.xlsx"),
            report_csv=str(tmp_path / "r.csv"),
            report_chart=str(tmp_path / "r.png"),
        )

    def test_build_sections_from_db(self, tmp_path):
        """Разделы PDF: только профессии из конфига, в его порядке"""
        from hh_pipeline import build_sections_from_db

        config = self._config(tmp_path)
        config.professions = ["Бухгалтер", "Python Developer"]
        storage = Storage(f"sqlite:///{config.db_path}")
        storage.save_vacancies([
            Vacancy(
                id="1", name="Python Dev", description="нужен Python",
                skills=["Python"], employer="ООО Ромашка",
                url="https://hh.ru/vacancy/1", profession="Python Developer",
                salary_min=100000, salary_max=200000,
                published_at="30 октября 2026",
            ),
            Vacancy(id="2", name="Бухгалтер", description="", skills=["1С"],
                    employer="АО Счёт", url="https://hh.ru/vacancy/2",
                    profession="Бухгалтер"),
            Vacancy(id="3", name="Юрист", description="", skills=["Право"],
                    employer="ООО Право", url="https://hh.ru/vacancy/3",
                    profession="Юрист"),
        ])

        sections = build_sections_from_db(storage, config)

        # «Юрист» в БД есть, но в конфиге нет — в отчёт не попадает
        assert [s["profession"] for s in sections] == ["Бухгалтер", "Python Developer"]
        py = sections[1]
        assert py["vacancies"] == 1
        assert py["salary_min"] == 100000 and py["salary_max"] == 200000
        v = py["vacancies_list"][0]
        assert v.employer == "ООО Ромашка"
        assert v.published_at == "30 октября 2026"
        assert any(r["Навык"] == "Python" for r in py["report"])

    def test_build_sections_last_50(self, tmp_path):
        """limit_per_profession=50 — последние 50 по parsed_at, свежие первыми"""
        from hh_pipeline import build_sections_from_db

        config = self._config(tmp_path)
        storage = Storage(f"sqlite:///{config.db_path}")
        for i in range(1, 56):
            storage.save_vacancies([
                Vacancy(
                    id=str(i), name=f"V{i}", description="", skills=["Python"],
                    url=f"https://hh.ru/vacancy/{i}", profession="Python Developer",
                    parsed_at=datetime(2026, 10, 1) + timedelta(days=i),
                ),
            ])

        sections = build_sections_from_db(storage, config, limit_per_profession=50)

        assert len(sections) == 1
        s = sections[0]
        assert s["vacancies"] == 50
        ids = [v.id for v in s["vacancies_list"]]
        assert "1" not in ids and "5" not in ids  # самые старые отброшены
        assert "6" in ids and "55" in ids
        assert ids[0] == "55"  # сначала свежие

    def test_get_skills_report_for(self, tmp_path):
        """Агрегация навыков по выбранным вакансиям (доля — от выбранных)"""
        config = self._config(tmp_path)
        storage = Storage(f"sqlite:///{config.db_path}")
        storage.save_vacancies([
            Vacancy(id="1", name="A", description="", skills=["Python", "SQL"]),
            Vacancy(id="2", name="B", description="", skills=["Python"]),
            Vacancy(id="3", name="C", description="", skills=["Go"]),
        ])

        rows = storage.get_skills_report_for(["1", "2"])
        report = {r["Навык"]: r for r in rows}
        assert report["Python"]["Упоминаний"] == 2
        assert report["Python"]["% вакансий"] == 100.0
        assert report["SQL"]["Упоминаний"] == 1
        assert report["SQL"]["% вакансий"] == 50.0
        assert "Go" not in report
        assert storage.get_skills_report_for([]) == []

    def test_report_only_last_50(self, tmp_path):
        """limit=2 — в отчёт попадают только 2 последние вакансии по parsed_at"""
        import matplotlib

        matplotlib.use("Agg")

        import pikepdf

        from hh_pipeline import report_only

        config = self._config(tmp_path)
        storage = Storage(f"sqlite:///{config.db_path}")
        for i in range(1, 4):
            storage.save_vacancies([
                Vacancy(
                    id=str(i), name=f"V{i}", description="",
                    skills=["Go"] if i == 1 else ["Python"],
                    url=f"https://hh.ru/vacancy/{i}", profession="Python Developer",
                    parsed_at=datetime(2026, 10, i),
                ),
            ])

        report_only(config, limit_per_profession=2)

        df = pd.read_csv(config.report_csv)
        assert set(df["Навык"]) == {"Python"}  # Go — только в самой старой вакансии

        pdfs = list(Path(config.reports_dir).glob("*-hh-parser.pdf"))
        assert len(pdfs) == 1
        with pikepdf.open(str(pdfs[0])) as pdf:
            # титул + график + таблица + вакансии (2 строки на одной странице)
            annots = pdf.pages[3].get("/Annots")
            assert annots is not None and len(annots) == 2

    def test_report_only_creates_files(self, tmp_path):
        """report_only создаёт Excel/CSV/PNG и PDF со страницей вакансий"""
        import matplotlib

        matplotlib.use("Agg")

        import pikepdf

        from hh_pipeline import report_only

        config = self._config(tmp_path)
        storage = Storage(f"sqlite:///{config.db_path}")
        storage.save_vacancies([
            Vacancy(
                id="1", name="Python Dev", description="", skills=["Python"],
                employer="ООО Ромашка", url="https://hh.ru/vacancy/1",
                profession="Python Developer", published_at="30 октября 2026",
            ),
        ])

        report_only(config)

        assert Path(config.report_excel).exists()
        assert Path(config.report_csv).exists()
        assert Path(config.report_chart).exists()

        pdfs = list(Path(config.reports_dir).glob("*-hh-parser.pdf"))
        assert len(pdfs) == 1
        with pikepdf.open(str(pdfs[0])) as pdf:
            # титул + график + таблица + вакансии
            assert len(pdf.pages) == 4
            annots = pdf.pages[3].get("/Annots")
            assert annots is not None and len(annots) == 1
            assert str(annots[0].A.URI) == "https://hh.ru/vacancy/1"

    def test_report_only_empty_db(self, tmp_path):
        """Пустая БД — отчёты не создаются, без исключений"""
        import matplotlib

        matplotlib.use("Agg")

        from hh_pipeline import report_only

        config = self._config(tmp_path)
        Storage(f"sqlite:///{config.db_path}")

        report_only(config)

        assert not list(Path(config.reports_dir).glob("*.pdf"))

    def test_report_only_without_professions(self, tmp_path, caplog):
        """Без профессий отчёты не создаются, ошибка в логе"""
        from hh_pipeline import report_only

        config = self._config(tmp_path)
        config.professions = []

        report_only(config)

        assert "Профессии не заданы" in caplog.text
        assert not list(Path(config.reports_dir).glob("*.pdf"))


# =============================================================================
# ТЕСТЫ ЛОГИРОВАНИЯ (MonthlyFileHandler)
# =============================================================================


class TestMonthlyFileHandler:
    """Тесты месячного лог-файла"""

    def _make_logger(self, handler, name):
        import logging as log_mod

        logger = log_mod.getLogger(name)
        logger.setLevel(log_mod.INFO)
        logger.propagate = False
        logger.handlers = [handler]
        return logger

    def test_writes_to_month_file(self, tmp_path):
        """Лог пишется в файл с именем YYYY-MM-hh-parser.log"""
        from main import MonthlyFileHandler

        handler = MonthlyFileHandler(log_dir=str(tmp_path))
        logger = self._make_logger(handler, "test-monthly-1")
        logger.info("запись в лог")
        handler.close()

        expected = f"{datetime.now().strftime('%Y-%m')}-hh-parser.log"
        assert (tmp_path / expected).exists()
        assert "запись в лог" in (tmp_path / expected).read_text(encoding="utf-8")

    def test_rotates_on_month_change(self, monkeypatch, tmp_path):
        """При смене месяца создаётся новый файл, старый остаётся"""
        from main import MonthlyFileHandler

        handler = MonthlyFileHandler(log_dir=str(tmp_path))
        logger = self._make_logger(handler, "test-monthly-2")
        logger.info("до смены месяца")

        monkeypatch.setattr(
            MonthlyFileHandler, "_current_month", staticmethod(lambda: "2000-01")
        )
        logger.info("после смены месяца")
        handler.close()

        rotated = tmp_path / "2000-01-hh-parser.log"
        assert rotated.exists()
        assert "после смены месяца" in rotated.read_text(encoding="utf-8")

        current = tmp_path / f"{datetime.now().strftime('%Y-%m')}-hh-parser.log"
        assert "до смены месяца" in current.read_text(encoding="utf-8")

    def test_separator_between_runs(self, tmp_path):
        """Перед каждым новым запуском в лог добавляется пустая строка"""
        import logging as log_mod

        from main import setup_logging

        log_file = tmp_path / f"{datetime.now().strftime('%Y-%m')}-hh-parser.log"

        added = []
        try:
            for _ in range(2):  # два "запуска" в один и тот же файл
                root = log_mod.getLogger()
                before = list(root.handlers)
                setup_logging(str(tmp_path))
                added.extend(h for h in root.handlers if h not in before)
        finally:
            root = log_mod.getLogger()
            for h in added:
                root.removeHandler(h)
                h.close()

        content = log_file.read_text(encoding="utf-8")
        # по одной пустой строке-разделителю на запуск
        assert content == "\n\n"

    def test_log_dir_created(self, tmp_path):
        """Папка log создаётся автоматически, в том числе вложенная"""
        from main import MonthlyFileHandler

        log_dir = tmp_path / "nested" / "log"
        handler = MonthlyFileHandler(log_dir=str(log_dir))
        handler.close()
        assert log_dir.exists()


# =============================================================================
# ЗАПУСК ТЕСТОВ
# =============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
