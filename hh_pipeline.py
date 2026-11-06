# hh_pipeline.py
"""
Сценарии работы парсера: полный прогон парсинга hh.ru (run_parsing),
пересчёт навыков по БД (recalc_skills), дозаполнение организаций/дат
(fill_employers), построение разделов отчёта из БД
(build_sections_from_db) и генерация отчётов (report_only).
"""

import time
import random
import logging
from datetime import datetime
from typing import List, Dict, Optional
from pathlib import Path

from sqlalchemy import or_
from sqlalchemy.orm import Session

from config import ParserConfig, load_skills
from hh_core import (
    Vacancy,
    HHSeleniumScraper,
    SkillExtractor,
    SkillModel,
    Storage,
    VacancyModel,
    _fetch_page_html,
    _parse_employer_html,
    _utcnow,
)
from hh_reports import PdfReportGenerator, ReportGenerator

logger = logging.getLogger(__name__)

def make_extractor(config: ParserConfig, profession: str) -> SkillExtractor:
    """SkillExtractor со словарём профессии:
    skills_files[профессия] (путь или список путей) > skills_file."""
    entry = (config.skills_files or {}).get(profession) or config.skills_file
    paths = [entry] if isinstance(entry, str) else list(entry or [])
    merged: Dict[str, str] = {}
    for path in paths:
        merged.update(load_skills(path))
    if merged:
        logger.info(
            f"📚 Словарь для '{profession}': {', '.join(paths)} "
            f"({len(merged)} ключей)"
        )
    else:
        logger.warning(
            f"⚠️ Для '{profession}' словарь не задан — "
            f"из описаний навыки извлекаться не будут"
        )
    return SkillExtractor(merged or None)


def fill_employers(config: ParserConfig) -> None:
    """Дозаполнение организации и даты публикации в БД.

    Для вакансий с пустым employer/published_at страница вакансии
    загружается прямым HTTP-запросом (без браузера) и поля обновляются."""
    storage = Storage(f"sqlite:///{config.db_path}")

    with Session(storage.engine) as session:
        targets = session.query(VacancyModel).filter(
            or_(
                VacancyModel.employer.is_(None),
                VacancyModel.employer == "",
                VacancyModel.published_at.is_(None),
                VacancyModel.published_at == "",
            )
        ).all()
        total = len(targets)
        logger.info(f"🏢 Вакансий без организации/даты: {total}")
        if total == 0:
            return

        filled_emp = filled_date = failed = 0
        for idx, vm in enumerate(targets, 1):
            url = vm.url or f"https://hh.ru/vacancy/{vm.id}"
            html = _fetch_page_html(url)
            if html is None:
                failed += 1
            else:
                employer, published = _parse_employer_html(html)
                if employer and not vm.employer:
                    vm.employer = employer
                    filled_emp += 1
                if published and not vm.published_at:
                    vm.published_at = published
                    filled_date += 1
                if not employer:
                    failed += 1

            if idx % 10 == 0 or idx == total:
                session.commit()
                logger.info(
                    f"  ⏳ {idx}/{total}: организация +{filled_emp}, "
                    f"дата +{filled_date}, не найдено {failed}"
                )
            time.sleep(random.uniform(0.3, 0.9))

        session.commit()

    logger.info(
        f"✅ Заполнено: организация — {filled_emp}, дата — {filled_date}, "
        f"пусто/недоступно — {failed}"
    )


def build_sections_from_db(
    storage: Storage,
    config: ParserConfig,
    limit_per_profession: Optional[int] = None,
) -> List[Dict]:
    """Разделы PDF-отчёта из данных БД — только по профессиям из конфига,
    в порядке их следования в config.professions.

    limit_per_profession — взять последние N вакансий профессии
    (по parsed_at, самые свежие первыми); None — все вакансии."""
    with Session(storage.engine) as session:
        in_db = {
            p for (p,) in session.query(VacancyModel.profession).distinct().all() if p
        }
        extra = sorted(in_db - set(config.professions))
        if extra:
            logger.info(
                f"⏭️  В БД есть профессии вне конфига — пропущены: {', '.join(extra)}"
            )

        sections = []
        for profession in config.professions:
            if profession not in in_db:
                continue
            # Последние вакансии первыми (новые — сверху)
            query = session.query(VacancyModel).filter_by(
                profession=profession
            ).order_by(VacancyModel.parsed_at.desc(), VacancyModel.id.desc())
            if limit_per_profession is not None:
                query = query.limit(limit_per_profession)
            models = query.all()
            if not models:
                continue
            vacancies = [
                Vacancy(
                    id=vm.id,
                    name=vm.name,
                    description=vm.description or "",
                    skills=[s.name for s in vm.skills],
                    employer=vm.employer or None,
                    area=vm.area or None,
                    url=vm.url or None,
                    profession=vm.profession,
                    salary_min=vm.salary_min,
                    salary_max=vm.salary_max,
                    published_at=vm.published_at or None,
                    parsed_at=vm.parsed_at or _utcnow(),
                )
                for vm in models
            ]
            extractor = make_extractor(config, profession)
            mins = [v.salary_min for v in vacancies if v.salary_min]
            maxs = [v.salary_max for v in vacancies if v.salary_max]
            sections.append({
                "profession": profession,
                "report": extractor.aggregate_skills(vacancies),
                "vacancies": len(vacancies),
                "vacancies_list": vacancies,
                "salary_min": min(mins) if mins else None,
                "salary_max": max(maxs) if maxs else None,
            })
        return sections


