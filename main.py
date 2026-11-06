# main.py
"""
Точка входа CLI HH Skills Parser.

Собирает конфигурацию, настраивает логирование и запускает нужный сценарий:
парсинг hh.ru, пересчёт навыков, дозаполнение организаций или генерацию
отчётов из БД. Запуск: python main.py [опции].
"""

import argparse
import logging
from datetime import datetime
from pathlib import Path

from config import ParserConfig
from hh_pipeline import (
    run_parsing,
    recalc_skills,
    fill_employers,
    report_only,
)

class MonthlyFileHandler(logging.FileHandler):
    """Лог-файл на месяц: log/YYYY-MM-hh-parser.log.

    При смене месяца первая же запись открывает файл нового месяца.
    Старые логи никогда не удаляются автоматически.
    """

    def __init__(self, log_dir: str = "log", encoding: str = "utf-8"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._month = self._current_month()
        super().__init__(
            self.log_dir / f"{self._month}-hh-parser.log", encoding=encoding
        )

    @staticmethod
    def _current_month() -> str:
        return datetime.now().strftime("%Y-%m")

    def write_separator(self) -> None:
        """Пустая строка-разделитель перед новым запуском"""
        if self.stream is None:
            self.stream = self._open()
        self.stream.write("\n")
        self.flush()

    def emit(self, record: logging.LogRecord) -> None:
        if self._current_month() != self._month:
            self._month = self._current_month()
            self.close()
            self.baseFilename = str(self.log_dir / f"{self._month}-hh-parser.log")
            self.stream = None
        super().emit(record)


def setup_logging(log_dir: str) -> None:
    """Логирование в консоль и в месячный файл log/YYYY-MM-hh-parser.log"""
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    file_handler = MonthlyFileHandler(log_dir=log_dir)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # Пустая строка-разделитель перед новым запуском
    file_handler.write_separator()


def main():
    """CLI точка входа"""
    parser = argparse.ArgumentParser(
        description="🕷️ HH Skills Parser — Анализ навыков с hh.ru",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument("--config", "-c", default=None, help="Путь к YAML конфигурации")
    parser.add_argument("--professions", "-p", nargs="+", help="Список профессий")
    parser.add_argument("--area", "-a", type=int, default=None, help="ID региона")
    parser.add_argument("--pages", type=int, default=None, help="Максимум страниц")
    parser.add_argument(
        "--max-details", type=int, default=None,
        help="Лимит загрузки деталей вакансий на профессию (быстрый прогон)",
    )
    parser.add_argument(
        "--skills-file", default=None,
        help="YAML-файл со своим словарём навыков",
    )
    parser.add_argument(
        "--recalc-skills", nargs="*", metavar="YAML",
        help="Пересчитать навыки по всем вакансиям в БД (можно указать YAML-словари)",
    )
    parser.add_argument(
        "--fill-employers", action="store_true",
        help="Дозаполнить организацию и дату публикации по вакансиям в БД (HTTP-запросы к hh.ru)",
    )
    parser.add_argument(
        "--report-only", action="store_true",
        help="Перегенерировать отчёты (Excel/CSV/PNG + PDF) из БД без парсинга",
    )

    args = parser.parse_args()

    # Загрузка конфигурации: --config, иначе dictionary/config.yaml, иначе дефолты
    config_path = args.config
    if config_path is None:
        default_config = Path(__file__).parent / "dictionary" / "config.yaml"
        if default_config.exists():
            config_path = str(default_config)
    config = ParserConfig.from_yaml(config_path) if config_path else ParserConfig()

    if args.professions:
        config.professions = args.professions
    if args.area is not None:
        config.area_id = args.area
    if args.pages is not None:
        config.max_pages = args.pages
    if args.max_details is not None:
        config.max_details_per_profession = args.max_details
    if args.skills_file is not None:
        config.skills_file = args.skills_file

    setup_logging(config.log_dir)

    # Режимы по флагу; каждый завершает работу, не доходя до парсинга
    if args.fill_employers:
        fill_employers(config)
        return
    if args.recalc_skills is not None:
        recalc_skills(config, args.recalc_skills)
        return
    if args.report_only:
        report_only(config)
        return
    if not config.parsing_hh:
        # Без парсинга hh.ru: отчёт по последним 50 вакансиям каждой профессии
        report_only(config, limit_per_profession=50)
        return

    run_parsing(config)


if __name__ == "__main__":
    main()
