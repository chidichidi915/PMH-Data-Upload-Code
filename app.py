"""
Free Streamlit UI for ILPQC/AIM hospital data formatting.

Designed for Streamlit Community Cloud (no laptop server). History is kept
in this browser session and can be exported/imported as a JSON package so
you can reopen results later on this device — not cookies, not server disk
(free Cloud disks are ephemeral).

Run:  streamlit run app.py
CLI:  python hospital_data.py
"""

from __future__ import annotations

import base64
import json
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import streamlit as st
import streamlit.components.v1 as components

import hospital_data as hd

PROGRAM_INTRO = (
    "This program formats quarterly ILPQC/AIM hospital questionnaire "
    "CSVs into AIM upload-ready measure data and a copy helper for "
    "pasting values. Created by Chidi Mba."
)

SOURCE_LABELS = {
    "pmh_hospital": "PMH Hospital Data CSV (52 columns)",
    "birth_quality": "Birth Quality Data CSV (9 columns)",
    "pmh_patient": "PMH Patient Data CSV (18 columns)",
    "screening_education": "PMH table screening/education CSV (8 columns)",
}

MODE_LABELS = {
    "aim_upload": "AIM upload file",
    "copy_helper": "Copy helper measure",
    "hospital_list_quick": "Quick hospital list",
    "hospital_list_detailed": "Detailed hospital list",
}


def label_required(text: str) -> str:
    return f"{text} *"


def label_optional(text: str) -> str:
    return f"{text} (optional)"


def friendly_mode(mode: str | None) -> str:
    if not mode:
        return "Unknown"
    if mode in MODE_LABELS:
        return MODE_LABELS[mode]
    if mode.startswith("hospital_list_"):
        return MODE_LABELS.get(mode, mode.replace("_", " ").title())
    return str(mode).replace("_", " ").title()


def friendly_timestamp(value: str | None) -> str:
    if not value:
        return "Unknown time"
    try:
        # 2026-08-05T14:30:00+00:00 or similar
        cleaned = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        return dt.strftime("%b %d, %Y at %I:%M %p UTC")
    except ValueError:
        return value[:19].replace("T", " ")

HISTORY_STORAGE_KEY = "aim_hospital_data_history_v1"
MAX_HISTORY_RUNS = 30


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def save_upload_to_temp(uploaded_file, suffix: str = ".csv") -> Path:
    temp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    temp.write(uploaded_file.getvalue())
    temp.close()
    return Path(temp.name)


def notes_from_ids(
    invalid_ids: list[str],
    missing_ids: list[str] | None = None,
) -> str:
    parts: list[str] = []
    if invalid_ids:
        parts.append(
            "Invalid hospital(s) omitted: " + ", ".join(invalid_ids) + "."
        )
    if missing_ids:
        parts.append(
            "Unmapped hospital ID(s) omitted: " + ", ".join(missing_ids) + "."
        )
    return " ".join(parts)


