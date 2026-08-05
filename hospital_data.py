"""
Quarterly AIM hospital data loader.

Each input CSV is assumed to contain exactly one reporting quarter
(three consecutive months). The program builds Hospital objects, flags
INVALID hospitals, and scores selected AIM process measures.

PMH Hospital Data CSVs used for most measures are expected to have exactly
52 columns (same layout as:
"PMH Hospital Data for AIM (Jan - Mar 2026) - PMH Hospital Data for AIM (Jan - Mar 2026).csv").

Other AIM measures are sourced from separate quarterly files:
  - Birth Quality Data CSV -> respectful_equitable_care_provider_and_nursing_education
  - PMH Patient Data CSV   -> pmhc_treatment_referral
  - PMH table screening/education CSV -> pmhc_patient_education
"""

from __future__ import annotations

import csv
import calendar
import io
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

# Wider quarterly exports can contain long question-label cells.
try:
    csv.field_size_limit(sys.maxsize)
except OverflowError:
    csv.field_size_limit(2**31 - 1)

# Fixed width for the main PMH Hospital quarterly export used by most measures.
EXPECTED_PMH_HOSPITAL_COLUMN_COUNT = 52
# Fixed width for the Birth Quality quarterly export.
EXPECTED_BIRTH_QUALITY_COLUMN_COUNT = 9
# Fixed width for the PMH Patient Data quarterly export.
EXPECTED_PMH_PATIENT_COLUMN_COUNT = 18
# Fixed width for the PMH table screening/education export.
EXPECTED_SCREENING_EDUCATION_COLUMN_COUNT = 8

HOSPITAL_COLUMN_INDEX = 2  # Column C: "NNN - Hospital Name"
MONTH_COLUMN_INDEX = 3  # Column D: "Mon-YY"
WORKGROUP_COLUMN_INDEX = 4  # Column E: care_coordination_workgroup
SCREENING_TOOL_COLUMN_INDEX = 5  # Column F: mental_health_screening_tool_sharing
PROTOCOL_COLUMN_INDEX = 6  # Column G: mental_health_protocol
# Columns H-K: OB providers, OB nursing, ER providers, ER nurses education %
EDUCATION_COLUMN_INDEXES = (7, 8, 9, 10)
# Columns L-U: patient charts reviewed (denominator for screening measures)
CHART_REVIEW_DENOMINATOR_COLUMN_INDEXES = tuple(range(11, 21))
# Columns V-AE: prenatal depression screening documented (numerator)
DEPRESSION_NUMERATOR_COLUMN_INDEXES = tuple(range(21, 31))
# Columns AF-AO: prenatal anxiety screening documented (numerator)
ANXIETY_NUMERATOR_COLUMN_INDEXES = tuple(range(31, 41))
# Birth Quality columns E-H: respectful/equitable care education percentages
BIRTH_QUALITY_EDUCATION_COLUMN_INDEXES = (4, 5, 6, 7)
# PMH Patient Data columns O-P: treatment / referral responses
PATIENT_MEDICATION_COLUMN_INDEX = 14  # Column O
PATIENT_COUNSELING_COLUMN_INDEX = 15  # Column P
# PMH Patient Data race checkboxes F-M and insurance column N
PATIENT_RACE_WHITE_INDEX = 5
PATIENT_RACE_BLACK_INDEX = 6
PATIENT_RACE_HISPANIC_INDEX = 7
PATIENT_RACE_ASIAN_INDEX = 8
PATIENT_RACE_NATIVE_AMERICAN_INDEX = 9
PATIENT_RACE_NHPI_INDEX = 10
PATIENT_RACE_OTHER_INDEX = 11
PATIENT_RACE_NOT_REPORTED_INDEX = 12
PATIENT_INSURANCE_COLUMN_INDEX = 13

# PMH table screening/education columns
SCREENING_EDUCATION_HOSPITAL_ID_INDEX = 0  # Column A
SCREENING_EDUCATION_MONTH_INDEX = 1  # Column B
SCREENING_EDUCATION_STRATIFICATION_INDEX = 2  # Column C
SCREENING_EDUCATION_GROUP_INDEX = 3  # Column D
SCREENING_EDUCATION_NUMERATOR_INDEX = 5  # Column F
SCREENING_EDUCATION_DENOMINATOR_INDEX = 6  # Column G

TREATMENT_REFERRAL_RESPONSE_VALUES = {
    "yes",
    "no",
    "counseling documented but declined",
    "referral made but not scheduled",
    "warm handoff provided",
}

# Reporting order for pmhc_treatment_referral populations.
TREATMENT_REFERRAL_RACE_POPULATIONS = [
    "asian",
    "african_american",
    "hispanic",
    "native_american",
    "native_hawaiian_pacific_islander",
    "white",
    "other",
    "race_not_reported",
    "unknown",
]
TREATMENT_REFERRAL_INSURANCE_POPULATIONS = [
    "medicaid",
    "private",
    "other_public",
    "uninsured",
]
TREATMENT_REFERRAL_POPULATIONS = (
    ["all"]
    + TREATMENT_REFERRAL_RACE_POPULATIONS
    + TREATMENT_REFERRAL_INSURANCE_POPULATIONS
)

# Same population order is used for stratified pmhc_patient_education.
PATIENT_EDUCATION_POPULATIONS = TREATMENT_REFERRAL_POPULATIONS

# Map screening-education stratification+group pairs to AIM population names.
# Race/Ethnicity (non-Detailed) and Primary Language rows are ignored.
SCREENING_EDUCATION_POPULATION_MAP = {
    ("Race/Ethnicity (Detailed)", "Asian"): "asian",
    ("Race/Ethnicity (Detailed)", "Black/African American"): "african_american",
    ("Race/Ethnicity (Detailed)", "Hispanic/Latino"): "hispanic",
    ("Race/Ethnicity (Detailed)", "Other"): "other",
    ("Race/Ethnicity (Detailed)", "Race Not Documented"): "race_not_reported",
    ("Race/Ethnicity (Detailed)", "White"): "white",
    ("Insurance", "Private"): "private",
    # Source file only labels public insurance as "Public".
    ("Insurance", "Public"): "other_public",
}

# AIM upload template measure order (matches placeholder rows).
AIM_UPLOAD_MEASURE_ORDER = [
    "respectful_equitable_care_provider_and_nursing_education",
    "pmhc_provider_nursing_education",
    "care_coordination_workgroup",
    "mental_health_protocol",
    "mental_health_screening_tool_sharing",
    "pmhc_treatment_referral",
    "pmhc_patient_education",
    "prenatal_depression_screening",
    "prenatal_anxiety_screening",
]

# These measures fill the value column; numerator/denominator stay blank.
AIM_VALUE_MEASURES = {
    "respectful_equitable_care_provider_and_nursing_education",
    "pmhc_provider_nursing_education",
    "care_coordination_workgroup",
    "mental_health_protocol",
    "mental_health_screening_tool_sharing",
}

# These measures fill numerator/denominator; value stays blank.
AIM_NUM_DEN_MEASURES = {
    "prenatal_depression_screening",
    "prenatal_anxiety_screening",
}

# Stratified numerator/denominator measures (race + insurance populations).
AIM_STRATIFIED_NUM_DEN_MEASURES = {
    "pmhc_treatment_referral",
    "pmhc_patient_education",
}

AIM_UPLOAD_HEADER = [
    "Hospital ID",
    "hospital_unique_identifier",
    "period_start_date",
    "period_end_date",
    "measure",
    "population",
    "numerator",
    "denominator",
    "value",
]

# Built-in ILPQC Hospital ID -> AIM hospital_unique_identifier map.
HOSPITAL_ID_TO_IDENTIFIER = {
    "1": "1061",
    "2": "1106",
    "3": "1028",
    "4": "1004",
    "5": "1090",
    "6": "1042",
    "7": "1065",
    "10": "1051",
    "11": "1070",
    "12": "1114",
    "13": "1087",
    "15": "1012",
    "16": "1026",
    "17": "1085",
    "18": "1021",
    "19": "1093",
    "20": "1095",
    "21": "1007",
    "22": "1027",
    "24": "1025",
    "25": "1034",
    "27": "1077",
    "28": "1019",
    "34": "1088",
    "36": "1060",
    "37": "1080",
    "39": "1121",
    "40": "1099",
    "42": "1003",
    "44": "1063",
    "45": "1030",
    "46": "1116",
    "48": "1071",
    "51": "1100",
    "53": "1008",
    "54": "1041",
    "55": "1055",
    "56": "1006",
    "58": "1038",
    "59": "1083",
    "60": "1117",
    "61": "1118",
    "63": "1119",
    "64": "1069",
    "65": "1086",
    "66": "1029",
    "68": "1011",
    "69": "1018",
    "70": "1020",
    "71": "1062",
    "72": "1024",
    "73": "1023",
    "76": "1033",
    "77": "1091",
    "80": "1039",
    "81": "1044",
    "83": "1047",
    "85": "1052",
    "87": "1056",
    "89": "1059",
    "90": "1064",
    "91": "1066",
    "92": "1072",
    "95": "1076",
    "96": "1078",
    "98": "1082",
    "99": "1084",
    "103": "1105",
    "104": "1101",
    "105": "1040",
    "107": "1048",
    "109": "1089",
    "110": "1103",
    "112": "1016",
    "113": "1013",
    "114": "1049",
    "115": "1002",
    "119": "1005",
    "120": "1010",
    "121": "1035",
    "122": "1054",
    "123": "1092",
    "124": "1104",
    "125": "1075",
    "126": "1111",
}

# Shared AIM process-measure scoring (1 / 3 / 5)
PROCESS_RESPONSE_VALUES = {
    "haven't started": 1,
    "working on it": 3,
    "in place": 5,
}

# Measures that are scored from the 52-column PMH Hospital Data CSV.
PMH_HOSPITAL_MEASURES = {
    "pmhc_provider_nursing_education",
    "care_coordination_workgroup",
    "mental_health_protocol",
    "mental_health_screening_tool_sharing",
    "prenatal_depression_screening",
    "prenatal_anxiety_screening",
}

