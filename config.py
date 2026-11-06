# config.py
"""
Модуль конфигурации парсера hh.ru.
Управляет настройками: список профессий, параметры запросов, задержки, пути.
"""

from dataclasses import dataclass, field
from typing import List, Dict, Optional
from pathlib import Path
import yaml


def load_skills(path: str) -> Dict[str, str]:
    """Загрузка словаря навыков из YAML (плоский или с категориями-вложениями)."""
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Ожидался YAML-словарь навыков: {path}")
    skills: Dict[str, str] = {}
    for key, value in data.items():
        if isinstance(value, dict):  # категории
            skills.update({str(k).lower(): str(v) for k, v in value.items()})
        else:
            skills[str(key).lower()] = str(value)
    return skills


@dataclass
class ParserConfig:
    """Конфигурация парсера hh.ru"""

    # Список профессий для поиска.
    # Единственный источник — dictionary/config.yaml (ключ professions) или CLI (-p).
    # В коде списка нет: пустой список означает «профессии не заданы».
    professions: List[str] = field(default_factory=list)

    # True — обычный режим (парсинг hh.ru); False — работа с БД:
    # отчёт по последним 50 вакансиям каждой профессии из БД, без интернета
    parsing_hh: bool = True

    # Регион (1 = Москва, 2 = Санкт-Петербург, 2019 = Московская область, 2090 = г. Королев)
    area_id: int = 1

    # Максимум страниц для парсинга (по 100 вакансий на страницу)
    max_pages: int = 1

    # Лимит загрузки деталей вакансий на одну профессию (None = без лимита).
    # Детали (описание + ключевые навыки) — самый медленный этап: ~3-5 сек на вакансию.
    max_details_per_profession: Optional[int] = None

    # Словарь навыков: YAML (ключ -> каноническое название; категории опциональны).
    # None — без словаря (навыки только из блока «Ключевые навыки»)
    skills_file: Optional[str] = None

    # Словари по профессиям: путь к YAML-файлу словаря.
    # Единственный источник — dictionary/config.yaml (ключ skills_files).
    # Пустой словарь означает «словарей не задано».
    skills_files: Dict[str, object] = field(default_factory=dict)

    # Задержка между запросами в секундах (rate limiting)
    request_delay: float = 0.5

    # Путь к базе данных (в корне проекта)
    db_path: str = "vacancies.db"

    # Путь к папке с отчётами
    reports_dir: str = "reports"

    # Папка для логов (файл на месяц: log/2026-11-hh-parser.log)
    log_dir: str = "log"

    # Форматы отчётов
    report_excel: str = "reports/skills_report.xlsx"
    report_csv: str = "reports/skills_report.csv"
    report_chart: str = "reports/skills_chart.png"

    # Количество вакансий для топ-графика
    chart_top_n: int = 20

    @classmethod
    def from_yaml(cls, path: str) -> "ParserConfig":
        """Загрузка конфигурации из YAML файла"""
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return cls(**data)

    def to_yaml(self, path: str) -> None:
        """Сохранение конфигурации в YAML файл"""
        data = {
            "professions": self.professions,
            "parsing_hh": self.parsing_hh,
            "area_id": self.area_id,
            "max_pages": self.max_pages,
            "max_details_per_profession": self.max_details_per_profession,
            "skills_file": self.skills_file,
            "skills_files": self.skills_files,
            "request_delay": self.request_delay,
            "db_path": self.db_path,
            "reports_dir": self.reports_dir,
            "log_dir": self.log_dir,
            "report_excel": self.report_excel,
            "report_csv": self.report_csv,
            "report_chart": self.report_chart,
            "chart_top_n": self.chart_top_n,
        }
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False)

    def ensure_dirs(self) -> None:
        """Создание необходимых директорий"""
        Path(self.reports_dir).mkdir(parents=True, exist_ok=True)

    def __str__(self) -> str:
        return (
            f"ParserConfig(\n"
            f"  professions={self.professions},\n"
            f"  area_id={self.area_id},\n"
            f"  max_pages={self.max_pages},\n"
            f"  request_delay={self.request_delay}s,\n"
            f"  db_path={self.db_path},\n"
            f"  reports_dir={self.reports_dir}\n"
            f")"
        )


# Пример YAML конфигурации (config.yaml):
# ---
# professions:
#   - "Python Developer"
#   - "Data Engineer"
#   - "Backend Developer"
# parsing_hh: true
# area_id: 1
# max_pages: 20
# max_details_per_profession: 50
# request_delay: 0.5
# db_path: "vacancies.db"
# reports_dir: "reports"
# log_dir: "log"
# skills_file: "dictionary/skills_accounting.yaml"
# report_excel: "reports/skills_report.xlsx"
# report_csv: "reports/skills_report.csv"
# report_chart: "reports/skills_chart.png"
# chart_top_n: 20
