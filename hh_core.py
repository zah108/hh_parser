# hh_core.py
"""
Ядро парсера hh.ru: модель вакансии, Selenium-скрапер, извлечение навыков,
хранилище SQLite (SQLAlchemy), вспомогательные функции разбора и загрузки
страниц вакансий.
"""

import re
import time
import random
import logging
from html import unescape
from datetime import datetime, timezone
from typing import List, Dict, Optional
from dataclasses import dataclass, field
from collections import Counter
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from sqlalchemy import (
    create_engine, Column, String, Integer,
    DateTime, ForeignKey, Table, Text, text, bindparam
)
from sqlalchemy.orm import declarative_base, relationship, Session
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

from config import ParserConfig

logger = logging.getLogger(__name__)

def _utcnow() -> datetime:
    """Текущее время UTC (наивное — для совместимости с SQLite)"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _fetch_page_html(url: str, timeout: int = 30) -> Optional[str]:
    """Загрузка HTML-страницы прямым HTTP-запросом (браузерный User-Agent).

    None — страница недоступна (вакансия снята/архивирована, блокировка)."""
    for attempt, pause in ((1, 0), (2, 15)):
        if pause:
            logger.info(f"⏳ Пауза {pause} с перед повтором {url}")
            time.sleep(pause)
        try:
            req = Request(
                url,
                headers={"User-Agent": BROWSER_UA, "Accept-Language": "ru-RU,ru;q=0.9"},
            )
            with urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except HTTPError as e:
            if e.code in (403, 429) and attempt == 1:
                continue  # вероятно троттлинг — пробуем ещё раз после паузы
            logger.warning(f"⚠️ {url}: HTTP {e.code}")
            return None
        except Exception as e:
            logger.warning(f"⚠️ {url}: {e}")
            return None
    return None


def _parse_employer_html(html: str) -> tuple:
    """Из HTML страницы вакансии: (организация, дата публикации).

    Организация — элемент .vacancy-company-name (у скрытых работодателей
    без ссылки — анонимное имя вроде «Медицинская компания»),
    дата — текст элемента <time dateTime="…">."""
    employer = None
    match = re.search(
        r'class="vacancy-company-name"[^>]*>(.*?)</span>', html, re.S
    ) or re.search(
        r'data-qa="vacancy-company-name"[^>]*>(.*?)</a>', html, re.S
    )
    if match:
        inner = re.sub(r"<[^>]+>", " ", match.group(1))
        employer = re.sub(r"\s+", " ", unescape(inner)).strip() or None

    published = None
    match = re.search(r"<time[^>]*dateTime=[^>]*>([^<]+)</time>", html)
    if match:
        published = unescape(match.group(1)).replace("\xa0", " ").strip() or None

    return employer, published


def _parse_salary(text: Optional[str]) -> tuple:
    """Разбор строки зарплаты hh.ru.

    'от 100 000 до 150 000 ₽' -> (100000, 150000)
    'от 80 000 руб.'          -> (80000, None)
    'до 200 000 ₽'            -> (None, 200000)
    'зарплата не указана'     -> (None, None)
    """
    if not text:
        return None, None
    t = text.replace("\u00a0", " ").lower()
    if "не указана" in t:
        return None, None
    numbers = [int(re.sub(r"\D", "", m)) for m in re.findall(r"\d[\d ]*", t)]
    numbers = [n for n in numbers if n > 0]
    if not numbers:
        return None, None
    has_from = bool(re.search(r"\bот\b", t))
    has_to = bool(re.search(r"\bдо\b", t))
    if has_from and has_to and len(numbers) >= 2:
        return numbers[0], numbers[-1]
    if has_from:
        return numbers[0], None
    if has_to:
        return None, numbers[-1]
    if len(numbers) >= 2:
        return min(numbers), max(numbers)
    return numbers[0], numbers[0]


# =============================================================================
# МОДЕЛЬ ДАННЫХ
# =============================================================================


@dataclass
class Vacancy:
    """Модель вакансии"""
    id: str
    name: str
    description: str
    skills: List[str]
    employer: Optional[str] = None
    area: Optional[str] = None
    url: Optional[str] = None
    profession: Optional[str] = None
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    published_at: Optional[str] = None
    parsed_at: datetime = field(default_factory=_utcnow)


# =============================================================================
# SELENIUM ПАРСЕР
# =============================================================================


class HHSeleniumScraper:
    """
    Парсер hh.ru на базе Selenium с обходом детекции автоматизации.
    """

    SEARCH_URL = "https://hh.ru/search/vacancy"

    def __init__(self, config: ParserConfig, headless: bool = True):
        self.config = config
        self.headless = headless
        self.driver = None

    def _create_driver(self):
        """Создание Chrome WebDriver с обходом детекции"""
        options = Options()

        if self.headless:
            options.add_argument("--headless=new")

        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        options.add_argument(
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        )

        # Не ждать полной загрузки (картинки/аналитика) — хватит готовности DOM:
        # описание и навыки лежат в статическом HTML
        options.page_load_strategy = "eager"

        # Картинки для парсинга не нужны — не загружаем
        options.add_experimental_option(
            "prefs", {"profile.managed_default_content_settings.images": 2}
        )

        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(60)

        # Скрытие webdriver флага
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": """
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    window.chrome = {runtime: {}, loadTimes: function() {}, csi: function() {}, app: {}};
                """
            },
        )

        return driver

    def __enter__(self):
        logger.info("🌐 Запуск Selenium WebDriver...")
        self.driver = self._create_driver()
        self.driver.implicitly_wait(5)
        return self

    def __exit__(self, *args):
        if self.driver:
            self.driver.quit()
            logger.info("🌐 Selenium WebDriver закрыт")

    def search_vacancies(self, profession: str) -> List[Vacancy]:
        """Поиск вакансий через Selenium"""
        all_vacancies = []

        for page in range(self.config.max_pages):
            try:
                params = {
                    "text": profession,
                    "area": self.config.area_id,
                    "page": page,
                    "per_page": "100",
                }

                url = f"{self.SEARCH_URL}?{urlencode(params)}"
                logger.info(f"🌐 Selenium: загрузка страницы {page}...")
                self.driver.get(url)
                time.sleep(1.5)  # страховка на дорисовку SPA после готовности DOM

                vacancies = self._parse_page()

                if not vacancies:
                    logger.info(f"🚫 Selenium страница {page}: нет результатов")
                    break

                all_vacancies.extend(vacancies)
                logger.info(
                    f"📄 Selenium страница {page}: {len(vacancies)} вакансий "
                    f"(всего: {len(all_vacancies)})"
                )

                time.sleep(random.uniform(
                    self.config.request_delay,
                    self.config.request_delay * 3
                ))

            except Exception as e:
                logger.error(f"❌ Selenium ошибка на странице {page}: {e}")
                break

        return all_vacancies

    def _parse_page(self) -> List[Vacancy]:
        """Парсинг текущей страницы выдачи.

        Основной путь — карточки [data-qa="vacancy-serp__vacancy"]:
        название, ссылка и организация берутся из карточки.
        Запасной путь (другая вёрстка) — все ссылки на вакансии.
        """
        vacancies = []
        seen = set()

        try:
            cards = self.driver.find_elements(
                By.CSS_SELECTOR, '[data-qa="vacancy-serp__vacancy"]'
            )
        except Exception:
            cards = []

        for card in cards:
            try:
                try:
                    link = card.find_element(
                        By.CSS_SELECTOR, 'a[data-qa="serp-item__title"]'
                    )
                except Exception:
                    link = card.find_element(
                        By.CSS_SELECTOR, 'a[href*="/vacancy/"]'
                    )
                href = link.get_attribute("href") or ""
                match = re.search(r"/vacancy/(\d+)", href)
                name = link.text.strip()

                if not match or not name:
                    continue

                vacancy_id = match.group(1)
                if vacancy_id in seen:
                    continue
                seen.add(vacancy_id)

                employer = None
                try:
                    emp_el = card.find_element(
                        By.CSS_SELECTOR,
                        '[data-qa="vacancy-serp__vacancy-employer"]',
                    )
                    employer = emp_el.text.strip() or None
                except Exception:
                    pass

                vacancies.append(Vacancy(
                    id=vacancy_id,
                    name=name,
                    description="",
                    skills=[],
                    employer=employer,
                    url=href.split("?")[0],
                ))
            except Exception:
                continue

        if vacancies:
            return vacancies

        # Запасной путь: ссылки на вакансии напрямую
        try:
            links = self.driver.find_elements(By.CSS_SELECTOR, 'a[href*="/vacancy/"]')

            for link in links:
                try:
                    href = link.get_attribute("href") or ""
                    match = re.search(r"/vacancy/(\d+)", href)
                    name = link.text.strip()

                    if not match or not name:
                        continue

                    vacancy_id = match.group(1)
                    if vacancy_id in seen:
                        continue
                    seen.add(vacancy_id)

                    vacancies.append(Vacancy(
                        id=vacancy_id,
                        name=name,
                        description="",
                        skills=[],
                        url=href.split("?")[0],
                    ))
                except Exception:
                    continue
        except Exception:
            pass

        return vacancies

    def get_vacancy_details(self, vacancy: Vacancy) -> Vacancy:
        """Загрузка страницы вакансии: описание и ключевые навыки"""
        if not vacancy.url or not self.driver:
            return vacancy

        try:
            self.driver.get(vacancy.url)
            time.sleep(random.uniform(
                self.config.request_delay,
                self.config.request_delay * 2,
            ))

            try:
                desc_el = self.driver.find_element(
                    By.CSS_SELECTOR, '[data-qa="vacancy-description"]'
                )
                vacancy.description = desc_el.text or ""
            except Exception:
                vacancy.description = ""

            try:
                sal_el = self.driver.find_element(
                    By.CSS_SELECTOR, '[data-qa="vacancy-salary"]'
                )
                vacancy.salary_min, vacancy.salary_max = _parse_salary(sal_el.text)
            except Exception:
                pass

            try:
                time_el = self.driver.find_element(By.CSS_SELECTOR, "time[datetime]")
                vacancy.published_at = time_el.text.strip() or None
            except Exception:
                pass

            try:
                emp_el = self.driver.find_element(
                    By.CSS_SELECTOR, '[data-qa="vacancy-company-name"]'
                )
                vacancy.employer = emp_el.text.strip() or None
            except Exception:
                pass

            # Каждый навык — отдельный <li data-qa="skills-element">
            key_skills = []
            try:
                tags = self.driver.find_elements(
                    By.CSS_SELECTOR, '[data-qa="skills-element"]'
                )
                key_skills = [t.text.strip() for t in tags if t.text and t.text.strip()]
            except Exception:
                key_skills = []

            vacancy.skills = key_skills

        except Exception as e:
            logger.warning(f"⚠️ Не удалось загрузить детали вакансии {vacancy.id}: {e}")

        return vacancy


# =============================================================================
# ИЗВЛЕЧЕНИЕ НАВЫКОВ
# =============================================================================


class SkillExtractor:
    """NLP-модуль для извлечения навыков из описания вакансий"""

    def __init__(self, skills: Optional[Dict[str, str]] = None):
        """Эффективный словарь передаётся извне (файлы из конфига)."""
        self.skills = dict(skills or {})

    def clean_html(self, text: str) -> str:
        """Очистка HTML-тегов"""
        text = unescape(text)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text)
        return text.lower()

    def extract_from_description(self, description: str) -> List[str]:
        """Извлечение навыков из описания"""
        cleaned = self.clean_html(description)
        found = set()

        for key, canonical in self.skills.items():
            pattern = rf'\b{re.escape(key)}\b'
            if re.search(pattern, cleaned):
                found.add(canonical)

        return list(found)

    def extract_from_key_skills(self, key_skills: List[str]) -> List[str]:
        """Нормализация ключевых навыков вакансии через словарь"""
        result = set()
        for raw in key_skills or []:
            item = (raw or "").strip()
            if not item:
                continue
            result.add(self.skills.get(item.lower(), item))
        return list(result)

    def extract_all(self, vacancy: Vacancy) -> List[str]:
        """Извлечение всех навыков из вакансии"""
        skills = set(self.extract_from_key_skills(vacancy.skills))
        skills.update(self.extract_from_description(vacancy.description))
        return list(skills)

    def aggregate_skills(self, vacancies: List[Vacancy]) -> List[Dict]:
        """Агрегация навыков по всем вакансиям"""
        counter = Counter()

        for vacancy in vacancies:
            unique_skills = set(self.extract_all(vacancy))
            counter.update(unique_skills)

        total = len(vacancies)
        if total == 0:
            return []

        return [
            {
                "Навык": skill,
                "Упоминаний": count,
                "% вакансий": round(count / total * 100, 1),
            }
            for skill, count in counter.most_common()
        ]


# =============================================================================
# ХРАНИЛИЩЕ ДАННЫХ
# =============================================================================


Base = declarative_base()

vacancy_skill = Table(
    "vacancy_skill",
    Base.metadata,
    Column("vacancy_id", String, ForeignKey("vacancies.id")),
    Column("skill_id", Integer, ForeignKey("skills.id")),
)


class VacancyModel(Base):
    """SQLAlchemy модель вакансии"""
    __tablename__ = "vacancies"

    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    description = Column(Text)
    employer = Column(String)
    area = Column(String)
    url = Column(String)
    salary_min = Column(Integer)
    salary_max = Column(Integer)
    profession = Column(String)
    published_at = Column(String)
    parsed_at = Column(DateTime, default=_utcnow)

    skills = relationship(
        "SkillModel", secondary=vacancy_skill, back_populates="vacancies"
    )


class SkillModel(Base):
    """SQLAlchemy модель навыка"""
    __tablename__ = "skills"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, unique=True, nullable=False)

    vacancies = relationship(
        "VacancyModel", secondary=vacancy_skill, back_populates="skills"
    )


class Storage:
    """Управление хранилищем данных"""

    def __init__(self, db_url: str = "sqlite:///vacancies.db"):
        self.engine = create_engine(db_url, echo=False)
        Base.metadata.create_all(self.engine)
        self._migrate()
        logger.info(f"🗄️  Хранилище инициализировано: {db_url}")

    def _migrate(self):
        """Добавляет в существующую БД столбцы, появившиеся позже
        (SQLite не умеет дополнять таблицу через create_all)."""
        with self.engine.begin() as conn:
            rows = conn.exec_driver_sql("PRAGMA table_info(vacancies)").fetchall()
            cols = {row[1] for row in rows}
            for column, ddl in (
                ("salary_min", "INTEGER"),
                ("salary_max", "INTEGER"),
                ("profession", "TEXT"),
                ("published_at", "TEXT"),
            ):
                if cols and column not in cols:
                    conn.exec_driver_sql(
                        f"ALTER TABLE vacancies ADD COLUMN {column} {ddl}"
                    )
                    logger.info(f"🛠 Миграция: добавлен столбец vacancies.{column}")

    def save_vacancies(self, vacancies: List[Vacancy]) -> int:
        """Сохранение вакансий в БД"""
        saved = 0

        with Session(self.engine) as session:
            for v in vacancies:
                existing = session.get(VacancyModel, v.id)
                if existing:
                    # Дубликат: дополняем зарплату, профессию, организацию и дату
                    if v.salary_min is not None:
                        existing.salary_min = v.salary_min
                    if v.salary_max is not None:
                        existing.salary_max = v.salary_max
                    if v.profession and not existing.profession:
                        existing.profession = v.profession
                    if v.employer and not existing.employer:
                        existing.employer = v.employer
                    if v.published_at and not existing.published_at:
                        existing.published_at = v.published_at
                    continue

                vacancy_model = VacancyModel(
                    id=v.id,
                    name=v.name,
                    description=v.description,
                    employer=v.employer or "",
                    area=v.area or "",
                    url=v.url or "",
                    salary_min=v.salary_min,
                    salary_max=v.salary_max,
                    profession=v.profession or "",
                    published_at=v.published_at or "",
                )
                session.add(vacancy_model)

                for skill_name in v.skills:
                    skill = session.query(SkillModel).filter_by(name=skill_name).first()
                    if not skill:
                        skill = SkillModel(name=skill_name)
                    vacancy_model.skills.append(skill)

                saved += 1

            session.commit()

        logger.info(f"💾 Сохранено вакансий: {saved}")
        return saved

    def get_skills_report(self) -> List[Dict]:
        """Получение агрегированного отчёта"""
        with Session(self.engine) as session:
            total = session.query(VacancyModel).count()
            if total == 0:
                return []

            query = """
                SELECT s.name, COUNT(vs.vacancy_id) as cnt
                FROM skills s
                JOIN vacancy_skill vs ON s.id = vs.skill_id
                GROUP BY s.name
                ORDER BY cnt DESC, s.name ASC
            """
            results = session.execute(text(query)).fetchall()

            return [
                {
                    "Навык": name,
                    "Упоминаний": cnt,
                    "% вакансий": round(cnt / total * 100, 1),
                }
                for name, cnt in results
            ]

    def get_skills_report_for(self, vacancy_ids: List[str]) -> List[Dict]:
        """Агрегированный отчёт по заданным вакансиям (тот же формат, что get_skills_report)"""
        total = len(vacancy_ids)
        if total == 0:
            return []

        with Session(self.engine) as session:
            rows = []
            # SQLite не переваривает in_(…) на тысячи значений — батчим по 200
            for start in range(0, total, 200):
                batch = list(vacancy_ids[start : start + 200])
                query = text(
                    "SELECT s.name, COUNT(vs.vacancy_id) AS cnt "
                    "FROM skills s "
                    "JOIN vacancy_skill vs ON s.id = vs.skill_id "
                    "WHERE vs.vacancy_id IN :ids "
                    "GROUP BY s.name"
                ).bindparams(bindparam("ids", expanding=True))
                rows.extend(session.execute(query, {"ids": batch}).fetchall())

            counts: Dict[str, int] = {}
            for name, cnt in rows:
                counts[name] = counts.get(name, 0) + cnt

        return [
            {
                "Навык": name,
                "Упоминаний": cnt,
                "% вакансий": round(cnt / total * 100, 1),
            }
            for name, cnt in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ]

    def get_vacancies_count(self) -> int:
        """Общее количество вакансий в БД"""
        with Session(self.engine) as session:
            return session.query(VacancyModel).count()

    def get_salary_range(self, profession: str) -> tuple:
        """Накопленная зарплатная вилка по профессии из БД"""
        with Session(self.engine) as session:
            row = session.execute(
                text(
                    "SELECT MIN(salary_min), MAX(salary_max) FROM vacancies "
                    "WHERE profession = :p "
                    "AND (salary_min IS NOT NULL OR salary_max IS NOT NULL)"
                ),
                {"p": profession},
            ).fetchone()
        if not row or (row[0] is None and row[1] is None):
            return None, None
        return row[0], row[1]