# Where each measure's source data lives.
MEASURE_DATA_SOURCES = {
    "pmhc_provider_nursing_education": "pmh_hospital",
    "care_coordination_workgroup": "pmh_hospital",
    "mental_health_protocol": "pmh_hospital",
    "mental_health_screening_tool_sharing": "pmh_hospital",
    "prenatal_depression_screening": "pmh_hospital",
    "prenatal_anxiety_screening": "pmh_hospital",
    "respectful_equitable_care_provider_and_nursing_education": "birth_quality",
    "pmhc_treatment_referral": "pmh_patient",
    "pmhc_patient_education": "screening_education",
}


class CSVDataError(ValueError):
    """Raised when the CSV does not match the expected quarterly structure."""


class Hospital:
    """One hospital and its submissions for a single reporting quarter."""

    VALID_STATES = {"Valid", "Invalid"}

    def __init__(
        self,
        ID: str,
        entry_num: int,
        state: str,
        name: str = "",
        months: list[datetime] | None = None,
        invalid_reasons: list[str] | None = None,
    ) -> None:
        if state not in self.VALID_STATES:
            raise ValueError("state may only be 'Valid' or 'Invalid'")

        self.ID = ID
        self.entry_num = entry_num
        self.state = state
        self.name = name
        self.months = sorted(months or [])
        self.invalid_reasons = invalid_reasons or []

    @staticmethod
    def find_hospital_id(hospital_value: str) -> str:
        """Return the first three digits before the hospital name in column C."""
        match = re.match(r"^\s*(\d{3})\s*-", hospital_value)
        if match is None:
            raise CSVDataError(
                f"Could not find a three-digit hospital ID in {hospital_value!r}. "
                "Expected a value such as '042 - Example Hospital'."
            )
        return match.group(1)

    @staticmethod
    def find_hospital_name(hospital_value: str) -> str:
        """Return the hospital name that follows the ID in column C."""
        match = re.match(r"^\s*\d{3}\s*-\s*(.+?)\s*$", hospital_value)
        if match is None or not match.group(1):
            raise CSVDataError(
                f"Could not find a hospital name in {hospital_value!r}."
            )
        return match.group(1)

    def __str__(self) -> str:
        month_text = ", ".join(month.strftime("%B %Y") for month in self.months)
        lines = [
            f"Hospital: {self.name}",
            f"ID: {self.ID}",
            f"State: {self.state}",
            f"Number of data entries: {self.entry_num}",
            f"Months: {month_text or 'None'}",
        ]
        if self.invalid_reasons:
            lines.append("Invalid reason(s): " + "; ".join(self.invalid_reasons))
        return "\n".join(lines)


def prompt_for_csv(
    prompt_message: str = "Enter the path to the quarterly CSV file: ",
) -> Path:
    """Prompt until the user provides a path to an existing CSV file."""
    while True:
        entered = input(prompt_message).strip()
        entered = entered.strip('"').strip("'")
        csv_path = Path(entered).expanduser()

        if csv_path.suffix.lower() != ".csv":
            print("Please select a file ending in .csv.")
        elif not csv_path.is_file():
            print(f"File not found: {csv_path}")
        else:
            return csv_path


def prompt_for_csv_optional(
    prompt_message: str,
) -> Path | None:
    """Prompt for an optional CSV path; empty input skips."""
    while True:
        entered = input(prompt_message).strip().strip('"').strip("'")
        if not entered:
            return None
        csv_path = Path(entered).expanduser()
        if csv_path.suffix.lower() != ".csv":
            print("Please select a file ending in .csv, or press Enter to skip.")
        elif not csv_path.is_file():
            print(f"File not found: {csv_path}")
        else:
            return csv_path


def resolve_csv_path(csv_path: str | Path | None = None) -> Path:
    """Return one CSV path for this program run (prompt only if needed)."""
    if csv_path is None:
        return prompt_for_csv()

    cleaned = str(csv_path).strip().strip('"').strip("'")
    selected_path = Path(cleaned).expanduser()

    if selected_path.suffix.lower() != ".csv":
        raise CSVDataError("Please select a file ending in .csv.")
    if not selected_path.is_file():
        raise FileNotFoundError(f"File not found: {selected_path}")

    return selected_path


def parse_month(month_value: str, row_number: int) -> datetime:
    """Convert a value such as Jan-26 into a datetime for sorting."""
    try:
        return datetime.strptime(month_value.strip(), "%b-%y")
    except ValueError as error:
        raise CSVDataError(
            f"Row {row_number} contains an invalid reporting month "
            f"{month_value!r}. Expected a value such as 'Jan-26'."
        ) from error


def format_quarter(quarter: list[datetime]) -> str:
    return (
        f"{quarter[0].strftime('%B %Y')} through {quarter[-1].strftime('%B %Y')}"
    )


def validate_quarter(months: list[datetime]) -> list[datetime]:
    """Confirm the file contains exactly three consecutive months."""
    unique_months = sorted({(month.year, month.month) for month in months})

    if len(unique_months) != 3:
        raise CSVDataError(
            "A quarterly file must contain exactly three consecutive months. "
            f"This file contains {len(unique_months)} distinct month(s)."
        )

    encoded = [year * 12 + month for year, month in unique_months]
    if encoded[1] != encoded[0] + 1 or encoded[2] != encoded[1] + 1:
        readable = ", ".join(
            datetime(year, month, 1).strftime("%B %Y")
            for year, month in unique_months
        )
        raise CSVDataError(
            "The file's three reporting months are not consecutive: " + readable
        )

    return [datetime(year, month, 1) for year, month in unique_months]


def validate_pmh_hospital_csv_shape(header: list[str]) -> None:
    """Require the fixed 52-column PMH Hospital Data layout."""
    column_count = len(header)
    if column_count != EXPECTED_PMH_HOSPITAL_COLUMN_COUNT:
        raise CSVDataError(
            "PMH Hospital Data CSVs must have exactly "
            f"{EXPECTED_PMH_HOSPITAL_COLUMN_COUNT} columns "
            f"(found {column_count})."
        )


def iter_data_rows(csv_path: Path, minimum_columns: int):
    """Yield usable data rows from a 52-column PMH Hospital quarterly CSV."""
    with csv_path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.reader(csv_file)
        header = next(reader, None)

        if header is None:
            raise CSVDataError("The selected CSV file is empty.")

        validate_pmh_hospital_csv_shape(header)
        if minimum_columns > EXPECTED_PMH_HOSPITAL_COLUMN_COUNT:
            raise CSVDataError(
                f"Requested {minimum_columns} columns, but PMH Hospital Data "
                f"CSVs only have {EXPECTED_PMH_HOSPITAL_COLUMN_COUNT}."
            )

        for row_number, row in enumerate(reader, start=2):
            if not row or not any(cell.strip() for cell in row):
                continue

            # Some exports omit trailing empty cells; pad to the fixed width.
            if len(row) < EXPECTED_PMH_HOSPITAL_COLUMN_COUNT:
                row = row + [""] * (EXPECTED_PMH_HOSPITAL_COLUMN_COUNT - len(row))
            elif len(row) > EXPECTED_PMH_HOSPITAL_COLUMN_COUNT:
                raise CSVDataError(
                    f"Row {row_number} has {len(row)} columns; expected "
                    f"{EXPECTED_PMH_HOSPITAL_COLUMN_COUNT}."
                )

            hospital_value = row[HOSPITAL_COLUMN_INDEX].strip()
            month_value = row[MONTH_COLUMN_INDEX].strip()
            if not hospital_value or not month_value:
                continue

            hospital_id = Hospital.find_hospital_id(hospital_value)
            hospital_name = Hospital.find_hospital_name(hospital_value)
            month = parse_month(month_value, row_number)
            yield row_number, row, hospital_id, hospital_name, month


def validate_birth_quality_csv_shape(header: list[str]) -> None:
    """Require the fixed 9-column Birth Quality Data layout."""
    column_count = len(header)
    if column_count != EXPECTED_BIRTH_QUALITY_COLUMN_COUNT:
        raise CSVDataError(
            "Birth Quality Data CSVs must have exactly "
            f"{EXPECTED_BIRTH_QUALITY_COLUMN_COUNT} columns "
            f"(found {column_count})."
        )


def iter_birth_quality_rows(csv_path: Path):
    """Yield usable data rows from a 9-column Birth Quality quarterly CSV."""
    with csv_path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.reader(csv_file)
        header = next(reader, None)

        if header is None:
            raise CSVDataError("The selected CSV file is empty.")

        validate_birth_quality_csv_shape(header)

        for row_number, row in enumerate(reader, start=2):
            if not row or not any(cell.strip() for cell in row):
                continue

            if len(row) < EXPECTED_BIRTH_QUALITY_COLUMN_COUNT:
                row = row + [""] * (EXPECTED_BIRTH_QUALITY_COLUMN_COUNT - len(row))
            elif len(row) > EXPECTED_BIRTH_QUALITY_COLUMN_COUNT:
                raise CSVDataError(
                    f"Row {row_number} has {len(row)} columns; expected "
                    f"{EXPECTED_BIRTH_QUALITY_COLUMN_COUNT}."
                )

            hospital_value = row[HOSPITAL_COLUMN_INDEX].strip()
            month_value = row[MONTH_COLUMN_INDEX].strip()
            if not hospital_value or not month_value:
                continue

            hospital_id = Hospital.find_hospital_id(hospital_value)
            hospital_name = Hospital.find_hospital_name(hospital_value)
            month = parse_month(month_value, row_number)
            yield row_number, row, hospital_id, hospital_name, month


def validate_pmh_patient_csv_shape(header: list[str]) -> None:
    """Require the fixed 18-column PMH Patient Data layout."""
    column_count = len(header)
    if column_count != EXPECTED_PMH_PATIENT_COLUMN_COUNT:
        raise CSVDataError(
            "PMH Patient Data CSVs must have exactly "
            f"{EXPECTED_PMH_PATIENT_COLUMN_COUNT} columns "
            f"(found {column_count})."
        )


