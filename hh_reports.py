# hh_reports.py
"""
Генераторы отчётов: Excel/CSV/PNG-график (ReportGenerator) и многостраничный
PDF с графиком, таблицей навыков и списком вакансий с кликабельными ссылками
(PdfReportGenerator). Кириллица — шрифт DejaVu Sans (matplotlib).
"""

import re
import logging
import textwrap
from datetime import datetime
from typing import List, Dict, Optional
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

def _wrap_label(text: str, width: int = 22) -> str:
    """Длинные названия навыков — переносом на 2 строки (для подписей осей)"""
    if len(text) <= width:
        return text
    return textwrap.fill(text, width=width, break_long_words=False)


def _fmt_money(value: int) -> str:
    """100000 -> '100 000'"""
    return f"{value:,}".replace(",", " ")


# Названия регионов hh.ru для подписи в отчёте (остальные — «регион hh.ru №N»)
AREA_NAMES = {
    1: "Москва",
    2: "Санкт-Петербург",
    113: "Россия",
    1001: "Другие страны",
    2019: "Московская область",
    2090: "г. Королев",
}


def _area_name(area_id: Optional[int]) -> Optional[str]:
    if area_id is None:
        return None
    return AREA_NAMES.get(area_id, f"регион hh.ru №{area_id}")


RU_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
    "январь": 1, "февраль": 2, "март": 3, "апрель": 4, "май": 5, "июнь": 6,
    "июль": 7, "август": 8, "сентябрь": 9, "октябрь": 10, "ноябрь": 11, "декабрь": 12,
}


def _vacancy_date(vacancy: "Vacancy") -> str:
    """Дата вакансии в формате дд.мм.гггг.

    Дата публикации с hh.ru («30 октября 2026»; год может быть опущен —
    тогда берётся год сохранения), иначе дата сохранения."""
    if vacancy.published_at:
        text = vacancy.published_at.replace("Вакансия опубликована", "").strip()
        match = re.match(r"(\d{1,2})\s+([А-Яа-яЁё]+)\s*(\d{4})?", text)
        if match:
            day = int(match.group(1))
            month = RU_MONTHS.get(match.group(2).lower())
            if month:
                if match.group(3):
                    year = int(match.group(3))
                elif vacancy.parsed_at:
                    year = vacancy.parsed_at.year
                else:
                    year = datetime.now().year
                return f"{day:02d}.{month:02d}.{year}"
        return text
    if vacancy.parsed_at:
        return vacancy.parsed_at.strftime("%d.%m.%Y")
    return "—"


def _vacancy_employer(vacancy: "Vacancy") -> str:
    return vacancy.employer or "—"


def _normalize_space(text: Optional[str]) -> str:
    """Переносы строк и повторяющиеся пробелы (Selenium кладет их в .text)"""
    return re.sub(r"\s+", " ", (text or "")).strip()


def _vacancy_salary(vacancy: "Vacancy") -> str:
    """Зарплатная вилка вакансии для строки отчёта; '' если не указана"""
    mn, mx = vacancy.salary_min, vacancy.salary_max
    if mn and mx:
        return f"от {_fmt_money(mn)} до {_fmt_money(mx)} ₽"
    if mn:
        return f"от {_fmt_money(mn)} ₽"
    if mx:
        return f"до {_fmt_money(mx)} ₽"
    return ""


def _vacancy_url_full(vacancy: "Vacancy") -> str:
    """Полная каноническая ссылка: https://hh.ru/vacancy/<id>"""
    if not vacancy.url:
        return ""
    match = re.search(r"/vacancy/(\d+)", vacancy.url)
    if match:
        return f"https://hh.ru/vacancy/{match.group(1)}"
    return vacancy.url


def _shorten(text: Optional[str], limit: int) -> str:
    """Обрезка длинного текста с многоточием"""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 1)].rstrip() + "…"


# =============================================================================
# ГЕНЕРАТОР ОТЧЁТОВ
# =============================================================================