def report_only(
    config: ParserConfig, limit_per_profession: Optional[int] = None
) -> None:
    """Перегенерация всех отчётов (Excel/CSV/PNG + PDF) из БД без интернета.

    limit_per_profession — построить отчёты по последним N вакансиям
    каждой профессии; None — по всем вакансиям БД."""
    config.ensure_dirs()
    if not config.professions:
        logger.error(
            "⚠️ Профессии не заданы — укажите их в dictionary/config.yaml "
            "(ключ professions) или через -p"
        )
        return

    storage = Storage(f"sqlite:///{config.db_path}")
    sections = build_sections_from_db(
        storage, config, limit_per_profession=limit_per_profession
    )
    if not sections:
        logger.warning("⚠️ В БД нет вакансий с профессией — отчёты не созданы")
        return

    if limit_per_profession is None:
        rows = storage.get_skills_report()
    else:
        vacancy_ids = [
            v.id for s in sections for v in (s.get("vacancies_list") or [])
        ]
        rows = storage.get_skills_report_for(vacancy_ids)
        logger.info(
            f"🗃️  Режим без парсинга: последние {limit_per_profession} "
            f"вакансий каждой профессии (всего {len(vacancy_ids)})"
        )

    reporter = ReportGenerator(rows)
    reporter.to_excel(config.report_excel)
    reporter.to_csv(config.report_csv)
    reporter.to_chart(config.report_chart, config.chart_top_n)

    now = datetime.now()
    pdf_path = Path(config.reports_dir) / f"{now:%Y-%m-%d}-hh-parser.pdf"
    PdfReportGenerator(sections, now, config.area_id).generate(str(pdf_path))
    logger.info("✅ Отчёты перегенерированы из БД")


