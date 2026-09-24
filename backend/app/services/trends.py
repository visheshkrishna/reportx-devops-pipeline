from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import FindingFlag, Report, ReportFinding

NON_QUANTITATIVE_MARKER_NAME = {
    "date",
    "report date",
    "collection date",
    "collected date",
    "observation date",
    "sample date",
    "specimen date",
    "reported date",
    "dob",
    "birth date",
}


@dataclass(frozen=True)
class TrendPoint:
    report_id: str
    observed_at: datetime
    value: float
    unit: str | None
    flag: FindingFlag
    reference_low: float | None
    reference_high: float | None


@dataclass(frozen=True)
class BiomarkerTrend:
    biomarker_key: str
    display_name: str
    unit: str | None
    direction: str
    trend_note: str
    points: list[TrendPoint]


def _normalize_biomarker_key(value: str | None) -> str:
    return (value or "").strip().lower()


def _is_quantitative_finding(finding: ReportFinding) -> bool:
    if finding.value_numeric is None:
        return False

    name = _normalize_biomarker_key(finding.display_name or finding.biomarker_key)
    if not name:
        return False

    return name not in NON_QUANTITATIVE_MARKER_NAME


def _has_consistent_units(findings: list[tuple[ReportFinding, Report]]) -> bool:
    normalized_units = {
        (finding.unit or "").strip().lower()
        for finding, _ in findings
        if (finding.unit or "").strip()
    }
    return len(normalized_units) <= 1


def _flag_severity(flag: FindingFlag) -> int:
    if flag is FindingFlag.NORMAL:
        return 0
    if flag is FindingFlag.UNKNOWN:
        return 1
    return 2


def _distance_to_reference(value: float, low: float | None, high: float | None) -> float | None:
    if low is None or high is None:
        return None
    if low <= value <= high:
        return 0.0
    if value < low:
        return low - value
    return value - high


def _pick_best_finding(
    findings: list[tuple[ReportFinding, Report]],
) -> tuple[ReportFinding, Report]:
    """From multiple findings for the same (report, biomarker), pick the most clinically relevant one.

    Priority: highest flag severity → largest distance from reference range → latest position.
    """

    def sort_key(item: tuple[ReportFinding, Report]) -> tuple[int, float, int]:
        finding, _ = item
        severity = _flag_severity(finding.flag)
        numeric_value = float(finding.value_numeric) if finding.value_numeric is not None else 0.0
        distance = _distance_to_reference(
            numeric_value, finding.reference_low, finding.reference_high
        )
        return (severity, distance or 0.0, finding.position)

    return max(findings, key=sort_key)


def _classify_direction(previous: TrendPoint, current: TrendPoint) -> str:
    previous_severity = _flag_severity(previous.flag)
    current_severity = _flag_severity(current.flag)
    if current_severity < previous_severity:
        return "improving"
    if current_severity > previous_severity:
        return "worsening"

    delta = current.value - previous.value
    baseline = abs(previous.value)
    relative_delta = abs(delta) / baseline if baseline > 0 else abs(delta)
    if relative_delta <= 0.03:
        return "stable"

    previous_distance = _distance_to_reference(
        previous.value, previous.reference_low, previous.reference_high
    )
    current_distance = _distance_to_reference(
        current.value, current.reference_low, current.reference_high
    )
    if previous_distance is not None and current_distance is not None:
        if current_distance + 1e-9 < previous_distance:
            return "improving"
        if current_distance > previous_distance + 1e-9:
            return "worsening"
        return "stable"

    if current.flag is FindingFlag.HIGH:
        return "improving" if delta < 0 else "worsening"
    if current.flag is FindingFlag.LOW:
        return "improving" if delta > 0 else "worsening"
    if current.flag in {FindingFlag.ABNORMAL, FindingFlag.UNKNOWN, FindingFlag.NORMAL}:
        return "worsening" if relative_delta > 0.1 else "stable"

    return "stable"


def _build_trend_note(display_name: str, direction: str) -> str:
    if direction == "improving":
        return f"{display_name} trend appears to be improving compared with prior reports."
    if direction == "worsening":
        return f"{display_name} trend appears to be worsening compared with prior reports."
    return f"{display_name} trend appears stable across recent reports."


async def build_trends_for_patient(
    session: AsyncSession,
    *,
    subject_user_id: str,
) -> list[BiomarkerTrend]:
    rows = await session.execute(
        select(ReportFinding, Report)
        .join(Report, Report.id == ReportFinding.report_id)
        .where(Report.subject_user_id == subject_user_id)
        .order_by(Report.observed_at.asc(), Report.created_at.asc(), ReportFinding.position.asc())
    )

    grouped: dict[str, list[tuple[ReportFinding, Report]]] = {}
    for finding, report in rows.all():
        if not _is_quantitative_finding(finding):
            continue
        biomarker_key = _normalize_biomarker_key(finding.biomarker_key or finding.display_name)
        if not biomarker_key:
            continue
        grouped.setdefault(biomarker_key, []).append((finding, report))

    trends: list[BiomarkerTrend] = []
    for biomarker_key, entries in grouped.items():
        if not _has_consistent_units(entries):
            continue

        # Collapse to one finding per report, picking the most clinically relevant
        by_report: dict[str, list[tuple[ReportFinding, Report]]] = {}
        for finding, report in entries:
            by_report.setdefault(report.id, []).append((finding, report))
        deduped = [_pick_best_finding(items) for items in by_report.values()]
        deduped.sort(key=lambda item: (item[1].observed_at, item[1].created_at, item[0].position))

        if len(deduped) < 2:
            continue

        points = [
            TrendPoint(
                report_id=report.id,
                observed_at=report.observed_at,
                value=float(finding.value_numeric),
                unit=finding.unit,
                flag=finding.flag,
                reference_low=finding.reference_low,
                reference_high=finding.reference_high,
            )
            for finding, report in deduped
        ]
        previous, current = points[-2], points[-1]
        direction = _classify_direction(previous, current)

        latest_finding = deduped[-1][0]
        trends.append(
            BiomarkerTrend(
                biomarker_key=biomarker_key,
                display_name=latest_finding.display_name,
                unit=latest_finding.unit,
                direction=direction,
                trend_note=_build_trend_note(latest_finding.display_name, direction),
                points=points,
            )
        )

    trends.sort(key=lambda item: item.display_name.lower())
    return trends
