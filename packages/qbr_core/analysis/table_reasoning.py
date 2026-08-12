from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Any


def _normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    return re.sub(r"[^a-z0-9%\u4e00-\u9fff]+", "", text)


HEADER_ALIASES: tuple[tuple[str, ...], ...] = (
    ("vonb", "新业务价值"),
    ("anp",),
    ("margin", "利润率"),
    ("13m继续率", "13个月继续率", "persistency13m"),
    ("数字直通率", "电子投保率", "digitalstp"),
    ("转化率",),
    ("cac",),
    ("ape",),
    ("线索", "线索量"),
    ("签发", "签发量"),
    ("年度额", "年度金额", "金额"),
    ("占比",),
    ("预期roe",),
    ("流动性",),
    ("风险限额利用",),
    ("人均产能",),
    ("股东资本比率", "shareholdercapitalratio"),
    ("operatingroev", "营运roev"),
    ("evequity", "内含价值权益"),
    ("同比变化", "同比", "变化", "固定汇率", "cer"),
    ("当前", "当前值", "current", "currentvalue"),
    ("阈值", "threshold", "limit", "target"),
)


def _header_matches(question: str, header: str) -> bool:
    question_key = _normalize(question)
    header_key = _normalize(header)
    if len(header_key) >= 2 and header_key in question_key:
        return True
    year = re.search(r"20\d{2}", header_key)
    if year and year.group(0) in question_key:
        return True
    for aliases in HEADER_ALIASES:
        normalized_aliases = tuple(_normalize(alias) for alias in aliases)
        if any(alias in header_key for alias in normalized_aliases) and any(alias in question_key for alias in normalized_aliases):
            return True
    return "同比变化" in header_key and any(
        term in question_key for term in ("同比", "每股", "增长", "固定汇率", "cer")
    )


def _header_question_position(question: str, header: str) -> int:
    folded = question.casefold()
    header_folded = header.casefold()
    if header_folded in folded:
        return folded.find(header_folded)
    header_key = _normalize(header)
    positions: list[int] = []
    for aliases in HEADER_ALIASES:
        if not any(_normalize(alias) in header_key for alias in aliases):
            continue
        positions.extend(folded.find(alias.casefold()) for alias in aliases if alias.casefold() in folded)
    year = re.search(r"20\d{2}", header_key)
    if year and year.group(0) in folded:
        positions.append(folded.find(year.group(0)))
    return min(positions, default=-1)


def _number(value: str) -> float | None:
    match = re.search(r"[-+]?\d[\d,]*(?:\.\d+)?", value.replace("−", "-"))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def _format_number(value: float) -> str:
    if math.isclose(value, round(value), abs_tol=1e-9):
        return f"{int(round(value)):,}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def _coordination_relaxed(value: str) -> str:
    """Normalize labels while treating optional CJK conjunctions as separators."""

    return re.sub(r"(?<=[\u4e00-\u9fff])[和与及](?=[\u4e00-\u9fff])", "", _normalize(value))


def _is_total_label(value: str) -> bool:
    key = _normalize(value)
    return any(marker in key for marker in ("合计", "总计", "集团合计", "total"))


def _label_span(question: str, label: str) -> tuple[int, int] | None:
    """Locate a source row label in the question with optional conjunction variation."""

    folded = unicodedata.normalize("NFKC", question).casefold()
    candidates = [label, *re.split(r"\s*(?:/|｜|\||·)\s*", label)]
    spans: list[tuple[int, int]] = []
    for candidate in candidates:
        candidate = unicodedata.normalize("NFKC", candidate).casefold().strip()
        if len(_normalize(candidate)) < 2:
            continue
        parts = [part for part in re.split(r"[和与及]", candidate) if part]
        pattern = r"\s*(?:和|与|及)?\s*".join(re.escape(part) for part in parts)
        match = re.search(pattern, folded, flags=re.I)
        if match:
            spans.append(match.span())
    return min(spans, default=None)