def run_parsing(config: ParserConfig) -> None:
    """Основная функция парсинга"""
    config.ensure_dirs()

    if not config.professions:
        logger.error(
            "⚠️ Профессии не заданы — укажите их в dictionary/config.yaml "
            "(ключ professions) или через -p"
        )
        return

    logger.info(f"⚙️  Конфигурация: {config}")
    logger.info(f"🎯 Профессии: {config.professions}")
    logger.info("🌐 Режим: Selenium WebDriver")

    storage = Storage(f"sqlite:///{config.db_path}")
    seen_ids: set[str] = set()
    total_saved = 0
    sections: List[Dict] = []

    with HHSeleniumScraper(config) as scraper:
        for profession in config.professions:
            extractor = make_extractor(config, profession)

            logger.info(f"🔍 Поиск: '{profession}'")
            found = scraper.search_vacancies(profession)

            # Пропускаем вакансии, уже найденные в других запросах
            fresh = []
            for vacancy in found:
                if vacancy.id not in seen_ids:
                    seen_ids.add(vacancy.id)
                    fresh.append(vacancy)
            logger.info(f"📋 '{profession}': найдено {len(found)}, новых {len(fresh)}")

            # Детали — самый медленный этап (~3-8 сек на вакансию)
            detailed = []
            total_details = (
                len(fresh)
                if config.max_details_per_profession is None
                else min(len(fresh), config.max_details_per_profession)
            )
            for vacancy in fresh:
                if (
                    config.max_details_per_profession is not None
                    and len(detailed) >= config.max_details_per_profession
                ):
                    break
                scraper.get_vacancy_details(vacancy)
                detailed.append(vacancy)
                if len(detailed) % 10 == 0 or len(detailed) == total_details:
                    logger.info(
                        f"  ⏳ '{profession}': детали {len(detailed)}/{total_details}"
                    )

            if len(detailed) < len(fresh):
                logger.info(
                    f"⏭️  '{profession}': без деталей пропущено "
                    f"{len(fresh) - len(detailed)} (лимит max_details_per_profession)"
                )

            # Извлечение навыков и сохранение сразу после профессии —
            # прогресс не теряется при остановке
            for vacancy in detailed:
                vacancy.profession = profession
                vacancy.skills = extractor.extract_all(vacancy)
            saved = storage.save_vacancies(detailed)
            total_saved += saved
            logger.info(f"💾 '{profession}': сохранено {saved} (всего {total_saved})")

            # Раздел профессии для PDF-отчёта; вилка — по данным последнего запуска
            mins = [v.salary_min for v in detailed if v.salary_min]
            maxs = [v.salary_max for v in detailed if v.salary_max]
            sections.append({
                "profession": profession,
                "report": extractor.aggregate_skills(detailed),
                "vacancies": len(detailed),
                "vacancies_list": detailed,
                "salary_min": min(mins) if mins else None,
                "salary_max": max(maxs) if maxs else None,
            })

    logger.info(f"📦 Всего сохранено вакансий: {total_saved}")

    if total_saved == 0:
        logger.warning("⚠️ Вакансии не найдены. Проверьте параметры.")
        return

    # Отчёты по накопленной БД: Excel/CSV/PNG — по всем вакансиям
    reporter = ReportGenerator(storage.get_skills_report())
    reporter.to_excel(config.report_excel)
    reporter.to_csv(config.report_csv)
    reporter.to_chart(config.report_chart, config.chart_top_n)

    # PDF: титул + страницы каждой профессии (график, таблица, вакансии)
    now = datetime.now()
    pdf_path = Path(config.reports_dir) / f"{now:%Y-%m-%d}-hh-parser.pdf"
    PdfReportGenerator(sections, now, config.area_id).generate(str(pdf_path))

    logger.info("✅ Готово! Все отчёты сформированы.")


def recalc_skills(config: ParserConfig, extra_files: Optional[List[str]] = None) -> None:
    """Пересчёт навыков по всем вакансиям в БД с текущими словарями.

    Описания хранятся в БД, поэтому навыки извлекаются заново.
    Существующие навыки сохраняются (уточняются через словарь), новые —
    добавляются; ничего не удаляется. После пересчёта обновляются
    Excel/CSV/PNG (PDF обновляется отдельно — режим --report-only).
    """
    storage = Storage(f"sqlite:///{config.db_path}")

    custom: Dict[str, str] = {}
    for path in extra_files or []:
        merged = load_skills(path)
        custom.update(merged)
        logger.info(f"📎 Словарь: {path} ({len(merged)} ключей)")

    extractor = SkillExtractor(custom or None)
    logger.info(f"🔄 Пересчёт навыков: словарь {len(extractor.skills)} ключей")

    with Session(storage.engine) as session:
        vacancies = session.query(VacancyModel).all()
        total = len(vacancies)
        logger.info(f"🔄 Вакансий в БД: {total}")

        updated = 0
        for idx, vm in enumerate(vacancies, 1):
            vacancy = Vacancy(
                id=vm.id,
                name=vm.name,
                description=vm.description or "",
                skills=[s.name for s in vm.skills],
            )
            new_names = set(extractor.extract_all(vacancy))
            if new_names == {s.name for s in vm.skills}:
                continue

            vm.skills = []
            for name in sorted(new_names):
                skill = session.query(SkillModel).filter_by(name=name).first()
                if not skill:
                    skill = SkillModel(name=name)
                vm.skills.append(skill)
            updated += 1

            if idx % 25 == 0 or idx == total:
                logger.info(f"  ⏳ пересчитано {idx}/{total} (обновлено {updated})")

        # Убираем осиротевшие названия навыков
        orphans = session.query(SkillModel).filter(~SkillModel.vacancies.any()).all()
        for orphan in orphans:
            session.delete(orphan)

        session.commit()

    logger.info(
        f"✅ Пересчёт завершён: обновлено {updated} из {total}, "
        f"удалено пустых навыков: {len(orphans)}"
    )

    # Обновляем файловые отчёты по свежим данным
    reporter = ReportGenerator(storage.get_skills_report())
    reporter.to_excel(config.report_excel)
    reporter.to_csv(config.report_csv)
    reporter.to_chart(config.report_chart, config.chart_top_n)
    logger.info("📊 Excel/CSV/PNG обновлены")