class ReportGenerator:
    """Генерация отчётов"""

    def __init__(self, data: List[Dict]):
        self.df = pd.DataFrame(data)

    def to_console(self) -> None:
        """Вывод в консоль"""
        print("\n" + "=" * 70)
        print("  📊 ОТЧЁТ ПО НАВЫКАМ")
        print("=" * 70)

        if self.df.empty:
            print("  Нет данных")
        else:
            for i, row in self.df.iterrows():
                bar = "█" * int(row["% вакансий"] / 5)
                print(
                    f"  {i + 1:3d}. {row['Навык']:<25s} "
                    f"| {row['Упоминаний']:>4d} "
                    f"| {row['% вакансий']:>5.1f}% "
                    f"| {bar}"
                )

        print("=" * 70)
        print(f"  Всего уникальных навыков: {len(self.df)}")
        print("=" * 70 + "\n")

    def to_excel(self, path: str) -> None:
        """Экспорт в Excel"""
        Path(path).parent.mkdir(parents=True, exist_ok=True)

        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            self.df.to_excel(writer, index=False, sheet_name="Навыки")

            ws = writer.sheets["Навыки"]
            for col in ws.columns:
                max_len = max(len(str(cell.value or "")) for cell in col)
                ws.column_dimensions[col[0].column_letter].width = max_len + 2

        logger.info(f"📊 Excel отчёт сохранён: {path}")

    def to_csv(self, path: str) -> None:
        """Экспорт в CSV"""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.df.to_csv(path, index=False, encoding="utf-8-sig")
        logger.info(f"📄 CSV отчёт сохранён: {path}")

    def to_chart(self, path: str, top_n: int = 20) -> None:
        """Визуализация топ-N навыков"""
        import matplotlib.pyplot as plt
        import matplotlib

        matplotlib.rcParams["font.family"] = "DejaVu Sans"

        top = self.df.head(top_n)

        fig, ax = plt.subplots(figsize=(12, max(6, len(top) * 0.4)))
        # Сверху самый тёмный, к низу светлеет
        colors = plt.cm.Blues(
            [0.3 + 0.7 * (1 - i / len(top)) for i in range(len(top))]
        )
        bars = ax.barh(range(len(top)), top["Упоминаний"], color=colors)

        ax.set_yticks(range(len(top)))
        ax.set_yticklabels([_wrap_label(s) for s in top["Навык"]], fontsize=10)
        ax.invert_yaxis()
        ax.set_xlabel("Количество упоминаний", fontsize=12)
        ax.set_title(f"Топ-{min(top_n, len(top))} навыков", fontsize=14, fontweight="bold")

        for bar, val in zip(bars, top["Упоминаний"]):
            ax.text(bar.get_width() + 1, bar.get_y() + bar.get_height() / 2, str(val), va="center", fontsize=9)

        plt.tight_layout()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        plt.savefig(path, dpi=150, bbox_inches="tight")
        plt.close()

        logger.info(f"📈 График сохранён: {path}")