def encode_bytes(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def decode_bytes(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def init_history() -> None:
    if "history_runs" not in st.session_state:
        st.session_state.history_runs = []
    if "history_loaded_from_browser" not in st.session_state:
        st.session_state.history_loaded_from_browser = False


def add_history_run(
    *,
    mode: str,
    outputs: dict[str, bytes],
    measure: str | None = None,
    quarter_label: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Append one run (outputs only — not raw patient source CSVs)."""
    init_history()
    entry = {
        "id": uuid.uuid4().hex,
        "created_at": utc_now_iso(),
        "mode": mode,
        "measure": measure,
        "quarter_label": quarter_label,
        "notes": notes,
        "outputs": [
            {
                "filename": name,
                "content_b64": encode_bytes(payload),
            }
            for name, payload in outputs.items()
        ],
    }
    runs: list[dict[str, Any]] = list(st.session_state.history_runs)
    runs.insert(0, entry)
    st.session_state.history_runs = runs[:MAX_HISTORY_RUNS]
    return entry


def history_as_export_json() -> str:
    init_history()
    payload = {
        "version": 1,
        "exported_at": utc_now_iso(),
        "runs": st.session_state.history_runs,
    }
    return json.dumps(payload, indent=2)


def import_history_json(text: str) -> int:
    data = json.loads(text)
    runs = data.get("runs", data if isinstance(data, list) else [])
    if not isinstance(runs, list):
        raise ValueError("History file must contain a list of runs.")
    cleaned: list[dict[str, Any]] = []
    for run in runs:
        if not isinstance(run, dict) or "outputs" not in run:
            continue
        cleaned.append(run)
    init_history()
    # Merge: imported first, then existing, de-dupe by id
    by_id: dict[str, dict[str, Any]] = {}
    for run in cleaned + list(st.session_state.history_runs):
        run_id = str(run.get("id") or uuid.uuid4().hex)
        run["id"] = run_id
        if run_id not in by_id:
            by_id[run_id] = run
    merged = sorted(
        by_id.values(),
        key=lambda r: str(r.get("created_at", "")),
        reverse=True,
    )
    st.session_state.history_runs = merged[:MAX_HISTORY_RUNS]
    return len(cleaned)


def persist_history_to_browser() -> None:
    """Write history package to this browser's localStorage (device-local)."""
    init_history()
    payload = history_as_export_json()
    # Escape for embedding in JS string
    safe = json.dumps(payload)
    components.html(
        f"""
        <script>
        (function() {{
          try {{
            localStorage.setItem({json.dumps(HISTORY_STORAGE_KEY)}, {safe});
            document.body.innerHTML =
              '<p style="font-family:sans-serif;font-size:13px;color:#0a0;">' +
              'Saved history to this browser (localStorage).</p>';
          }} catch (e) {{
            document.body.innerHTML =
              '<p style="font-family:sans-serif;font-size:13px;color:#a00;">' +
              'Could not save to browser: ' + e + '</p>';
          }}
        }})();
        </script>
        """,
        height=40,
    )


def try_load_history_from_browser() -> None:
    """Offer one-time pull of localStorage history into the session."""
    if st.session_state.get("history_loaded_from_browser"):
        return
    # Streamlit cannot reliably read localStorage back into Python without a
    # custom component round-trip. Users can export/import JSON; we also write
    # localStorage when they click "Save history to this browser".
    pass


def page_home() -> None:
    st.header("Home")
    st.write(PROGRAM_INTRO)
    st.markdown(
        """
**What you can do**
1. **AIM upload CSV** — build an AIM file; only the hospital CSV is required (*)  
2. **Copy helper** — score one measure; only that measure’s source file is required  
3. **Hospital lists** — quick or detailed Valid / Invalid list  
4. **History** — re-download results from this session and save them for later  

Original patient uploads are **not** kept in History. Free Streamlit Cloud has  
**no lasting server disk** — download or export results you need to keep.
        """
    )


def page_aim_upload() -> None:
    st.header("Create AIM upload CSV")
    st.write(
        "Only the **PMH Hospital Data** file is required (*). "
        "Add the other files if you want those measures included. "
        "Any optional files you add must cover the **same quarter** as the hospital file."
    )
    st.caption("* = required")

    hospital_file = st.file_uploader(
        label_required(SOURCE_LABELS["pmh_hospital"]),
        type=["csv"],
        key="aim_hospital",
    )
    birth_file = st.file_uploader(
        label_optional(SOURCE_LABELS["birth_quality"]),
        type=["csv"],
        key="aim_birth",
    )
    patient_file = st.file_uploader(
        label_optional(SOURCE_LABELS["pmh_patient"]),
        type=["csv"],
        key="aim_patient",
    )
    education_file = st.file_uploader(
        label_optional(SOURCE_LABELS["screening_education"]),
        type=["csv"],
        key="aim_edu",
    )

    if st.button("Generate AIM upload CSV", type="primary"):
        if hospital_file is None:
            st.error("Please upload the required PMH Hospital Data CSV (*).")
            return

        hospital_path = save_upload_to_temp(hospital_file)
        birth_path = save_upload_to_temp(birth_file) if birth_file else None
        patient_path = save_upload_to_temp(patient_file) if patient_file else None
        education_path = (
            save_upload_to_temp(education_file) if education_file else None
        )

        try:
            with st.spinner("Scoring measures..."):
                result = hd.generate_aim_upload(
                    hospital_path,
                    birth_path,
                    patient_path,
                    education_path,
                )
        except (hd.CSVDataError, FileNotFoundError, ValueError) as error:
            st.error(str(error))
            return

        st.success(f"Reporting quarter: {result['quarter_label']}")
        st.write(
            f"Valid hospitals: {result['valid_hospital_count']} / "
            f"{result['hospital_count']}"
        )
        st.write(f"Data rows written: {result['data_row_count']}")
        if result.get("measures_skipped"):
            st.info(
                "Measures not included (file not uploaded): "
                + ", ".join(result["measures_skipped"])
            )
        notes = notes_from_ids(result["invalid_ids"], result["missing_ids"])
        if notes:
            st.warning(notes)

        csv_bytes = result["csv_text"].encode("utf-8-sig")
        st.download_button(
            "Download AIM upload CSV",
            data=csv_bytes,
            file_name=result["default_filename"],
            mime="text/csv",
        )

        add_history_run(
            mode="aim_upload",
            outputs={result["default_filename"]: csv_bytes},
            quarter_label=result["quarter_label"],
            notes=notes or None,
        )
        st.caption("Saved to History. You can re-download it later from the History page.")


def page_copy_helper() -> None:
    st.header("Copy helper")
    st.write(
        "Pick a measure, then upload only the file(s) that measure needs. "
        "Files stay available after you choose them so you can score other "
        "measures without re-uploading."
    )
    st.caption("* = required for the selected measure")

    measure_name = st.selectbox("Measure", list(hd.MEASURES.keys()))
    source = hd.MEASURE_DATA_SOURCES.get(measure_name, "pmh_hospital")

    hospital_file = None
    measure_file = None

    if source == "pmh_hospital":
        hospital_file = st.file_uploader(
            label_required(SOURCE_LABELS["pmh_hospital"]),
            type=["csv"],
            key="copy_hospital",
        )
        st.caption("This measure reads the PMH Hospital Data CSV.")
    else:
        measure_file = st.file_uploader(
            label_required(SOURCE_LABELS[source]),
            type=["csv"],
            key=f"copy_{source}",
        )
        hospital_file = st.file_uploader(
            label_optional(
                SOURCE_LABELS["pmh_hospital"]
                + " — to exclude Invalid hospitals from results"
            ),
            type=["csv"],
            key="copy_hospital",
        )

    if st.button("Score measure", type="primary"):
        if source == "pmh_hospital":
            if hospital_file is None:
                st.error("Please upload the required PMH Hospital Data CSV (*).")
                return
            hospital_path = save_upload_to_temp(hospital_file)
            measure_path = hospital_path
        else:
            if measure_file is None:
                st.error(f"Please upload the required {SOURCE_LABELS[source]} (*).")
                return
            measure_path = save_upload_to_temp(measure_file)
            hospital_path = (
                save_upload_to_temp(hospital_file) if hospital_file else None
            )

        try:
            with st.spinner("Scoring..."):
                payload = hd.score_measure_for_web(
                    measure_name, measure_path, hospital_path
                )
        except (hd.CSVDataError, FileNotFoundError, ValueError) as error:
            st.error(str(error))
            return

        st.success(f"Reporting period: {payload['quarter_label']}")
        notes = notes_from_ids(payload["invalid_ids"])
        if notes:
            st.warning(notes)

        result_type = payload["result_type"]
        if result_type == "value":
            st.subheader(measure_name)
            col1, col2 = st.columns(2)
            with col1:
                st.markdown("**Hospital ID**")
                st.code("\n".join(payload["hospital_ids"]) or "(none)")
            with col2:
                st.markdown("**value**")
                st.code(
                    "\n".join(str(v) for v in payload["values"]) or "(none)"
                )
        elif result_type == "num_den":
            st.subheader(measure_name)
            c1, c2, c3 = st.columns(3)
            with c1:
                st.markdown("**Hospital ID**")
                st.code("\n".join(payload["hospital_ids"]) or "(none)")
            with c2:
                st.markdown("**numerator**")
                st.code(
                    "\n".join(str(n) for n in payload["numerators"]) or "(none)"
                )
            with c3:
                st.markdown("**denominator**")
                st.code(
                    "\n".join(str(d) for d in payload["denominators"])
                    or "(none)"
                )
        else:
            for population, (
                hospital_ids,
                numerators,
                denominators,
            ) in payload["populations"].items():
                st.subheader(f"{measure_name} — {population}")
                c1, c2, c3 = st.columns(3)
                with c1:
                    st.markdown("**Hospital ID**")
                    st.code("\n".join(hospital_ids) or "(none)")
                with c2:
                    st.markdown("**numerator**")
                    st.code("\n".join(str(n) for n in numerators) or "(none)")
                with c3:
                    st.markdown("**denominator**")
                    st.code(
                        "\n".join(str(d) for d in denominators) or "(none)"
                    )

        csv_text = hd.measure_result_to_csv_text(payload)
        out_name = f"{measure_name}_copy_helper.csv"
        out_bytes = csv_text.encode("utf-8-sig")
        st.download_button(
            "Download measure CSV",
            data=out_bytes,
            file_name=out_name,
            mime="text/csv",
        )

        add_history_run(
            mode="copy_helper",
            outputs={out_name: out_bytes},
            measure=measure_name,
            quarter_label=payload["quarter_label"],
            notes=notes or None,
        )
        st.caption("Saved to History.")


def page_hospital_lists() -> None:
    st.header("Valid and Invalid hospitals")
    style = st.radio("List style", ["quick", "detailed"], horizontal=True)
    hospital_file = st.file_uploader(
        label_required(SOURCE_LABELS["pmh_hospital"]),
        type=["csv"],
        key="list_hospital",
    )
    st.caption("* = required")

    if st.button("List hospitals", type="primary"):
        if hospital_file is None:
            st.error("Please upload the required PMH Hospital Data CSV (*).")
            return

        hospital_path = save_upload_to_temp(hospital_file)
        try:
            report = hd.load_hospital_report(hospital_path)
        except (hd.CSVDataError, FileNotFoundError, ValueError) as error:
            st.error(str(error))
            return

        st.success(
            f"Reporting quarter: {report['quarter_label']} — "
            f"{len(report['hospitals'])} hospitals"
        )

        lines: list[str] = [
            f"Reporting quarter: {report['quarter_label']}",
            f"Hospitals found: {len(report['hospitals'])}",
            "",
            "VALID hospitals:",
        ]
        if not report["valid"]:
            lines.append("- None")
        elif style == "quick":
            for hospital in report["valid"]:
                lines.append(f"- {hospital.ID} - {hospital.name}")
        else:
            for index, hospital in enumerate(report["valid"]):
                if index:
                    lines.append("-" * 40)
                lines.append(str(hospital))

        lines.append("")
        lines.append("INVALID hospitals:")
        if not report["invalid"]:
            lines.append("- None")
        elif style == "quick":
            for hospital in report["invalid"]:
                reasons = "; ".join(hospital.invalid_reasons)
                if reasons:
                    lines.append(
                        f"- {hospital.ID} - {hospital.name}: {reasons}"
                    )
                else:
                    lines.append(f"- {hospital.ID} - {hospital.name}")
        else:
            for index, hospital in enumerate(report["invalid"]):
                if index:
                    lines.append("-" * 40)
                lines.append(str(hospital))

        report_text = "\n".join(lines)
        st.text(report_text)

        out_name = f"hospital_list_{style}.txt"
        out_bytes = report_text.encode("utf-8")
        st.download_button(
            "Download list",
            data=out_bytes,
            file_name=out_name,
            mime="text/plain",
        )

        add_history_run(
            mode=f"hospital_list_{style}",
            outputs={out_name: out_bytes},
            quarter_label=report["quarter_label"],
            notes=(
                f"Valid: {len(report['valid'])}; "
                f"Invalid: {len(report['invalid'])}"
            ),
        )
        st.caption("Saved to History.")


def page_history() -> None:
    st.header("History")
    st.write(
        "This page keeps the **result files** you already created in this "
        "session (downloadable). Original uploads are not stored here. "
        "To keep results after you close the browser, use **Save history file** "
        "and open it later with **Open saved history**."
    )

    init_history()
    try_load_history_from_browser()

    st.subheader("Save or restore your history")
    col_a, col_b = st.columns(2)
    with col_a:
        st.download_button(
            "Download my history file",
            data=history_as_export_json().encode("utf-8"),
            file_name="aim_history_backup.json",
            mime="application/json",
            help="Save this file on your computer, then import it later.",
        )
        if st.button("Remember history in this browser"):
            persist_history_to_browser()
            st.success("Attempted to save history in this browser only.")

    with col_b:
        imported = st.file_uploader(
            "Open a saved history file",
            type=["json"],
            key="history_import",
        )
        if imported is not None and st.button("Load history file"):
            try:
                count = import_history_json(
                    imported.getvalue().decode("utf-8")
                )
                st.success(f"Loaded {count} past result(s).")
                st.rerun()
            except (ValueError, json.JSONDecodeError, UnicodeError) as error:
                st.error(f"Could not open that file: {error}")

    runs: list[dict[str, Any]] = list(st.session_state.history_runs)
    if not runs:
        st.info(
            "No saved results yet. Create an AIM file, score a measure, "
            "or list hospitals — results will show up here."
        )
        return

    st.subheader("Your past results")
    labels = []
    for run in runs:
        when = friendly_timestamp(run.get("created_at"))
        what = friendly_mode(run.get("mode"))
        measure = run.get("measure")
        measure_part = f" — {measure}" if measure else ""
        quarter = run.get("quarter_label")
        quarter_part = f" · {quarter}" if quarter else ""
        labels.append(f"{when} · {what}{measure_part}{quarter_part}")

    choice = st.selectbox(
        "Choose a result to view",
        options=range(len(runs)),
        format_func=lambda i: labels[i],
    )
    run = runs[choice]

    st.markdown("##### Details")
    detail_rows = [
        ("When", friendly_timestamp(run.get("created_at"))),
        ("What you did", friendly_mode(run.get("mode"))),
    ]
    if run.get("measure"):
        detail_rows.append(("Measure", run["measure"]))
    if run.get("quarter_label"):
        detail_rows.append(("Reporting period", run["quarter_label"]))
    if run.get("notes"):
        detail_rows.append(("Notes", run["notes"]))
    file_names = [
        item.get("filename") or "file"
        for item in run.get("outputs", [])
    ]
    detail_rows.append(("Files ready", ", ".join(file_names) if file_names else "None"))

    for label, value in detail_rows:
        st.markdown(f"**{label}:** {value}")

    st.markdown("##### Download files")
    for item in run.get("outputs", []):
        filename = item.get("filename") or "output.bin"
        try:
            data = decode_bytes(item["content_b64"])
        except (KeyError, ValueError):
            st.warning(f"Could not open {filename}")
            continue
        st.download_button(
            f"Download {filename}",
            data=data,
            file_name=filename,
            key=f"hist_{run.get('id')}_{filename}",
        )

    st.markdown("---")
    if st.button("Clear all history in this session"):
        st.session_state.history_runs = []
        st.rerun()


def main() -> None:
    st.set_page_config(
        page_title="AIM Hospital Data",
        layout="wide",
    )
    init_history()

    with st.sidebar:
        st.markdown("**AIM Hospital Data**")
        st.caption("Free web UI · no Python install for end users")
        page = st.radio(
            "Go to",
            [
                "Home",
                "AIM upload CSV",
                "Copy helper",
                "Hospital lists",
                "History",
            ],
        )

    if page == "Home":
        page_home()
    elif page == "AIM upload CSV":
        page_aim_upload()
    elif page == "Copy helper":
        page_copy_helper()
    elif page == "Hospital lists":
        page_hospital_lists()
    else:
        page_history()


if __name__ == "__main__":
    main()