def iter_patient_data_rows(csv_path: Path):
    """Yield usable patient-level rows from an 18-column PMH Patient CSV."""
    with csv_path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.reader(csv_file)
        header = next(reader, None)

        if header is None:
            raise CSVDataError("The selected CSV file is empty.")

        validate_pmh_patient_csv_shape(header)

        for row_number, row in enumerate(reader, start=2):
            if not row or not any(cell.strip() for cell in row):
                continue

            if len(row) < EXPECTED_PMH_PATIENT_COLUMN_COUNT:
                row = row + [""] * (EXPECTED_PMH_PATIENT_COLUMN_COUNT - len(row))
            elif len(row) > EXPECTED_PMH_PATIENT_COLUMN_COUNT:
                raise CSVDataError(
                    f"Row {row_number} has {len(row)} columns; expected "
                    f"{EXPECTED_PMH_PATIENT_COLUMN_COUNT}."
                )

            hospital_value = row[HOSPITAL_COLUMN_INDEX].strip()
            month_value = row[MONTH_COLUMN_INDEX].strip()
            if not hospital_value or not month_value:
                continue

            hospital_id = Hospital.find_hospital_id(hospital_value)
            hospital_name = Hospital.find_hospital_name(hospital_value)
            month = parse_month(month_value, row_number)
            yield row_number, row, hospital_id, hospital_name, month


def parse_screening_education_month(month_value: str, row_number: int) -> datetime:
    """Parse screening-education months such as 'Jan 2026' or 'Jan-26'."""
    cleaned = month_value.strip()
    for fmt in ("%b %Y", "%b-%y", "%b-%Y"):
        try:
            parsed = datetime.strptime(cleaned, fmt)
            return datetime(parsed.year, parsed.month, 1)
        except ValueError:
            continue
    raise CSVDataError(
        f"Row {row_number} contains an invalid reporting month "
        f"{month_value!r}. Expected a value such as 'Jan 2026'."
    )


def validate_screening_education_csv_shape(header: list[str]) -> None:
    """Require the fixed 8-column PMH table screening/education layout."""
    column_count = len(header)
    if column_count != EXPECTED_SCREENING_EDUCATION_COLUMN_COUNT:
        raise CSVDataError(
            "PMH table screening/education CSVs must have exactly "
            f"{EXPECTED_SCREENING_EDUCATION_COLUMN_COUNT} columns "
            f"(found {column_count})."
        )


def iter_screening_education_rows(csv_path: Path):
    """Yield usable rows from an 8-column screening/education CSV."""
    with csv_path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.reader(csv_file)
        header = next(reader, None)

        if header is None:
            raise CSVDataError("The selected CSV file is empty.")

        validate_screening_education_csv_shape(header)

        for row_number, row in enumerate(reader, start=2):
            if not row or not any(cell.strip() for cell in row):
                continue

            if len(row) < EXPECTED_SCREENING_EDUCATION_COLUMN_COUNT:
                row = row + [""] * (
                    EXPECTED_SCREENING_EDUCATION_COLUMN_COUNT - len(row)
                )
            elif len(row) > EXPECTED_SCREENING_EDUCATION_COLUMN_COUNT:
                raise CSVDataError(
                    f"Row {row_number} has {len(row)} columns; expected "
                    f"{EXPECTED_SCREENING_EDUCATION_COLUMN_COUNT}."
                )

            hospital_id = row[SCREENING_EDUCATION_HOSPITAL_ID_INDEX].strip()
            month_value = row[SCREENING_EDUCATION_MONTH_INDEX].strip()
            if not hospital_id or not month_value:
                continue

            month = parse_screening_education_month(month_value, row_number)
            yield row_number, row, hospital_id, month


def parse_non_negative_int(
    value: str,
    row_number: int,
    column_letter: str,
) -> int:
    """Parse a blank-or-integer count cell; blank means 0."""
    cleaned = value.strip()
    if not cleaned:
        return 0
    try:
        number = int(float(cleaned))
    except ValueError as error:
        raise CSVDataError(
            f"Row {row_number} column {column_letter} has an invalid count "
            f"{value!r}."
        ) from error
    if number < 0:
        raise CSVDataError(
            f"Row {row_number} column {column_letter} count {value!r} "
            "must be zero or greater."
        )
    return number


def parse_treatment_referral_response(
    value: str,
    row_number: int,
    column_letter: str,
) -> str:
    """Validate and normalize a treatment/referral response from column O or P."""
    cleaned = value.strip()
    if not cleaned:
        raise CSVDataError(
            f"Row {row_number} column {column_letter} is blank. "
            "Expected one of: Yes, No, Counseling documented but declined, "
            "Referral made but not scheduled, Warm handoff provided."
        )

    normalized = cleaned.casefold()
    if normalized not in TREATMENT_REFERRAL_RESPONSE_VALUES:
        raise CSVDataError(
            f"Row {row_number} column {column_letter} has unexpected response "
            f"{value!r}. Expected one of: Yes, No, Counseling documented but "
            "declined, Referral made but not scheduled, Warm handoff provided."
        )
    return normalized


def load_hospitals(csv_path: Path) -> tuple[list[Hospital], list[datetime]]:
    """Read one quarterly CSV and initialize Hospital objects."""
    entries_by_id: dict[str, list[datetime]] = defaultdict(list)
    names_by_id: dict[str, set[str]] = defaultdict(set)
    all_months: list[datetime] = []

    for _row_number, _row, hospital_id, hospital_name, month in iter_data_rows(
        csv_path,
        minimum_columns=4,
    ):
        entries_by_id[hospital_id].append(month)
        names_by_id[hospital_id].add(hospital_name)
        all_months.append(month)

    if not all_months:
        raise CSVDataError("The selected CSV contains no hospital data rows.")

    quarter = validate_quarter(all_months)

    hospitals: list[Hospital] = []
    for hospital_id in sorted(entries_by_id, key=int):
        months = entries_by_id[hospital_id]
        names = names_by_id[hospital_id]
        reasons: list[str] = []

        # More than 3 entries, or the same month twice, means INVALID.
        if len(months) > 3:
            reasons.append(f"has {len(months)} entries; the maximum is 3")

        month_counts = Counter((month.year, month.month) for month in months)
        duplicate_months = [
            datetime(year, month, 1).strftime("%B %Y")
            for (year, month), count in sorted(month_counts.items())
            if count > 1
        ]
        if duplicate_months:
            reasons.append(
                "contains duplicate data for " + ", ".join(duplicate_months)
            )

        if len(names) > 1:
            reasons.append(
                "the same ID is associated with multiple hospital names: "
                + ", ".join(sorted(names))
            )

        state = "Invalid" if reasons else "Valid"
        hospitals.append(
            Hospital(
                ID=hospital_id,
                entry_num=len(months),
                state=state,
                name=" / ".join(sorted(names)),
                months=months,
                invalid_reasons=reasons,
            )
        )

    return hospitals, quarter


def print_invalid_hospitals(hospitals: list[Hospital]) -> None:
    """Report INVALID hospitals with entries, months, and validity."""
    invalid = [hospital for hospital in hospitals if hospital.state == "Invalid"]

    if not invalid:
        print("\nNo invalid hospitals were found.")
        return

    print("\nThe following hospitals are INVALID:")
    for index, hospital in enumerate(invalid):
        if index:
            print("-" * 40)
        print(hospital)


def run_hospital_importer(
    csv_path: str | Path | None = None,
    show_full_report: bool = False,
    report_invalids: bool = True,
) -> tuple[list[Hospital], Path]:
    """Load the quarterly CSV and optionally report INVALID hospitals."""
    selected_path = resolve_csv_path(csv_path)
    hospitals, quarter = load_hospitals(selected_path)

    print(f"\nReporting quarter: {format_quarter(quarter)}")
    print(f"Hospitals found: {len(hospitals)}")

    if show_full_report:
        print("\nHOSPITAL REPORT")
        print("=" * 60)
        for index, hospital in enumerate(hospitals):
            if index:
                print("-" * 60)
            print(hospital)

    if report_invalids:
        print_invalid_hospitals(hospitals)

    return hospitals, selected_path


def format_hospital_id_for_upload(hospital_id: str) -> str:
    """Format a hospital ID the way the AIM upload sheet expects (no leading zeros)."""
    return str(int(hospital_id))


def invalid_hospital_id_set(hospitals: list[Hospital]) -> set[str]:
    """Return upload-formatted IDs for hospitals marked Invalid."""
    return {
        format_hospital_id_for_upload(hospital.ID)
        for hospital in hospitals
        if hospital.state == "Invalid"
    }


def notify_invalid_hospitals_omitted(invalid_ids: set[str]) -> None:
    """Tell the user when Invalid hospitals were left out of reporting."""
    if not invalid_ids:
        return
    sorted_ids = ", ".join(sorted(invalid_ids, key=int))
    print(
        f"\nNote: Invalid hospital(s) omitted from this report: {sorted_ids}."
    )


def filter_value_measure_results(
    hospital_ids: list[str],
    values: list[int],
    invalid_ids: set[str],
) -> tuple[list[str], list[int]]:
    """Drop Invalid hospitals from a value-measure result pair."""
    kept_ids: list[str] = []
    kept_values: list[int] = []
    for hospital_id, value in zip(hospital_ids, values):
        if hospital_id in invalid_ids:
            continue
        kept_ids.append(hospital_id)
        kept_values.append(value)
    return kept_ids, kept_values


def filter_num_den_measure_results(
    hospital_ids: list[str],
    numerators: list[int],
    denominators: list[int],
    invalid_ids: set[str],
) -> tuple[list[str], list[int], list[int]]:
    """Drop Invalid hospitals from a numerator/denominator result."""
    kept_ids: list[str] = []
    kept_nums: list[int] = []
    kept_dens: list[int] = []
    for hospital_id, numerator, denominator in zip(
        hospital_ids, numerators, denominators
    ):
        if hospital_id in invalid_ids:
            continue
        kept_ids.append(hospital_id)
        kept_nums.append(numerator)
        kept_dens.append(denominator)
    return kept_ids, kept_nums, kept_dens