def _condition_passes(current: str, condition: str) -> bool | None:
    current_value = _number(current)
    threshold_value = _number(condition)
    if current_value is None or threshold_value is None:
        return None
    if ">=" in condition or "≥" in condition:
        return current_value >= threshold_value
    if "<=" in condition or "≤" in condition:
        return current_value <= threshold_value
    if ">" in condition:
        return current_value > threshold_value
    if "<" in condition:
        return current_value < threshold_value
    return current_value == threshold_value


def _requests_evaluation(question: str) -> bool:
    """Detect decision wording without tying it to a metric or domain."""

    return bool(
        re.search(
            r"(?:是否|能否|有没有|算不算|达标|满足|状态|闸门|风险|越线|突破|超出|合规|"
            r"within\s+(?:the\s+)?(?:limit|threshold|target)|pass(?:es|ed)?|breach(?:es|ed)?|"
            r"compliant|acceptable|material|risk)",
            question,
            flags=re.I,
        )
    )


@dataclass(slots=True)
class ParsedTable:
    """Provide normalized headers and rows for deterministic table reasoning."""
    source: dict[str, Any]
    headers: list[str]
    rows: list[list[str]]

    @classmethod
    def from_source(cls, source: dict[str, Any]) -> ParsedTable | None:
        """Build a parsed table from one structured evidence source."""
        lines = [line.strip() for line in str(source.get("content", "")).splitlines() if line.strip()]
        cells = [[cell.strip() for cell in line.split("|")] for line in lines]
        if len(cells) < 2 or len(cells[0]) < 2:
            return None
        width = len(cells[0])
        rows = [(row + [""] * width)[:width] for row in cells[1:]]
        return cls(source=source, headers=cells[0], rows=rows)

    def row_matches(self, question: str) -> list[tuple[int, list[str]]]:
        """Return source rows or columns matching the normalized query terms."""
        question_key = _normalize(question)
        relaxed_question_key = _coordination_relaxed(question)
        matches: list[tuple[int, list[str]]] = []
        for index, row in enumerate(self.rows):
            if not row:
                continue
            labels = [row[0], *re.split(r"\s*(?:/|｜|\||·)\s*", row[0])]
            direct_match = False
            for label in labels:
                label_key = _normalize(label)
                relaxed_label_key = _coordination_relaxed(label)
                if len(label_key) < 2 or (
                    label_key not in question_key and relaxed_label_key not in relaxed_question_key
                ):
                    continue
                active_key = label_key if label_key in question_key else relaxed_label_key
                active_question = question_key if label_key in question_key else relaxed_question_key
                position = active_question.find(active_key)
                after = active_question[position + len(active_key):position + len(active_key) + 1]
                if label_key.isascii() and after and after.isascii() and after.isalnum():
                    continue
                direct_match = True
                break
            if direct_match or _header_matches(question, row[0]):
                matches.append((index, row))
        return matches

    def column_matches(self, question: str) -> list[int]:
        """Return source rows or columns matching the normalized query terms."""
        return [index for index, header in enumerate(self.headers[1:], 1) if _header_matches(question, header)]

    def relevance(self, question: str) -> int:
        """Score table relevance against requested terms."""
        question_key = _normalize(question)
        score = 4 * len(self.row_matches(question)) + 3 * len(self.column_matches(question))
        if self.headers and _normalize(self.headers[0]) in question_key:
            score += 5
        if "表" in question and self.source.get("chunk_type") == "table":
            score += 2
        for term in ("矩阵", "阈值", "闸门", "合计", "最高", "最低", "最大", "最小"):
            if term in question and term in str(self.source.get("content", "")):
                score += 2
        header_keys = {_normalize(header) for header in self.headers}
        has_current_and_threshold = bool(header_keys & {"当前", "当前值", "current", "currentvalue"}) and bool(
            header_keys & {"阈值", "threshold", "limit", "target", "绿", "green"}
        )
        if has_current_and_threshold and _requests_evaluation(question):
            score += 3
        return score


@dataclass(slots=True)
class ReasoningResult:
    """Carry a table-derived answer and the evidence used to derive it."""
    answer: str
    source: dict[str, Any]
    operation: str = "table_reasoning"