class PdfReportGenerator:
    """Многостраничный PDF-отчёт с поддержкой кириллицы (альбомная ориентация).

    Титульная страница: название отчёта + дата/время создания + список профессий.
    Далее для каждой профессии: страница с графиком топ-N навыков,
    страница с таблицей (48 навыков в двух колонках с продолжением)
    и страница(ы) со списком вакансий — по строке на вакансию через запятые.
    Шрифт — DejaVu Sans (поставляется с matplotlib).
    """

    TITLE = "Анализ требований hh.ru по вакансиям"
    PAGE_SIZE = (11.69, 8.27)  # A4, альбомная
    CHART_TOP_N = 20
    TABLE_ROWS = 48
    VACANCIES_FONTSIZE = 9  # обычный текст списка вакансий под номерами
    VACANCIES_LINE_SPACING = 1.45
    VACANCIES_MAX = 50  # не более 50 последних вакансий на страницах ссылок

    def __init__(
        self,
        sections: List[Dict],
        created_at: datetime,
        area_id: Optional[int] = None,
    ):
        """sections: [{"profession": str, "report": List[Dict], "vacancies": int}]"""
        self.sections = sections
        self.created_at = created_at
        self.area_name = _area_name(area_id)
        self._fig_links: dict = {}
        self._page_links: list = []

    def generate(self, path: str) -> None:
        import matplotlib

        matplotlib.use("Agg")
        matplotlib.rcParams["font.family"] = "DejaVu Sans"
        from matplotlib.backends.backend_pdf import PdfPages

        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._fig_links: dict = {}
        self._page_links: list = []
        page_no = 0

        with PdfPages(path) as pdf:
            info = pdf.infodict()
            info["Title"] = self.TITLE
            info["Author"] = "HH Skills Parser"
            info["CreationDate"] = self.created_at

            def emit(fig):
                """Сохраняет страницу и запоминает её номер для ссылок"""
                nonlocal page_no
                pdf.savefig(fig)
                links = self._fig_links.pop(id(fig), None)
                if links:
                    for bbox, url, dpi in links:
                        self._page_links.append((page_no, bbox, dpi, url))
                page_no += 1
                import matplotlib.pyplot as plt
                plt.close(fig)

            emit(self._title_page())
            for section in self.sections:
                if section["report"]:
                    emit(self._chart_page(section))
                    emit(self._table_page(section))
                    if section.get("vacancies_list") is not None:
                        for vac_fig in self._vacancies_pages(section):
                            emit(vac_fig)
                else:
                    emit(self._empty_page(section))

        self._add_pdf_links(path)
        logger.info(f"📑 PDF отчёт сохранён: {path}")

    def _add_pdf_links(self, path: str) -> None:
        """Добавляет в готовый PDF кликабельные ссылки-аннотации (pikepdf)"""
        if not self._page_links:
            return
        try:
            import pikepdf
        except ImportError:
            logger.warning("⚠️ pikepdf не установлен — ссылки в PDF не активны")
            return

        with pikepdf.open(path, allow_overwriting_input=True) as pdf:
            for page_no, bbox, dpi, url in self._page_links:
                if page_no >= len(pdf.pages):
                    continue
                page = pdf.pages[page_no]
                annot = pdf.make_indirect(
                    pikepdf.Dictionary(
                        Type=pikepdf.Name.Annot,
                        Subtype=pikepdf.Name.Link,
                        Rect=[
                            float(bbox.x0 * 72 / dpi),
                            float(bbox.y0 * 72 / dpi),
                            float(bbox.x1 * 72 / dpi),
                            float(bbox.y1 * 72 / dpi),
                        ],
                        Border=[0, 0, 0],
                        A=pikepdf.Dictionary(S=pikepdf.Name.URI, URI=url),
                    )
                )
                existing = page.get("/Annots")
                if existing is None:
                    page.Annots = pikepdf.Array([annot])
                else:
                    page.Annots = pikepdf.Array(list(existing) + [annot])
            pdf.save(path)
        logger.info(f"🔗 Кликабельных ссылок в PDF: {len(self._page_links)}")

    @staticmethod
    def _salary_note(section: Dict) -> str:
        """Зарплатная вилка в скобках: (от X до Y ₽)"""
        mn, mx = section.get("salary_min"), section.get("salary_max")
        if mn and mx:
            return f"(от {_fmt_money(mn)} до {_fmt_money(mx)} ₽)"
        if mn:
            return f"(от {_fmt_money(mn)} ₽)"
        if mx:
            return f"(до {_fmt_money(mx)} ₽)"
        return "(зарплата не указана)"

    def _title_page(self):
        import matplotlib.pyplot as plt

        fig = plt.figure(figsize=self.PAGE_SIZE)
        fig.text(
            0.5, 0.70, self.TITLE,
            ha="center", va="center", fontsize=24, fontweight="bold",
        )
        if self.area_name:
            fig.text(
                0.5, 0.625, f"Регион: {self.area_name}",
                ha="center", va="center", fontsize=15,
            )
        fig.text(
            0.5, 0.56,
            f"Отчёт создан: {self.created_at.strftime('%d.%m.%Y %H:%M')}",
            ha="center", va="center", fontsize=14,
        )
        if self.sections:
            # Метка и список с точками — по одной левой границе
            names = "\n".join(
                f"•  {s['profession']} {self._salary_note(s)}" for s in self.sections
            )
            fig.text(
                0.30, 0.44,
                "Профессии в отчёте:\n" + names,
                ha="left", va="top", fontsize=13, linespacing=1.9,
            )
        return fig

    def _header(self, fig, section, suffix: str = ""):
        fig.text(0.5, 0.95, section["profession"] + suffix, ha="center",
                 fontsize=17, fontweight="bold")
        fig.text(0.5, 0.905,
                 f"Вакансий проанализировано: {section['vacancies']}",
                 ha="center", fontsize=10, color="gray")

    def _chart_page(self, section):
        import matplotlib.pyplot as plt

        fig = plt.figure(figsize=self.PAGE_SIZE)
        self._header(fig, section, ", график")

        top = section["report"][: self.CHART_TOP_N]
        ax = fig.add_axes([0.15, 0.07, 0.81, 0.78])
        # Сверху самый тёмный, к низу светлеет
        colors = plt.cm.Blues(
            [0.3 + 0.7 * (1 - i / len(top)) for i in range(len(top))]
        )
        bars = ax.barh(range(len(top)), [r["Упоминаний"] for r in top], color=colors)
        ax.set_yticks(range(len(top)))
        ax.set_yticklabels([_wrap_label(r["Навык"]) for r in top], fontsize=8)
        ax.invert_yaxis()
        ax.set_xlabel("Количество упоминаний", fontsize=11)
        ax.set_title(f"Топ-{len(top)} навыков", fontsize=14, fontweight="bold")
        for bar, r in zip(bars, top):
            ax.text(bar.get_width() + 0.3, bar.get_y() + bar.get_height() / 2,
                    str(r["Упоминаний"]), va="center", fontsize=8)
        return fig

    def _table_page(self, section):
        import matplotlib.pyplot as plt

        fig = plt.figure(figsize=self.PAGE_SIZE)
        self._header(fig, section, ", таблица")

        rows = section["report"][: self.TABLE_ROWS]
        halves = [rows[: self.TABLE_ROWS // 2], rows[self.TABLE_ROWS // 2 :]]
        layouts = [(1, [0.03, 0.05, 0.45, 0.8]), (self.TABLE_ROWS // 2 + 1, [0.52, 0.05, 0.45, 0.8])]

        for half, (start_num, pos) in zip(halves, layouts):
            if not half:
                continue
            ax = fig.add_axes(pos)
            ax.axis("off")
            table = ax.table(
                colLabels=["№", "Навык", "Упоминаний", "% вакансий"],
                cellText=[
                    [
                        str(start_num + i),
                        r["Навык"],
                        str(r["Упоминаний"]),
                        f"{r['% вакансий']:.1f}%",
                    ]
                    for i, r in enumerate(half)
                ],
                colWidths=[0.09, 0.49, 0.2, 0.22],
                loc="upper center",
            )
            table.auto_set_font_size(False)
            table.set_fontsize(9)
            table.scale(1, 1.6)
            for j in range(4):
                header = table[0, j]
                header.set_facecolor("#2F5496")
                header.set_text_props(color="white", fontweight="bold")
            for i in range(1, len(half) + 1):
                if i % 2 == 0:
                    for j in range(4):
                        table[i, j].set_facecolor("#EDF2FA")
            # «Навык» — по левому краю (заголовок по центру), «№» — по центру
            for i in range(1, len(half) + 1):
                name_cell = table[i, 1]
                name_cell.set_text_props(ha="left")
                name_cell.PAD = 0.02
                table[i, 0].set_text_props(ha="center")
        return fig

    def _vacancies_pages(self, section) -> list:
        """Страницы «<профессия>, вакансии»: одна вакансия — одна строка через
        запятые (не более 50 последних вакансий). Длинная строка переносится
        на следующую; если строки не помещаются на лист — продолжение на новой
        странице (без заголовка). Ссылка полная, не сокращается и кликабельна
        (аннотация — после сборки PDF). Организация и название вакансии
        укорачиваются до 50 символов; после названия выводится зарплата."""
        import matplotlib.pyplot as plt

        vacancies = (section.get("vacancies_list") or [])[: self.VACANCIES_MAX]
        if not vacancies:
            fig = plt.figure(figsize=self.PAGE_SIZE)
            self._header(fig, section)
            fig.text(0.5, 0.5, "Нет данных о вакансиях", ha="center", fontsize=13)
            return [fig]

        fs = self.VACANCIES_FONTSIZE
        line_h = fs * self.VACANCIES_LINE_SPACING / (72 * self.PAGE_SIZE[1])
        left = 0.035
        right = 0.98
        bottom = 0.05
        y_first_page = 0.86  # под заголовком
        y_other_pages = 0.94  # продолжение — без заголовка, с самого верха

        # Строки вакансий: слова префикса + ссылка (неразрывный токен)
        rows = []
        for i, v in enumerate(vacancies, 1):
            prefix = (
                f"{i}. {_vacancy_date(v)}, "
                f"{_shorten(_normalize_space(_vacancy_employer(v)), 50)}, "
                f"{_shorten(_normalize_space(v.name), 50)}, "
                f"{_vacancy_salary(v)}, "
            )
            tokens = [(word + " ", False) for word in prefix.split(" ") if word]
            url = _vacancy_url_full(v)
            if url:
                tokens.append((url, True))
            rows.append(tokens)

        # Измерение ширины токенов на временной фигуре того же размера
        meas_fig = plt.figure(figsize=self.PAGE_SIZE)
        meas_fig.canvas.draw()
        meas_renderer = meas_fig.canvas.get_renderer()
        width_px = meas_fig.bbox.width
        max_w = (right - left) * width_px
        cache: Dict[str, float] = {}

        def text_w(text: str) -> float:
            if text not in cache:
                t = meas_fig.text(0, 0, text, fontsize=fs)
                cache[text] = t.get_window_extent(meas_renderer).width
                t.remove()
            return cache[text]

        # Жадная раскладка по строкам
        all_lines = []
        for tokens in rows:
            cur, cur_w = [], 0.0
            for token, is_url in tokens:
                w = text_w(token)
                if cur and cur_w + w > max_w:
                    all_lines.append(cur)
                    cur, cur_w = [], 0.0
                cur.append((token, is_url))
                cur_w += w
            if cur:
                all_lines.append(cur)
        plt.close(meas_fig)

        # Постранично: первая страница — с заголовком, продолжения — без него
        figures = []
        idx = 0
        part = 0
        while idx < len(all_lines):
            fig = plt.figure(figsize=self.PAGE_SIZE)
            if part == 0:
                self._header(fig, section, ", вакансии")
                y = y_first_page
            else:
                y = y_other_pages
            capacity = max(int((y - bottom) / line_h), 1)
            chunk = all_lines[idx : idx + capacity]
            idx += len(chunk)

            url_artists = []
            for line in chunk:
                before = "".join(t for t, is_url in line if not is_url)
                url_text = "".join(t for t, is_url in line if is_url)
                t_before = None
                if before:
                    t_before = fig.text(
                        left, y, before, ha="left", va="center", fontsize=fs
                    )
                if url_text:
                    t_url = fig.text(
                        left, y, url_text,
                        ha="left", va="center", fontsize=fs, color="#1F4E9C",
                    )
                    url_artists.append((t_before, t_url, url_text, y))
                y -= line_h

            link_entries: list = []
            try:
                fig.canvas.draw()
                renderer = fig.canvas.get_renderer()
                for t_before, t_url, url_text, y in url_artists:
                    x = left
                    if t_before is not None:
                        x += t_before.get_window_extent(renderer).width / width_px
                    t_url.set_position((x, y))
                fig.canvas.draw()
                renderer = fig.canvas.get_renderer()
                for t_before, t_url, url_text, y in url_artists:
                    bbox = t_url.get_window_extent(renderer)
                    link_entries.append((bbox, url_text, fig.dpi))
            except Exception as e:
                logger.warning(f"⚠️ Ссылки на странице вакансий не активны: {e}")

            self._fig_links[id(fig)] = link_entries
            figures.append(fig)
            part += 1

        return figures

    def _empty_page(self, section):
        import matplotlib.pyplot as plt

        fig = plt.figure(figsize=self.PAGE_SIZE)
        self._header(fig, section)
        fig.text(0.5, 0.5, "Нет данных по этой профессии",
                 ha="center", fontsize=14)
        return fig
