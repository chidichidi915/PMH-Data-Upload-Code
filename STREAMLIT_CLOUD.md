# Free always-on AIM Hospital Data site (Streamlit Community Cloud)

This app runs [`hospital_data.py`](hospital_data.py) measure logic in the browser
UI without requiring users to install Python or leave a laptop on.

## Quick local test

```bash
cd work_code
python -m pip install -r requirements.txt
streamlit run app.py
```

CLI (optional):

```bash
python hospital_data.py
```

## Deploy free on Streamlit Community Cloud (no laptop server)

1. Put **code only** in a **public or private GitHub** repo (see what not to commit below).
2. Go to [https://share.streamlit.io](https://share.streamlit.io) and sign in with GitHub.
3. **New app** → select the repo, branch, and main file: `app.py`.
4. Deploy. You get a URL like `https://your-app-name.streamlit.app`.
5. Share that URL with staff who need AIM formatting.

Requirements file used by Cloud: `requirements.txt` (`streamlit` only; library is pure stdlib).

### Do **not** commit to GitHub

- Patient or hospital raw quarterly CSVs  
- `data/` folders if present  
- passwords, `.env`, secrets  
- anything with PHI/PII  

Suggested `.gitignore` includes `data/`, `.venv/`, `__pycache__/`.

## Features in the web app

| Page | What it does |
|------|----------------|
| AIM upload CSV | Upload 4 quarterly files → download full AIM CSV |
| Copy helper | Score one measure → copyable columns + CSV download |
| Hospital lists | Quick or detailed Valid/Invalid list |
| History | Session history of **outputs**; export/import JSON; optional browser localStorage save |

## History model (why not “server history”)

**Streamlit Community Cloud disks are temporary.** Files written only on the
server can disappear when the app restarts.

This free app therefore:

- Keeps history in the **session** while the browser tab is open  
- Lets you **Export history package (JSON)** to your machine  
- Lets you **Import** that JSON later  
- Can **Save history into this browser** (localStorage) for that device only  

It stores **outputs** (AIM CSV, measure CSV, lists)—**not** full patient source
uploads—to limit size and risk. Re-upload sources when you need a re-run.

Cookies are not used for CSV archives (too small and unsafe).

## Offline CLI still works

`python hospital_data.py` is unchanged for people who prefer the terminal.

## Optional later: team server history

If you need shared multi-user file history on a permanent disk, use the
persistent-disk notes in `DEPLOY.md` (login + `storage.py`). That is **not**
required for the free Community Cloud path.

## Security

- Treat the app URL as internal: anyone who can open it can upload CSVs and
  process data. Share only with authorized staff.  
- Prefer private GitHub repos for code if your org requires it.  
- Do not upload production patient files into Git or public screenshots.