class TableReasoner:
    """Deterministic reasoning over parser-preserved PowerPoint table rows."""

    def answer(self, question: str, sources: list[dict[str, Any]]) -> ReasoningResult | None:
        """Produce an evidence-grounded answer for the supplied question."""
        tables = [table for source in sources if (table := ParsedTable.from_source(source)) is not None]
        tables.sort(key=lambda table: table.relevance(question), reverse=True)
        for table in tables:
            if table.relevance(question) < 3:
                continue
            operations = (
                ("grouped_aggregate_comparison", self._grouped_aggregate_comparison),
                ("threshold", self._threshold),
                ("extreme", self._extreme),
                ("filter", self._filter),
                ("ratio", self._ratio),
                ("difference", self._difference),
                ("lookup", self._lookup),
            )
            for operation, resolver in operations:
                result = resolver(question, table)
                if not result:
                    continue
                subject = re.match(r"^(.{2,30}?(?:表|矩阵))", question)
                if subject and subject.group(1) not in result:
                    result = f"{subject.group(1)}中，{result}"
                return ReasoningResult(result + " [1]", table.source, operation)
        return None

    @staticmethod
    def _requested_columns(question: str, table: ParsedTable) -> list[int]:
        """Resolve explicitly requested table columns."""
        matches = table.column_matches(question)
        unit_indices = [index for index, header in enumerate(table.headers) if _normalize(header) == "单位"]
        return list(dict.fromkeys([*matches, *unit_indices]))

    def _grouped_aggregate_comparison(self, question: str, table: ParsedTable) -> str | None:
        """Aggregate two explicitly delimited row groups and compare like-for-like columns."""

        folded = question.casefold()
        if not any(cue in folded for cue in ("合计", "合共", "总和", "加总", "combined", "sum", "total")):
            return None
        matched = [
            (index, row, span)
            for index, row in table.row_matches(question)
            if row and not _is_total_label(row[0]) and (span := _label_span(question, row[0])) is not None
        ]
        if len(matched) < 3:
            return None
        matched.sort(key=lambda item: item[2][0])
        boundaries: list[int] = []
        for position, (left, right) in enumerate(zip(matched, matched[1:], strict=False), 1):
            between = question[left[2][1] : right[2][0]]
            if re.search(
                r"(?:合计|合共|总和|加总|combined|sum|total).{0,80}(?:[？?；;。]|与|和|versus|vs\.?|compared)",
                between,
                flags=re.I | re.S,
            ):
                boundaries.append(position)
        if len(boundaries) != 1:
            return None
        boundary = boundaries[0]
        groups = ([item[1] for item in matched[:boundary]], [item[1] for item in matched[boundary:]])
        if not all(groups):
            return None

        columns = [index for index in table.column_matches(question) if index > 0]
        if "占" in question or any(cue in folded for cue in ("share", "percentage", "percent")):
            columns.extend(
                index
                for index, header in enumerate(table.headers)
                if any(cue in _normalize(header) for cue in ("占比", "比例", "份额", "share", "percent"))
            )
        columns = list(dict.fromkeys(columns))
        numeric_columns = [
            index
            for index in columns
            if all(_number(row[index]) is not None for group in groups for row in group)
        ]
        if not numeric_columns:
            return None

        totals = [
            {index: sum(float(_number(row[index]) or 0) for row in group) for index in numeric_columns}
            for group in groups
        ]

        def is_share(index: int) -> bool:
            header = _normalize(table.headers[index])
            return any(marker in header for marker in ("占比", "比例", "份额", "share", "percent")) or all(
                "%" in row[index] for group in groups for row in group
            )

        group_descriptions: list[str] = []
        for group_no, (group, values) in enumerate(zip(groups, totals, strict=True), 1):
            members = "、".join(row[0] for row in group)
            measures = "，".join(
                f"{table.headers[index]}合计为{_format_number(values[index])}{'%' if is_share(index) else ''}"
                for index in numeric_columns
            )
            group_descriptions.append(f"第{group_no}组（{members}）：{measures}")

        comparisons: list[str] = []
        for index in numeric_columns:
            difference = totals[0][index] - totals[1][index]
            if math.isclose(difference, 0.0, abs_tol=1e-9):
                comparisons.append(f"{table.headers[index]}相同")
                continue
            direction = "高" if difference > 0 else "低"
            unit = "个百分点" if is_share(index) else ""
            comparisons.append(f"第1组{table.headers[index]}{direction}{_format_number(abs(difference))}{unit}")
        amount_column = next((index for index in numeric_columns if not is_share(index)), None)
        if amount_column is not None and not math.isclose(totals[1][amount_column], 0.0, abs_tol=1e-12):
            comparisons.append(
                f"第1组{table.headers[amount_column]}约为第2组的"
                f"{totals[0][amount_column] / totals[1][amount_column]:.2f}倍"
            )
        return "；".join(group_descriptions) + "。相比之下，" + "，".join(comparisons) + "。"

    def _threshold(self, question: str, table: ParsedTable) -> str | None:
        """Extract a numeric threshold and comparison operator from the question."""
        if not _requests_evaluation(question):
            return None
        header_keys = [_normalize(header) for header in table.headers]
        current_index = next(
            (i for i, key in enumerate(header_keys) if key in {"当前", "当前值", "current", "currentvalue"}),
            None,
        )
        threshold_index = next(
            (i for i, key in enumerate(header_keys) if key in {"阈值", "threshold", "limit", "target"}),
            None,
        )
        green_index = next((i for i, key in enumerate(header_keys) if key in {"绿", "green"}), None)
        condition_index = threshold_index if threshold_index is not None else green_index
        if current_index is None or condition_index is None:
            return None
        matched = [row for _, row in table.row_matches(question)]
        selected = matched or [row for row in table.rows if row and row[0] and "合计" not in row[0]]
        if not selected or len(selected) > 8:
            return None
        outcomes: list[bool] = []
        details: list[str] = []
        for row in selected:
            current, condition = row[current_index], row[condition_index]
            passed = _condition_passes(current, condition)
            if passed is None:
                continue
            outcomes.append(passed)
            state = "绿色/达标" if passed else "未达标"
            condition_label = table.headers[condition_index]
            if "阈值" not in condition_label:
                condition_label += "阈值"
            details.append(f"{row[0]} {current}（{condition_label} {condition}，{state}）")
        if not details:
            return None
        overall = "全部满足阈值（均达标）" if all(outcomes) else "并非全部达标"
        if green_index is not None:
            overall = "均为绿色" if all(outcomes) else "并非均为绿色"
        return f"{overall}：" + "；".join(details) + "。"

    def _extreme(self, question: str, table: ParsedTable) -> str | None:
        """Answer a supported minimum or maximum table query."""
        operation = next((term for term in ("最高", "最大", "最低", "最小") if term in question), None)
        if operation is None:
            return None
        columns = [index for index in table.column_matches(question) if index > 0]
        numeric_columns = [
            index
            for index in columns
            if sum(_number(row[index]) is not None for row in table.rows) >= 2
        ]
        if not numeric_columns:
            return None
        operation_position = question.index(operation)
        primary = min(
            numeric_columns,
            key=lambda index: abs(_header_question_position(question, table.headers[index]) - operation_position)
            if _header_question_position(question, table.headers[index]) >= 0
            else 10_000 + index,
        )
        candidates = [
            row
            for row in table.rows
            if row and not any(term in row[0] for term in ("合计", "总计", "集团/合计")) and _number(row[primary]) is not None
        ]
        if not candidates:
            return None
        reverse = operation in {"最高", "最大"}
        candidates.sort(key=lambda row: float(_number(row[primary]) or 0), reverse=reverse)
        count = 2 if any(term in question for term in ("两项", "两个", "前二", "前两", "top2", "top-2")) else 1
        selected = candidates[:count]
        requested = [index for index in columns if index != primary]
        pieces = []
        for row in selected:
            extras = "".join(f"，{table.headers[index]}为{row[index]}" for index in requested)
            pieces.append(f"{row[0]}（{table.headers[primary]}为{row[primary]}{extras}）")
        label = "、".join(pieces)
        return f"{table.headers[primary]}{operation}的{'两项' if count == 2 else '项目'}是{label}。"

    def _filter(self, question: str, table: ParsedTable) -> str | None:
        """Answer a supported threshold filter over table rows."""
        condition: tuple[int, str] | None = None
        for index, header in enumerate(table.headers[1:], 1):
            values = sorted({row[index].strip() for row in table.rows if row[index].strip()}, key=len, reverse=True)
            for value in values:
                if re.search(rf"{re.escape(header)}\s*为\s*{re.escape(value)}", question, flags=re.I):
                    condition = (index, value)
                    break
            if condition is not None:
                break
        if condition is None:
            return None
        filter_index, expected = condition
        selected = [
            row for row in table.rows
            if row and "合计" not in row[0] and _normalize(row[filter_index]) == _normalize(expected)
        ]
        if not selected:
            return None
        requested = [
            index for index in table.column_matches(question)
            if index > 0 and index != filter_index and _normalize(table.headers[index]) != "单位"
        ]
        if not requested:
            return None
        if any(term in question for term in ("合计", "占总额", "合共")):
            numeric_index = next(
                (index for index in requested if all(_number(row[index]) is not None for row in selected)),
                None,
            )
            if numeric_index is None:
                return None
            total = sum(float(_number(row[numeric_index]) or 0) for row in selected)
            item_names = "、".join(row[0] for row in selected)
            answer = (
                f"{table.headers[filter_index]}为{expected}的用途包括{item_names}；"
                f"{table.headers[numeric_index]}合计为{_format_number(total)}"
            )
            if "占总额" in question or "比例" in question:
                total_row = next((row for row in table.rows if "合计" in row[0]), None)
                denominator = _number(total_row[numeric_index]) if total_row else None
                if denominator:
                    share_index = next(
                        (index for index, header in enumerate(table.headers) if _normalize(header) == "占比"),
                        None,
                    )
                    stated_share = (
                        sum(float(_number(row[share_index]) or 0) for row in selected)
                        if share_index is not None else None
                    )
                    if stated_share is not None:
                        answer += (
                            f"，按占比列合计{_format_number(stated_share)}%；"
                            f"按金额除以总额精算约{total / denominator * 100:.2f}%"
                        )
                    else:
                        answer += f"，占总额{_format_number(total / denominator * 100)}%"
            return answer + "。"
        details = []
        for row in selected:
            values = "，".join(f"{table.headers[index]}为{row[index]}" for index in requested)
            details.append(f"{row[0]}（{values}）")
        return f"{table.headers[filter_index]}为{expected}的用途有" + "、".join(details) + "。"

    def _ratio(self, question: str, table: ParsedTable) -> str | None:
        """Calculate a supported ratio between two table values."""
        if not any(term in question for term in ("占", "多少倍", "几倍")):
            return None
        row_matches = table.row_matches(question)
        columns = [index for index in table.column_matches(question) if index > 0]
        if "占比" in question and len(columns) >= 2 and not any(term in question for term in ("占合计", "比例约")):
            return None
        numeric_columns = [index for index in columns if any(_number(row[index]) is not None for row in table.rows)]
        if not numeric_columns:
            return None
        column = numeric_columns[0]
        if "占" in question and row_matches:
            numerator_row = row_matches[0][1]
            total_row = next((row for row in table.rows if any(term in row[0] for term in ("合计", "总计"))), None)
            if total_row is None:
                return None
            numerator, denominator = _number(numerator_row[column]), _number(total_row[column])
            if numerator is None or denominator in {None, 0}:
                return None
            ratio = numerator / denominator * 100
            return (
                f"{numerator_row[0]}的{table.headers[column]}为{numerator_row[column]}，合计为{total_row[column]}；"
                f"{_format_number(numerator)}/{_format_number(denominator)}≈{ratio:.1f}%。"
            )
        if any(term in question for term in ("多少倍", "几倍")) and len(row_matches) >= 2:
            ordered = sorted(row_matches, key=lambda item: question.find(item[1][0]))
            first, second = ordered[0][1], ordered[1][1]
            numerator, denominator = _number(first[column]), _number(second[column])
            if numerator is None or denominator in {None, 0}:
                return None
            return (
                f"{first[0]}的{table.headers[column]}为{first[column]}，{second[0]}为{second[column]}；"
                f"{_format_number(numerator)}/{_format_number(denominator)}≈{numerator / denominator:.2f}倍。"
            )
        return None

    def _difference(self, question: str, table: ParsedTable) -> str | None:
        """Calculate a supported difference between two table values."""
        if not any(
            term in question for term in ("提高", "增加", "下降", "高多少", "低多少", "差多少", "变化了多少")
        ):
            return None
        row_matches = table.row_matches(question)
        columns = [index for index in table.column_matches(question) if index > 0]
        if len(row_matches) >= 2 and columns:
            column = columns[0]
            ordered = sorted(row_matches, key=lambda item: question.find(item[1][0]))
            first, second = ordered[0][1], ordered[1][1]
            first_value, second_value = _number(first[column]), _number(second[column])
            if first_value is None or second_value is None:
                return None
            difference = first_value - second_value
            if "低多少" in question:
                comparison = f"{first[0]}低{_format_number(abs(difference))}"
            elif "高多少" in question:
                comparison = f"{first[0]}高{_format_number(abs(difference))}"
            else:
                comparison = f"差额为{_format_number(difference)}"
            return (
                f"{first[0]}的{table.headers[column]}为{first[column]}，{second[0]}为{second[column]}；"
                f"{comparison}。"
            )
        if len(row_matches) == 1 and len(columns) >= 2:
            row = row_matches[0][1]
            time_columns = [index for index in columns if re.search(r"20\d{2}", table.headers[index])]
            if len(time_columns) < 2:
                return None
            time_columns.sort(
                key=lambda index: question.casefold().find(table.headers[index].casefold())
                if table.headers[index].casefold() in question.casefold()
                else 10_000 + index
            )
            first_index, second_index = time_columns[0], time_columns[-1]
            first_value, second_value = _number(row[first_index]), _number(row[second_index])
            if first_value is None or second_value is None:
                return None
            difference = second_value - first_value
            magnitude = abs(difference)
            direction = "提高" if difference >= 0 else "下降"
            suffix = "个百分点" if "%" in row[first_index] or "%" in row[second_index] else ""
            unit_index = next((i for i, header in enumerate(table.headers) if _normalize(header) == "单位"), None)
            if not suffix and unit_index is not None and row[unit_index]:
                suffix = f" {row[unit_index]}"
            return (
                f"{row[0]}从{table.headers[first_index]}的{row[first_index]}变为"
                f"{table.headers[second_index]}的{row[second_index]}，{direction}{_format_number(magnitude)}{suffix}。"
            )
        return None

    def _lookup(self, question: str, table: ParsedTable) -> str | None:
        """Return a directly requested table value with provenance."""
        row_matches = table.row_matches(question)
        if not row_matches:
            if "合计" in question:
                total = next((row for row in table.rows if "合计" in row[0]), None)
                row_matches = [(0, total)] if total else []
            if not row_matches:
                return None
        columns = self._requested_columns(question, table)
        columns = [index for index in columns if index > 0 and _normalize(table.headers[index]) != "单位"]
        if not columns:
            return None
        # A lookup should not silently choose an unrelated row when several are
        # matched; multi-row comparisons are handled by dedicated operations.
        row = row_matches[0][1]
        unit_index = next((i for i, header in enumerate(table.headers) if _normalize(header) == "单位"), None)
        unit = row[unit_index] if unit_index is not None else ""
        details = []
        for index in columns:
            value = row[index]
            if unit and index != unit_index and not any(symbol in value for symbol in ("%", "$", "bn", "m")):
                value = f"{value} {unit}"
            details.append(f"{table.headers[index]}为{value}")
        return f"{row[0]}：" + "；".join(details) + "。"