def filter_treatment_referral_results(
    results: dict[str, tuple[list[str], list[int], list[int]]],
    invalid_ids: set[str],
) -> dict[str, tuple[list[str], list[int], list[int]]]:
    """Drop Invalid hospitals from every treatment-referral population."""
    filtered: dict[str, tuple[list[str], list[int], list[int]]] = {}
    for population, (hospital_ids, numerators, denominators) in results.items():
        kept = filter_num_den_measure_results(
            hospital_ids, numerators, denominators, invalid_ids
        )
        if kept[0]:
            filtered[population] = kept
    return filtered


def parse_percentage(value: str, row_number: int, column_letter: str) -> float:
    """Parse a percentage cell such as '40%' into 40.0."""
    cleaned = value.strip().rstrip("%").strip()
    try:
        percent = float(cleaned)
    except ValueError as error:
        raise CSVDataError(
            f"Row {row_number} column {column_letter} has an invalid percentage "
            f"{value!r}."
        ) from error

    if percent < 0 or percent > 100:
        raise CSVDataError(
            f"Row {row_number} column {column_letter} percentage {value!r} "
            "must be between 0% and 100%."
        )
    return percent


def format_percentage_midpoint(average: float) -> int:
    """Map an average percent to the AIM 10% midpoint estimate.

    0-9% -> 5, 10-19% -> 15, ..., 80-89% -> 85, 90-100% -> 95
    """
    if average >= 90:
        return 95
    return int(average // 10) * 10 + 5


def print_measure_columns(
    measure_name: str,
    hospital_ids: list[str],
    measure_values: list[int],
    silent: bool = False,
) -> None:
    """Print two separate copyable columns for pasting into a table/Excel."""
    if silent:
        return

    print(f"\n{measure_name}")
    print("=" * 40)
    print(
        "Copy each block below into its own spreadsheet column "
        "(Hospital ID, then value)."
    )

    print("\nHospital ID")
    print("-" * 12)
    for hospital_id in hospital_ids:
        print(hospital_id)

    print("\nvalue")
    print("-" * 12)
    for measure_value in measure_values:
        print(measure_value)


def print_numerator_denominator_columns(
    measure_name: str,
    hospital_ids: list[str],
    numerators: list[int],
    denominators: list[int],
    silent: bool = False,
) -> None:
    """Print Hospital ID, numerator, and denominator as separate copyable lists."""
    if silent:
        return

    print(f"\n{measure_name}")
    print("=" * 40)
    print(
        "Copy each block below into its own spreadsheet column "
        "(Hospital ID, numerator, then denominator)."
    )

    print("\nHospital ID")
    print("-" * 12)
    for hospital_id in hospital_ids:
        print(hospital_id)

    print("\nnumerator")
    print("-" * 12)
    for numerator in numerators:
        print(numerator)

    print("\ndenominator")
    print("-" * 12)
    for denominator in denominators:
        print(denominator)


def count_checked_cells(
    row: list[str],
    column_indexes: tuple[int, ...],
    row_number: int,
    column_range_label: str,
) -> int:
    """Count Checked values in the given columns; Unchecked adds nothing."""
    checked_count = 0

    for column_index in column_indexes:
        cell = row[column_index].strip()
        if not cell:
            continue

        normalized = cell.casefold()
        if normalized == "checked":
            checked_count += 1
        elif normalized == "unchecked":
            continue
        else:
            raise CSVDataError(
                f"Row {row_number} in columns {column_range_label} has an unexpected "
                f"checkbox value {cell!r}. Expected 'Checked' or 'Unchecked'."
            )

    return checked_count


def score_process_measure(
    csv_path: str | Path,
    measure_name: str,
    response_column_index: int,
    column_letter: str,
    silent: bool = False,
) -> tuple[list[str], list[int]]:
    """Score a 1/3/5 process measure from one response column.

    Uses only the most recent month for each hospital and prints two copyable
    lists (Hospital ID and value).
    """
    selected_path = resolve_csv_path(csv_path)
    latest_by_id: dict[str, dict[str, object]] = {}
    all_months: list[datetime] = []

    for row_number, row, hospital_id, _hospital_name, month in iter_data_rows(
        selected_path,
        minimum_columns=response_column_index + 1,
    ):
        all_months.append(month)
        response = row[response_column_index].strip().lower()
        if not response:
            continue

        measure_value = PROCESS_RESPONSE_VALUES.get(response)
        if measure_value is None:
            raise CSVDataError(
                f"Row {row_number} has an unknown {measure_name} response "
                f"{row[response_column_index]!r}. Expected one of: "
                "Haven't started, Working on it, In Place."
            )

        current = latest_by_id.get(hospital_id)
        if current is None or month > current["month"]:
            latest_by_id[hospital_id] = {
                "month": month,
                "value": measure_value,
            }

    if not all_months:
        raise CSVDataError("The selected CSV contains no hospital data rows.")
    validate_quarter(all_months)

    if not latest_by_id:
        raise CSVDataError(f"No {measure_name} responses were found in column {column_letter}.")

    sorted_ids = sorted(latest_by_id, key=int)
    hospital_ids = [
        format_hospital_id_for_upload(hospital_id) for hospital_id in sorted_ids
    ]
    measure_values = [
        int(latest_by_id[hospital_id]["value"]) for hospital_id in sorted_ids
    ]

    print_measure_columns(measure_name, hospital_ids, measure_values, silent=silent)
    return hospital_ids, measure_values


def care_coordination_workgroup(
    csv_path: str | Path,
    silent: bool = False,
) -> tuple[list[str], list[int]]:
    """Score care_coordination_workgroup from column E (most recent month only)."""
    return score_process_measure(
        csv_path=csv_path,
        measure_name="care_coordination_workgroup",
        response_column_index=WORKGROUP_COLUMN_INDEX,
        column_letter="E",
        silent=silent,
    )


def respectful_equitable_care_provider_and_nursing_education(
    csv_path: str | Path,
    silent: bool = False,
) -> tuple[list[str], list[int]]:
    """Score respectful_equitable_care_provider_and_nursing_education.

    Reads Birth Quality Data columns E-H. For each hospital, average every
    non-empty percentage across those four columns and all reported months.
    Blank cells are skipped (not treated as 0%). Map the average to the AIM
    midpoint estimate (0-9% -> 5, ..., 90-100% -> 95).
    """
    selected_path = resolve_csv_path(csv_path)
    values_by_id: dict[str, list[float]] = defaultdict(list)
    all_months: list[datetime] = []
    column_letters = ("E", "F", "G", "H")

    for row_number, row, hospital_id, _hospital_name, month in iter_birth_quality_rows(
        selected_path
    ):
        all_months.append(month)
        for column_index, column_letter in zip(
            BIRTH_QUALITY_EDUCATION_COLUMN_INDEXES,
            column_letters,
        ):
            cell = row[column_index].strip()
            if not cell:
                continue
            values_by_id[hospital_id].append(
                parse_percentage(cell, row_number, column_letter)
            )

    if not all_months:
        raise CSVDataError("The selected CSV contains no hospital data rows.")
    validate_quarter(all_months)

    if not values_by_id:
        raise CSVDataError(
            "No respectful_equitable_care_provider_and_nursing_education "
            "percentages were found in columns E-H."
        )

    hospital_ids: list[str] = []
    measure_values: list[int] = []
    for hospital_id in sorted(values_by_id, key=int):
        percentages = values_by_id[hospital_id]
        average = sum(percentages) / len(percentages)
        hospital_ids.append(format_hospital_id_for_upload(hospital_id))
        measure_values.append(format_percentage_midpoint(average))

    print_measure_columns(
        "respectful_equitable_care_provider_and_nursing_education",
        hospital_ids,
        measure_values,
        silent=silent,
    )
    return hospital_ids, measure_values


def pmhc_provider_nursing_education(
    csv_path: str | Path,
    silent: bool = False,
) -> tuple[list[str], list[int]]:
    """Score pmhc_provider_nursing_education from columns H-K.

    For each hospital, average every reported education percentage across the
    four staff groups and all available monthly rows (ideally 12 values:
    4 columns x 3 months):
        H - % of OB providers with PMH training completed
        I - % of OB nursing staff with PMH training completed
        J - % of ER providers with PMH training completed
        K - % of ER nurses with PMH training completed

    Map that average to the AIM midpoint estimate
    (0-9% -> 5, ..., 90-100% -> 95). Values are reported without a % symbol.
    """
    selected_path = resolve_csv_path(csv_path)
    values_by_id: dict[str, list[float]] = defaultdict(list)
    all_months: list[datetime] = []
    column_letters = ("H", "I", "J", "K")

    for row_number, row, hospital_id, _hospital_name, month in iter_data_rows(
        selected_path,
        minimum_columns=max(EDUCATION_COLUMN_INDEXES) + 1,
    ):
        all_months.append(month)
        for column_index, column_letter in zip(
            EDUCATION_COLUMN_INDEXES,
            column_letters,
        ):
            cell = row[column_index].strip()
            if not cell:
                continue
            values_by_id[hospital_id].append(
                parse_percentage(cell, row_number, column_letter)
            )

    if not all_months:
        raise CSVDataError("The selected CSV contains no hospital data rows.")
    validate_quarter(all_months)

    if not values_by_id:
        raise CSVDataError(
            "No pmhc_provider_nursing_education percentages were found in columns H-K."
        )

    hospital_ids: list[str] = []
    measure_values: list[int] = []
    for hospital_id in sorted(values_by_id, key=int):
        percentages = values_by_id[hospital_id]
        average = sum(percentages) / len(percentages)
        hospital_ids.append(format_hospital_id_for_upload(hospital_id))
        measure_values.append(format_percentage_midpoint(average))

    print_measure_columns(
        "pmhc_provider_nursing_education",
        hospital_ids,
        measure_values,
        silent=silent,
    )
    return hospital_ids, measure_values


def mental_health_protocol(
    csv_path: str | Path,
    silent: bool = False,
) -> tuple[list[str], list[int]]:
    """Score mental_health_protocol from column G (most recent month only)."""
    return score_process_measure(
        csv_path=csv_path,
        measure_name="mental_health_protocol",
        response_column_index=PROTOCOL_COLUMN_INDEX,
        column_letter="G",
        silent=silent,
    )


def mental_health_screening_tool_sharing(
    csv_path: str | Path,
    silent: bool = False,
) -> tuple[list[str], list[int]]:
    """Score mental_health_screening_tool_sharing from column F (most recent month only)."""
    return score_process_measure(
        csv_path=csv_path,
        measure_name="mental_health_screening_tool_sharing",
        response_column_index=SCREENING_TOOL_COLUMN_INDEX,
        column_letter="F",
        silent=silent,
    )


def pmhc_patient_education(
    csv_path: str | Path,
    silent: bool = False,
) -> dict[str, tuple[list[str], list[int], list[int]]]:
    """Score pmhc_patient_education from the PMH table screening/education CSV.

    Sums numerator (column F) and denominator (column G) across the quarter for
    each hospital and population. Populations match pmhc_treatment_referral:
    all, race/ethnicity, and insurance. Race rows use Race/Ethnicity (Detailed)
    only. Population \"all\" is the sum of those detailed race groups.
    """
    selected_path = resolve_csv_path(csv_path)
    numerators: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    denominators: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    all_months: list[datetime] = []

    for row_number, row, hospital_id, month in iter_screening_education_rows(
        selected_path
    ):
        all_months.append(month)
        stratification = row[SCREENING_EDUCATION_STRATIFICATION_INDEX].strip()
        group = row[SCREENING_EDUCATION_GROUP_INDEX].strip()
        population = SCREENING_EDUCATION_POPULATION_MAP.get((stratification, group))
        if population is None:
            continue

        numerator = parse_non_negative_int(
            row[SCREENING_EDUCATION_NUMERATOR_INDEX],
            row_number,
            "F",
        )
        denominator = parse_non_negative_int(
            row[SCREENING_EDUCATION_DENOMINATOR_INDEX],
            row_number,
            "G",
        )

        numerators[population][hospital_id] += numerator
        denominators[population][hospital_id] += denominator

        if population in TREATMENT_REFERRAL_RACE_POPULATIONS:
            numerators["all"][hospital_id] += numerator
            denominators["all"][hospital_id] += denominator

    if not all_months:
        raise CSVDataError("The selected CSV contains no screening/education rows.")
    validate_quarter(all_months)

    results: dict[str, tuple[list[str], list[int], list[int]]] = {}
    for population in PATIENT_EDUCATION_POPULATIONS:
        hospital_counts = denominators.get(population, {})
        if not hospital_counts:
            continue

        sorted_ids = sorted(hospital_counts, key=int)
        hospital_ids = [
            format_hospital_id_for_upload(hospital_id) for hospital_id in sorted_ids
        ]
        population_numerators = [
            numerators[population][hospital_id] for hospital_id in sorted_ids
        ]
        population_denominators = [
            hospital_counts[hospital_id] for hospital_id in sorted_ids
        ]

        # Omit hospitals with denominator 0 for this population.
        kept_ids: list[str] = []
        kept_nums: list[int] = []
        kept_dens: list[int] = []
        for hospital_id, numerator, denominator in zip(
            hospital_ids,
            population_numerators,
            population_denominators,
        ):
            if denominator == 0:
                continue
            kept_ids.append(hospital_id)
            kept_nums.append(numerator)
            kept_dens.append(denominator)

        if not kept_ids:
            continue

        print_numerator_denominator_columns(
            f"pmhc_patient_education (population: {population})",
            kept_ids,
            kept_nums,
            kept_dens,
            silent=silent,
        )
        results[population] = (kept_ids, kept_nums, kept_dens)

    return results


def score_checkbox_screening_measure(
    csv_path: str | Path,
    measure_name: str,
    numerator_column_indexes: tuple[int, ...],
    numerator_column_label: str,
    silent: bool = False,
) -> tuple[list[str], list[int], list[int]]:
    """Score a checkbox screening measure with shared L-U denominators.

    Denominator (L-U): total Checked patient charts reviewed across the quarter.
    Numerator: total Checked values in the provided numerator columns.

    Returns three parallel lists: Hospital IDs, numerators, and denominators.
    """
    selected_path = resolve_csv_path(csv_path)
    numerators_by_id: dict[str, int] = defaultdict(int)
    denominators_by_id: dict[str, int] = defaultdict(int)
    all_months: list[datetime] = []

    for row_number, row, hospital_id, _hospital_name, month in iter_data_rows(
        selected_path,
        minimum_columns=max(numerator_column_indexes) + 1,
    ):
        all_months.append(month)
        denominators_by_id[hospital_id] += count_checked_cells(
            row,
            CHART_REVIEW_DENOMINATOR_COLUMN_INDEXES,
            row_number,
            "L-U",
        )
        numerators_by_id[hospital_id] += count_checked_cells(
            row,
            numerator_column_indexes,
            row_number,
            numerator_column_label,
        )

    if not all_months:
        raise CSVDataError("The selected CSV contains no hospital data rows.")
    validate_quarter(all_months)

    sorted_ids = sorted(denominators_by_id, key=int)
    hospital_ids = [
        format_hospital_id_for_upload(hospital_id) for hospital_id in sorted_ids
    ]
    numerators = [numerators_by_id[hospital_id] for hospital_id in sorted_ids]
    denominators = [denominators_by_id[hospital_id] for hospital_id in sorted_ids]

    print_numerator_denominator_columns(
        measure_name,
        hospital_ids,
        numerators,
        denominators,
        silent=silent,
    )
    return hospital_ids, numerators, denominators


def prenatal_depression_screening(
    csv_path: str | Path,
    silent: bool = False,
) -> tuple[list[str], list[int], list[int]]:
    """Score prenatal_depression_screening from checkbox columns L-U and V-AE."""
    return score_checkbox_screening_measure(
        csv_path=csv_path,
        measure_name="prenatal_depression_screening",
        numerator_column_indexes=DEPRESSION_NUMERATOR_COLUMN_INDEXES,
        numerator_column_label="V-AE",
        silent=silent,
    )


def prenatal_anxiety_screening(
    csv_path: str | Path,
    silent: bool = False,
) -> tuple[list[str], list[int], list[int]]:
    """Score prenatal_anxiety_screening from checkbox columns L-U and AF-AO.

    Denominator (L-U): total Checked patient charts reviewed across the quarter.
    Numerator (AF-AO): total Checked patients with documented prenatal anxiety
    screening across the quarter.
    """
    return score_checkbox_screening_measure(
        csv_path=csv_path,
        measure_name="prenatal_anxiety_screening",
        numerator_column_indexes=ANXIETY_NUMERATOR_COLUMN_INDEXES,
        numerator_column_label="AF-AO",
        silent=silent,
    )


def is_checkbox_checked(value: str) -> bool:
    return value.strip().casefold() == "checked"


def categorize_patient_race(row: list[str]) -> str:
    """Assign one race/ethnicity using AIM disparity-priority rules.

    From Race Ethnicity Categorization.pdf:
      1. Black with any other race -> african_american
      2. Hispanic with any other race except Black -> hispanic
      3. Native American with any other race except Black -> native_american
      4. Asian/Pacific Islander:
           - Asian (alone or with White/NHPI) -> asian
           - NHPI with White -> asian
           - NHPI alone -> native_hawaiian_pacific_islander
      5. White, non-Hispanic -> white
      6. Other only when coded as other -> other
      7. Race not reported -> race_not_reported
      8. No race selected -> unknown
    """
    white = is_checkbox_checked(row[PATIENT_RACE_WHITE_INDEX])
    black = is_checkbox_checked(row[PATIENT_RACE_BLACK_INDEX])
    hispanic = is_checkbox_checked(row[PATIENT_RACE_HISPANIC_INDEX])
    asian = is_checkbox_checked(row[PATIENT_RACE_ASIAN_INDEX])
    native_american = is_checkbox_checked(row[PATIENT_RACE_NATIVE_AMERICAN_INDEX])
    nhpi = is_checkbox_checked(row[PATIENT_RACE_NHPI_INDEX])
    other = is_checkbox_checked(row[PATIENT_RACE_OTHER_INDEX])
    not_reported = is_checkbox_checked(row[PATIENT_RACE_NOT_REPORTED_INDEX])

    if black:
        return "african_american"
    if hispanic:
        return "hispanic"
    if native_american:
        return "native_american"
    if asian or (nhpi and white):
        return "asian"
    if nhpi:
        return "native_hawaiian_pacific_islander"
    if white:
        return "white"
    if other:
        return "other"
    if not_reported:
        return "race_not_reported"
    return "unknown"


def categorize_patient_insurance(value: str) -> str | None:
    """Map Health Insurance text to an AIM insurance population, if any."""
    cleaned = value.strip().casefold()
    if not cleaned:
        return None
    if "medicaid" in cleaned:
        return "medicaid"
    if "private" in cleaned:
        return "private"
    if "other public" in cleaned:
        return "other_public"
    if cleaned in {"uninsured", "self-pay"}:
        return "uninsured"
    return None


def pmhc_treatment_referral(
    csv_path: str | Path,
    silent: bool = False,
) -> dict[str, tuple[list[str], list[int], list[int]]]:
    """Score pmhc_treatment_referral from PMH Patient Data columns O and P.

    Denominator: patients with valid O/P responses in the population.
    Numerator: patients who did not answer \"No\" to both questions.

    Reports separate Hospital ID / numerator / denominator lists for:
      - population all
      - race/ethnicity populations
      - insurance populations

    Populations with denominator 0 are omitted. Each patient is counted in
    exactly one race category.
    """
    selected_path = resolve_csv_path(csv_path)
    numerators: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    denominators: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    all_months: list[datetime] = []

    for row_number, row, hospital_id, _hospital_name, month in iter_patient_data_rows(
        selected_path
    ):
        medication_raw = row[PATIENT_MEDICATION_COLUMN_INDEX].strip()
        counseling_raw = row[PATIENT_COUNSELING_COLUMN_INDEX].strip()

        if not medication_raw and not counseling_raw:
            continue

        medication = parse_treatment_referral_response(
            medication_raw,
            row_number,
            "O",
        )
        counseling = parse_treatment_referral_response(
            counseling_raw,
            row_number,
            "P",
        )

        in_numerator = not (medication == "no" and counseling == "no")
        race_population = categorize_patient_race(row)
        insurance_population = categorize_patient_insurance(
            row[PATIENT_INSURANCE_COLUMN_INDEX]
        )

        populations = ["all", race_population]
        if insurance_population is not None:
            populations.append(insurance_population)

        all_months.append(month)
        for population in populations:
            denominators[population][hospital_id] += 1
            if in_numerator:
                numerators[population][hospital_id] += 1

    if not all_months:
        raise CSVDataError("The selected CSV contains no patient data rows.")
    validate_quarter(all_months)

    results: dict[str, tuple[list[str], list[int], list[int]]] = {}
    for population in TREATMENT_REFERRAL_POPULATIONS:
        hospital_counts = denominators.get(population, {})
        if not hospital_counts:
            continue

        sorted_ids = sorted(hospital_counts, key=int)
        hospital_ids = [
            format_hospital_id_for_upload(hospital_id) for hospital_id in sorted_ids
        ]
        population_numerators = [
            numerators[population][hospital_id] for hospital_id in sorted_ids
        ]
        population_denominators = [
            hospital_counts[hospital_id] for hospital_id in sorted_ids
        ]

        print_numerator_denominator_columns(
            f"pmhc_treatment_referral (population: {population})",
            hospital_ids,
            population_numerators,
            population_denominators,
            silent=silent,
        )
        results[population] = (
            hospital_ids,
            population_numerators,
            population_denominators,
        )

    return results


IMPLEMENTED_MEASURES = {
    "care_coordination_workgroup",
    "mental_health_protocol",
    "mental_health_screening_tool_sharing",
    "pmhc_provider_nursing_education",
    "pmhc_patient_education",
    "prenatal_depression_screening",
    "prenatal_anxiety_screening",
    "respectful_equitable_care_provider_and_nursing_education",
    "pmhc_treatment_referral",
}

MEASURES = {
    "respectful_equitable_care_provider_and_nursing_education": (
        respectful_equitable_care_provider_and_nursing_education
    ),
    "pmhc_provider_nursing_education": pmhc_provider_nursing_education,
    "care_coordination_workgroup": care_coordination_workgroup,
    "mental_health_protocol": mental_health_protocol,
    "mental_health_screening_tool_sharing": mental_health_screening_tool_sharing,
    "pmhc_patient_education": pmhc_patient_education,
    "prenatal_depression_screening": prenatal_depression_screening,
    "prenatal_anxiety_screening": prenatal_anxiety_screening,
    "pmhc_treatment_referral": pmhc_treatment_referral,
}


def prompt_for_measure() -> str | None:
    """Prompt until the user selects a measure name, or quit."""
    measure_names = list(MEASURES.keys())

    print("\nAvailable measures:")
    for index, name in enumerate(measure_names, start=1):
        print(f"  {index}. {name}")
    print("  q. quit")

    while True:
        choice = input(
            "\nEnter a measure name/number, or q to quit: "
        ).strip()

        if choice.lower() in {"q", "quit"}:
            return None

        if choice.isdigit():
            choice_number = int(choice)
            if 1 <= choice_number <= len(measure_names):
                return measure_names[choice_number - 1]
            print(
                f"Please enter a number between 1 and {len(measure_names)}, or q to quit."
            )
            continue

        if choice in MEASURES:
            return choice

        print(
            "That measure was not recognized. "
            "Choose a name or number from the list, or q to quit."
        )


def run_selected_measure(
    measure_name: str,
    csv_path: str | Path,
    silent: bool = False,
) -> (
    tuple[list[str], list[int]]
    | tuple[list[str], list[int], list[int]]
    | dict[str, tuple[list[str], list[int], list[int]]]
):
    """Run the method attached to the selected measure name."""
    return MEASURES[measure_name](csv_path, silent=silent)


def format_period_dates(quarter: list[datetime]) -> tuple[str, str]:
    """Return AIM template dates as M/D/YYYY for the quarter start and end."""
    start = quarter[0]
    end = quarter[-1]
    last_day = calendar.monthrange(end.year, end.month)[1]
    start_text = f"{start.month}/{start.day}/{start.year}"
    end_text = f"{end.month}/{last_day}/{end.year}"
    return start_text, end_text


def quarter_key(quarter: list[datetime]) -> tuple[tuple[int, int], ...]:
    return tuple((month.year, month.month) for month in quarter)


def get_file_quarter(csv_path: Path, source: str) -> list[datetime]:
    """Read reporting months from a data file and return its validated quarter."""
    months: list[datetime] = []
    if source == "pmh_hospital":
        for _row_number, _row, _hospital_id, _hospital_name, month in iter_data_rows(
            csv_path,
            minimum_columns=4,
        ):
            months.append(month)
    elif source == "birth_quality":
        for _row_number, _row, _hospital_id, _hospital_name, month in (
            iter_birth_quality_rows(csv_path)
        ):
            months.append(month)
    elif source == "pmh_patient":
        for _row_number, _row, _hospital_id, _hospital_name, month in (
            iter_patient_data_rows(csv_path)
        ):
            months.append(month)
    elif source == "screening_education":
        for _row_number, _row, _hospital_id, month in iter_screening_education_rows(
            csv_path
        ):
            months.append(month)
    else:
        raise CSVDataError(f"Unknown data source {source!r}.")

    if not months:
        raise CSVDataError(f"No reporting months found in {csv_path}.")
    return validate_quarter(months)


def built_in_hospital_id_map() -> dict[str, str]:
    """Return the built-in ILPQC ID -> hospital_unique_identifier map."""
    return {
        format_hospital_id_for_upload(hospital_id): identifier
        for hospital_id, identifier in HOSPITAL_ID_TO_IDENTIFIER.items()
    }


def print_valid_and_invalid_hospitals_detailed(
    hospitals: list[Hospital],
) -> None:
    """Print Valid and Invalid hospitals in the detailed multi-line format."""
    valid = [hospital for hospital in hospitals if hospital.state == "Valid"]
    invalid = [hospital for hospital in hospitals if hospital.state == "Invalid"]

    print("\nVALID hospitals:")
    if not valid:
        print("- None")
    else:
        for index, hospital in enumerate(valid):
            if index:
                print("-" * 40)
            print(hospital)

    print("\nINVALID hospitals:")
    if not invalid:
        print("- None")
    else:
        for index, hospital in enumerate(invalid):
            if index:
                print("-" * 40)
            print(hospital)


def print_valid_and_invalid_hospitals_quick(
    hospitals: list[Hospital],
) -> None:
    """Print Valid and Invalid hospitals as short bullet lines."""
    valid = [hospital for hospital in hospitals if hospital.state == "Valid"]
    invalid = [hospital for hospital in hospitals if hospital.state == "Invalid"]

    print("\nVALID hospitals:")
    if not valid:
        print("- None")
    else:
        for hospital in valid:
            print(f"- {hospital.ID} - {hospital.name}")

    print("\nINVALID hospitals:")
    if not invalid:
        print("- None")
    else:
        for hospital in invalid:
            reasons = "; ".join(hospital.invalid_reasons)
            if reasons:
                print(f"- {hospital.ID} - {hospital.name}: {reasons}")
            else:
                print(f"- {hospital.ID} - {hospital.name}")


def build_aim_upload_rows(
    quarter: list[datetime],
    id_map: dict[str, str],
    value_results: dict[str, dict[str, int]],
    num_den_results: dict[str, dict[str, tuple[int, int]]],
    stratified_results: dict[str, dict[str, dict[str, tuple[int, int]]]],
) -> tuple[list[list[str]], set[str]]:
    """Build AIM upload rows matching the default template layout.

    Returns the CSV rows and any Hospital IDs skipped because they were
    missing from the built-in ID map.
    """
    period_start, period_end = format_period_dates(quarter)
    rows: list[list[str]] = [AIM_UPLOAD_HEADER]
    missing_ids: set[str] = set()

    # Placeholder rows required by the template.
    for measure_name in AIM_UPLOAD_MEASURE_ORDER:
        rows.append(
            [
                "",
                "#N/A",
                period_start,
                period_end,
                measure_name,
                "all",
                "",
                "",
                "",
            ]
        )

    for measure_name in AIM_UPLOAD_MEASURE_ORDER:
        if measure_name in AIM_VALUE_MEASURES:
            by_hospital = value_results.get(measure_name, {})
            for hospital_id in sorted(by_hospital, key=int):
                identifier = id_map.get(hospital_id)
                if identifier is None:
                    missing_ids.add(hospital_id)
                    continue
                rows.append(
                    [
                        hospital_id,
                        identifier,
                        period_start,
                        period_end,
                        measure_name,
                        "all",
                        "",
                        "",
                        str(by_hospital[hospital_id]),
                    ]
                )
            continue

        if measure_name in AIM_STRATIFIED_NUM_DEN_MEASURES:
            measure_populations = stratified_results.get(measure_name, {})
            for population in PATIENT_EDUCATION_POPULATIONS:
                by_hospital = measure_populations.get(population, {})
                if not by_hospital:
                    continue
                for hospital_id in sorted(by_hospital, key=int):
                    numerator, denominator = by_hospital[hospital_id]
                    if denominator == 0:
                        continue
                    identifier = id_map.get(hospital_id)
                    if identifier is None:
                        missing_ids.add(hospital_id)
                        continue
                    rows.append(
                        [
                            hospital_id,
                            identifier,
                            period_start,
                            period_end,
                            measure_name,
                            population,
                            str(numerator),
                            str(denominator),
                            "",
                        ]
                    )
            continue

        if measure_name in AIM_NUM_DEN_MEASURES:
            by_hospital = num_den_results.get(measure_name, {})
            for hospital_id in sorted(by_hospital, key=int):
                numerator, denominator = by_hospital[hospital_id]
                identifier = id_map.get(hospital_id)
                if identifier is None:
                    missing_ids.add(hospital_id)
                    continue
                rows.append(
                    [
                        hospital_id,
                        identifier,
                        period_start,
                        period_end,
                        measure_name,
                        "all",
                        str(numerator),
                        str(denominator),
                        "",
                    ]
                )

    return rows, missing_ids


def write_aim_upload_csv(output_path: Path, rows: list[list[str]]) -> None:
    """Write AIM upload rows to CSV using the template header order."""
    with output_path.open("w", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerows(rows)


def generate_aim_upload(
    hospital_path: str | Path,
    birth_path: str | Path | None = None,
    patient_path: str | Path | None = None,
    screening_education_path: str | Path | None = None,
) -> dict:
    """Build an AIM upload CSV. Only the PMH Hospital file is required.

    Optional paths:
      - birth_path -> respectful_equitable_care_...
      - patient_path -> pmhc_treatment_referral
      - screening_education_path -> pmhc_patient_education

    When optional files are provided, they must match the same reporting quarter
    as the PMH Hospital file.
    """
    hospital_path = Path(hospital_path)
    birth_path = Path(birth_path) if birth_path else None
    patient_path = Path(patient_path) if patient_path else None
    screening_education_path = (
        Path(screening_education_path) if screening_education_path else None
    )

    hospital_quarter = get_file_quarter(hospital_path, "pmh_hospital")
    hospital_key = quarter_key(hospital_quarter)

    sources_included: list[str] = ["pmh_hospital"]
    measures_scored: list[str] = [
        "care_coordination_workgroup",
        "mental_health_protocol",
        "mental_health_screening_tool_sharing",
        "pmhc_provider_nursing_education",
        "prenatal_depression_screening",
        "prenatal_anxiety_screening",
    ]
    measures_skipped: list[str] = []

    if birth_path is not None:
        birth_quarter = get_file_quarter(birth_path, "birth_quality")
        if quarter_key(birth_quarter) != hospital_key:
            raise CSVDataError(
                "Birth Quality file must cover the same reporting quarter as "
                "PMH Hospital Data.\n"
                f"PMH Hospital: {format_quarter(hospital_quarter)}\n"
                f"Birth Quality: {format_quarter(birth_quarter)}"
            )
        sources_included.append("birth_quality")
        measures_scored.append(
            "respectful_equitable_care_provider_and_nursing_education"
        )
    else:
        measures_skipped.append(
            "respectful_equitable_care_provider_and_nursing_education"
        )

    if patient_path is not None:
        patient_quarter = get_file_quarter(patient_path, "pmh_patient")
        if quarter_key(patient_quarter) != hospital_key:
            raise CSVDataError(
                "PMH Patient file must cover the same reporting quarter as "
                "PMH Hospital Data.\n"
                f"PMH Hospital: {format_quarter(hospital_quarter)}\n"
                f"PMH Patient: {format_quarter(patient_quarter)}"
            )
        sources_included.append("pmh_patient")
        measures_scored.append("pmhc_treatment_referral")
    else:
        measures_skipped.append("pmhc_treatment_referral")

    if screening_education_path is not None:
        education_quarter = get_file_quarter(
            screening_education_path, "screening_education"
        )
        if quarter_key(education_quarter) != hospital_key:
            raise CSVDataError(
                "Screening/education file must cover the same reporting quarter "
                "as PMH Hospital Data.\n"
                f"PMH Hospital: {format_quarter(hospital_quarter)}\n"
                f"Screening/education: {format_quarter(education_quarter)}"
            )
        sources_included.append("screening_education")
        measures_scored.append("pmhc_patient_education")
    else:
        measures_skipped.append("pmhc_patient_education")

    hospitals, _quarter = load_hospitals(hospital_path)
    invalid_ids = invalid_hospital_id_set(hospitals)
    id_map = built_in_hospital_id_map()

    value_results: dict[str, dict[str, int]] = {}
    num_den_results: dict[str, dict[str, tuple[int, int]]] = {}
    stratified_results: dict[str, dict[str, dict[str, tuple[int, int]]]] = {}

    value_results["care_coordination_workgroup"] = dict(
        zip(
            *filter_value_measure_results(
                *care_coordination_workgroup(hospital_path, silent=True),
                invalid_ids,
            )
        )
    )
    value_results["mental_health_protocol"] = dict(
        zip(
            *filter_value_measure_results(
                *mental_health_protocol(hospital_path, silent=True),
                invalid_ids,
            )
        )
    )
    value_results["mental_health_screening_tool_sharing"] = dict(
        zip(
            *filter_value_measure_results(
                *mental_health_screening_tool_sharing(hospital_path, silent=True),
                invalid_ids,
            )
        )
    )
    value_results["pmhc_provider_nursing_education"] = dict(
        zip(
            *filter_value_measure_results(
                *pmhc_provider_nursing_education(hospital_path, silent=True),
                invalid_ids,
            )
        )
    )

    if birth_path is not None:
        birth_ids, birth_values = (
            respectful_equitable_care_provider_and_nursing_education(
                birth_path,
                silent=True,
            )
        )
        birth_ids, birth_values = filter_value_measure_results(
            birth_ids, birth_values, invalid_ids
        )
        value_results[
            "respectful_equitable_care_provider_and_nursing_education"
        ] = dict(zip(birth_ids, birth_values))

    for measure_name, scorer in (
        ("prenatal_depression_screening", prenatal_depression_screening),
        ("prenatal_anxiety_screening", prenatal_anxiety_screening),
    ):
        hospital_ids, numerators, denominators = scorer(hospital_path, silent=True)
        hospital_ids, numerators, denominators = filter_num_den_measure_results(
            hospital_ids, numerators, denominators, invalid_ids
        )
        num_den_results[measure_name] = {
            hospital_id: (numerator, denominator)
            for hospital_id, numerator, denominator in zip(
                hospital_ids,
                numerators,
                denominators,
            )
        }

    if patient_path is not None:
        raw = pmhc_treatment_referral(patient_path, silent=True)
        raw = filter_treatment_referral_results(raw, invalid_ids)
        stratified_results["pmhc_treatment_referral"] = {}
        for population, (hospital_ids, numerators, denominators) in raw.items():
            stratified_results["pmhc_treatment_referral"][population] = {
                hospital_id: (numerator, denominator)
                for hospital_id, numerator, denominator in zip(
                    hospital_ids,
                    numerators,
                    denominators,
                )
            }

    if screening_education_path is not None:
        raw = pmhc_patient_education(screening_education_path, silent=True)
        raw = filter_treatment_referral_results(raw, invalid_ids)
        stratified_results["pmhc_patient_education"] = {}
        for population, (hospital_ids, numerators, denominators) in raw.items():
            stratified_results["pmhc_patient_education"][population] = {
                hospital_id: (numerator, denominator)
                for hospital_id, numerator, denominator in zip(
                    hospital_ids,
                    numerators,
                    denominators,
                )
            }

    rows, missing_ids = build_aim_upload_rows(
        quarter=hospital_quarter,
        id_map=id_map,
        value_results=value_results,
        num_den_results=num_den_results,
        stratified_results=stratified_results,
    )

    default_name = (
        f"PMH AIM Upload ({hospital_quarter[0].strftime('%b')}-"
        f"{hospital_quarter[-1].strftime('%b %Y')}).csv"
    )

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerows(rows)
    csv_text = buffer.getvalue()

    return {
        "rows": rows,
        "csv_text": csv_text,
        "quarter": hospital_quarter,
        "quarter_label": format_quarter(hospital_quarter),
        "default_filename": default_name,
        "invalid_ids": sorted(invalid_ids, key=int),
        "missing_ids": sorted(missing_ids, key=int),
        "valid_hospital_count": sum(
            1 for hospital in hospitals if hospital.state == "Valid"
        ),
        "hospital_count": len(hospitals),
        "data_row_count": len(rows) - 1,
        "hospitals": hospitals,
        "sources_included": sources_included,
        "measures_scored": measures_scored,
        "measures_skipped": measures_skipped,
    }


def filter_measure_result(
    result: (
        tuple[list[str], list[int]]
        | tuple[list[str], list[int], list[int]]
        | dict[str, tuple[list[str], list[int], list[int]]]
    ),
    invalid_ids: set[str],
) -> (
    tuple[list[str], list[int]]
    | tuple[list[str], list[int], list[int]]
    | dict[str, tuple[list[str], list[int], list[int]]]
):
    """Apply Invalid-hospital filtering to any measure return type."""
    if isinstance(result, dict):
        return filter_treatment_referral_results(result, invalid_ids)
    if len(result) == 2:
        return filter_value_measure_results(result[0], result[1], invalid_ids)
    return filter_num_den_measure_results(
        result[0], result[1], result[2], invalid_ids
    )


def score_measure_for_web(
    measure_name: str,
    measure_source_path: str | Path,
    hospital_path: str | Path | None = None,
) -> dict:
    """Score one measure for the web/copy-helper.

    hospital_path is required when the measure uses PMH Hospital Data.
    For other sources it is optional: when provided, Invalid hospitals from
    that file are omitted; when omitted, no invalid-hospital filtering is done.
    """
    if measure_name not in MEASURES:
        raise CSVDataError(f"Unknown measure {measure_name!r}.")

    source = MEASURE_DATA_SOURCES.get(measure_name, "pmh_hospital")
    measure_source_path = Path(measure_source_path)

    if source == "pmh_hospital":
        if hospital_path is None:
            hospital_path = measure_source_path
        else:
            hospital_path = Path(hospital_path)

    invalid_ids: set[str] = set()
    quarter_label = ""
    if hospital_path is not None:
        hospitals, quarter = load_hospitals(Path(hospital_path))
        invalid_ids = invalid_hospital_id_set(hospitals)
        quarter_label = format_quarter(quarter)
    else:
        source_for_quarter = {
            "birth_quality": "birth_quality",
            "pmh_patient": "pmh_patient",
            "screening_education": "screening_education",
            "pmh_hospital": "pmh_hospital",
        }.get(source, "pmh_hospital")
        quarter = get_file_quarter(measure_source_path, source_for_quarter)
        quarter_label = format_quarter(quarter)

    raw = run_selected_measure(measure_name, measure_source_path, silent=True)
    filtered = filter_measure_result(raw, invalid_ids)

    payload: dict = {
        "measure_name": measure_name,
        "source": source,
        "quarter_label": quarter_label,
        "invalid_ids": sorted(invalid_ids, key=int),
        "result_type": (
            "stratified"
            if isinstance(filtered, dict)
            else "value"
            if len(filtered) == 2
            else "num_den"
        ),
    }

    if isinstance(filtered, dict):
        payload["populations"] = filtered
    elif len(filtered) == 2:
        payload["hospital_ids"] = filtered[0]
        payload["values"] = filtered[1]
    else:
        payload["hospital_ids"] = filtered[0]
        payload["numerators"] = filtered[1]
        payload["denominators"] = filtered[2]

    return payload


def measure_result_to_csv_text(score_payload: dict) -> str:
    """Serialize a score_measure_for_web payload to CSV text."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    measure_name = score_payload["measure_name"]

    if score_payload["result_type"] == "value":
        writer.writerow(["Hospital ID", "measure", "value"])
        for hospital_id, value in zip(
            score_payload["hospital_ids"],
            score_payload["values"],
        ):
            writer.writerow([hospital_id, measure_name, value])
    elif score_payload["result_type"] == "num_den":
        writer.writerow(["Hospital ID", "measure", "numerator", "denominator"])
        for hospital_id, numerator, denominator in zip(
            score_payload["hospital_ids"],
            score_payload["numerators"],
            score_payload["denominators"],
        ):
            writer.writerow([hospital_id, measure_name, numerator, denominator])
    else:
        writer.writerow(
            ["Hospital ID", "measure", "population", "numerator", "denominator"]
        )
        for population, (
            hospital_ids,
            numerators,
            denominators,
        ) in score_payload["populations"].items():
            for hospital_id, numerator, denominator in zip(
                hospital_ids, numerators, denominators
            ):
                writer.writerow(
                    [hospital_id, measure_name, population, numerator, denominator]
                )

    return buffer.getvalue()


def load_hospital_report(csv_path: str | Path) -> dict:
    """Load hospitals and quarter for listing modes (no console prompts)."""
    selected_path = Path(csv_path)
    hospitals, quarter = load_hospitals(selected_path)
    valid = [hospital for hospital in hospitals if hospital.state == "Valid"]
    invalid = [hospital for hospital in hospitals if hospital.state == "Invalid"]
    return {
        "path": selected_path,
        "hospitals": hospitals,
        "valid": valid,
        "invalid": invalid,
        "quarter": quarter,
        "quarter_label": format_quarter(quarter),
    }


def create_aim_upload_csv() -> None:
    """Prompt for source files and write an AIM upload CSV.

    Only the PMH Hospital Data CSV is required; other files are optional.
    """
    print("\nCreate the AIM upload data CSV.")
    print("Required: PMH Hospital Data CSV.")
    print(
        "Optional (press Enter to skip): Birth Quality, PMH Patient, "
        "and screening/education CSVs."
    )

    hospital_path = prompt_for_csv(
        "Enter the path to the PMH Hospital Data CSV *: "
    )
    birth_path = prompt_for_csv_optional(
        "Enter the path to the Birth Quality Data CSV (optional, Enter to skip): "
    )
    patient_path = prompt_for_csv_optional(
        "Enter the path to the PMH Patient Data CSV (optional, Enter to skip): "
    )
    screening_education_path = prompt_for_csv_optional(
        "Enter the path to the PMH table screening/education CSV "
        "(optional, Enter to skip): "
    )

    print("\nScoring measures for AIM upload...")
    result = generate_aim_upload(
        hospital_path,
        birth_path,
        patient_path,
        screening_education_path,
    )

    print(f"\nReporting quarter confirmed: {result['quarter_label']}")
    notify_invalid_hospitals_omitted(set(result["invalid_ids"]))
    if result.get("measures_skipped"):
        print(
            "Skipped measures (no source file provided): "
            + ", ".join(result["measures_skipped"])
        )

    default_name = result["default_filename"]
    output_entered = input(
        f"\nEnter output CSV path [{default_name}]: "
    ).strip().strip('"').strip("'")
    output_path = Path(output_entered) if output_entered else Path(default_name)
    write_aim_upload_csv(output_path, result["rows"])

    print(f"\nAIM upload CSV written to: {output_path.resolve()}")
    print(f"Data rows written: {result['data_row_count']}")
    print(f"Valid hospitals in PMH Hospital file: {result['valid_hospital_count']}")
    if result["missing_ids"]:
        print(
            "\nNote: Hospital ID(s) omitted because they have no "
            "hospital_unique_identifier mapping: "
            + ", ".join(result["missing_ids"])
            + "."
        )


def run_copy_helper() -> None:
    """Interactive copy-helper mode for individual measures."""
    print("\nCopy helper mode.")
    print(
        "PMH Hospital Data CSV is only required for hospital-based measures "
        "(or optionally to exclude Invalid hospitals on other measures)."
    )

    hospital_path: Path | None = None
    invalid_ids: set[str] = set()
    birth_quality_path: Path | None = None
    patient_data_path: Path | None = None
    screening_education_path: Path | None = None

    while True:
        measure_name = prompt_for_measure()
        if measure_name is None:
            print("\nReturning to main menu.")
            break

        try:
            source = MEASURE_DATA_SOURCES.get(measure_name, "pmh_hospital")
            if source == "birth_quality":
                if birth_quality_path is None:
                    birth_quality_path = prompt_for_csv(
                        "Enter the path to the Birth Quality Data CSV *: "
                    )
                measure_path = birth_quality_path
            elif source == "pmh_patient":
                if patient_data_path is None:
                    patient_data_path = prompt_for_csv(
                        "Enter the path to the PMH Patient Data CSV *: "
                    )
                measure_path = patient_data_path
            elif source == "screening_education":
                if screening_education_path is None:
                    screening_education_path = prompt_for_csv(
                        "Enter the path to the PMH table screening/education CSV *: "
                    )
                measure_path = screening_education_path
            else:
                if hospital_path is None:
                    hospital_path = prompt_for_csv(
                        "Enter the path to the PMH Hospital Data CSV *: "
                    )
                    hospitals, _path = run_hospital_importer(
                        hospital_path, show_full_report=False
                    )
                    invalid_ids = invalid_hospital_id_set(hospitals)
                measure_path = hospital_path

            if source != "pmh_hospital" and hospital_path is None:
                maybe_hospital = prompt_for_csv_optional(
                    "Optional: path to PMH Hospital Data CSV to exclude Invalid "
                    "hospitals (Enter to skip): "
                )
                if maybe_hospital is not None:
                    hospital_path = maybe_hospital
                    hospitals, _path = run_hospital_importer(
                        hospital_path, show_full_report=False
                    )
                    invalid_ids = invalid_hospital_id_set(hospitals)

            result = run_selected_measure(measure_name, measure_path, silent=True)
            notify_invalid_hospitals_omitted(invalid_ids)

            if isinstance(result, dict):
                filtered = filter_treatment_referral_results(result, invalid_ids)
                for population, (
                    hospital_ids,
                    numerators,
                    denominators,
                ) in filtered.items():
                    print_numerator_denominator_columns(
                        f"{measure_name} (population: {population})",
                        hospital_ids,
                        numerators,
                        denominators,
                        silent=False,
                    )
            elif len(result) == 2:
                hospital_ids, values = result
                hospital_ids, values = filter_value_measure_results(
                    hospital_ids, values, invalid_ids
                )
                print_measure_columns(
                    measure_name, hospital_ids, values, silent=False
                )
            else:
                hospital_ids, numerators, denominators = result
                hospital_ids, numerators, denominators = (
                    filter_num_den_measure_results(
                        hospital_ids, numerators, denominators, invalid_ids
                    )
                )
                print_numerator_denominator_columns(
                    measure_name,
                    hospital_ids,
                    numerators,
                    denominators,
                    silent=False,
                )
        except NotImplementedError as error:
            print(f"\n{error}")


def run_valid_invalid_list(style: str) -> None:
    """Load a PMH Hospital Data CSV and list Valid vs Invalid hospitals."""
    if style == "quick":
        print("\nQuick list of Valid and Invalid hospitals.")
    else:
        print("\nDetailed list of Valid and Invalid hospitals.")

    csv_path = prompt_for_csv("Enter the path to the PMH Hospital Data CSV: ")
    hospitals, _path = run_hospital_importer(
        csv_path, show_full_report=False, report_invalids=False
    )
    if style == "quick":
        print_valid_and_invalid_hospitals_quick(hospitals)
    else:
        print_valid_and_invalid_hospitals_detailed(hospitals)


def prompt_for_main_mode() -> str | None:
    """Prompt for the top-level program mode."""
    print("\nWhat would you like to do?")
    print("  1. Create the whole AIM upload data CSV")
    print("  2. Copy helper (measure-by-measure lists)")
    print("  3. Quick list of Valid and Invalid hospitals")
    print("  4. Detailed list of Valid and Invalid hospitals")
    print("  q. quit")

    while True:
        choice = input("\nEnter 1, 2, 3, 4, or q: ").strip().casefold()
        if choice in {"q", "quit"}:
            return None
        if choice in {"1", "2", "3", "4"}:
            return choice
        print("Please enter 1, 2, 3, 4, or q.")


def print_program_intro() -> None:
    """Print a short program purpose and author credit."""
    print(
        "\n\nThis program formats quarterly ILPQC/AIM hospital questionnaire "
        "CSVs into AIM upload-ready measure data and a copy helper for "
        "pasting values. Created by Chidi Mba."
    )


def main() -> None:
    """Start with upload vs copy-helper vs hospital validation options."""
    print_program_intro()
    while True:
        mode = prompt_for_main_mode()
        if mode is None:
            print("\nExiting.")
            break

        try:
            if mode == "1":
                create_aim_upload_csv()
            elif mode == "2":
                run_copy_helper()
            elif mode == "3":
                run_valid_invalid_list("quick")
            else:
                run_valid_invalid_list("detailed")
        except (CSVDataError, FileNotFoundError, NotImplementedError) as error:
            print(f"\nError: {error}")


if __name__ == "__main__":
    main()
