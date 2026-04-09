import streamlit as st
import gspread
from datetime import datetime, timezone
import uuid
import io
import base64
import csv
from pathlib import PurePosixPath
import ssl
import random

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import streamlit.components.v1 as components

# --------------------------------------
# Page configuration
# --------------------------------------
st.set_page_config(page_title="Dysarthric Speech Transcription Study", page_icon="🎧")


def inject_layout_css():
    """Adjust layout padding to reduce vertical space but avoid cutting titles."""
    st.markdown(
        """
        <style>
        .block-container {
            padding-top: 2.5rem;
            padding-bottom: 1.0rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


LOGIN_PAGE = "login"
FINAL_PAGE = "thank_you"
BASE_PAGES = [LOGIN_PAGE, "intro", "screening", "headphone_instructions", "headphone", "instructions"]


# --------------------------------------
# Google Sheets helpers
# --------------------------------------


def get_gspread_client():
    """Create a fresh gspread client (no cross-session caching)."""
    sa_info = st.secrets["gcp_service_account"]
    gc = gspread.service_account_from_dict(sa_info)
    return gc


def get_worksheet(which: str):
    """
    which: 'survey' or 'transcript'
    Returns the first worksheet of the corresponding spreadsheet.
    """
    gc = get_gspread_client()
    if which == "survey":
        url = st.secrets["gsheets"]["survey_url"]
    elif which == "transcript":
        url = st.secrets["gsheets"]["transcript_url"]
    else:
        raise ValueError("Unknown sheet type")

    sh = gc.open_by_url(url)
    return sh.sheet1


def get_existing_participant_ids():
    """
    Return a set of all participant_ids present in survey and transcript sheets.
    No caching to avoid stale data and concurrency surprises.
    """
    ids = set()

    try:
        survey_ws = get_worksheet("survey")
        survey_rows = survey_ws.get_all_values()
        for r in survey_rows[1:]:
            if len(r) > 1 and r[1].strip():
                ids.add(r[1].strip())
    except Exception:
        pass

    try:
        transcript_ws = get_worksheet("transcript")
        trans_rows = transcript_ws.get_all_values()
        for r in trans_rows[1:]:
            if len(r) > 1 and r[1].strip():
                ids.add(r[1].strip())
    except Exception:
        pass

    return ids


def generate_unique_participant_id() -> str:
    """Generate a 10-character ID not already used in Sheets."""
    existing_ids = get_existing_participant_ids()
    while True:
        candidate = uuid.uuid4().hex[:10]
        if candidate not in existing_ids:
            return candidate


# --------------------------------------
# Google Drive helpers
# --------------------------------------


def get_drive_service():
    """
    Create a Drive API service client using the same service account.
    Not cached to avoid sharing a non-thread-safe client across sessions.
    """
    sa_info = st.secrets["gcp_service_account"]
    creds = Credentials.from_service_account_info(
        sa_info,
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    service = build("drive", "v3", credentials=creds)
    return service


def download_file_bytes(file_id: str) -> bytes:
    """
    Download a file from Google Drive and return its raw bytes.
    Includes a simple retry (2 attempts) to handle transient SSL errors.
    """
    last_err = None
    for attempt in range(2):
        try:
            service = get_drive_service()
            request = service.files().get_media(fileId=file_id)

            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)

            done = False
            while not done:
                _, done = downloader.next_chunk()

            fh.seek(0)
            return fh.read()

        except ssl.SSLError as e:
            last_err = e
            if attempt == 0:
                continue
            raise
        except Exception as e:
            last_err = e
            if attempt == 0:
                continue
            raise

    if last_err:
        raise last_err


def render_limited_audio(audio_bytes: bytes, element_id: str, max_plays: int = 2):
    """
    Render an HTML5 audio player that disables itself after max_plays.
    """
    b64 = base64.b64encode(audio_bytes).decode("utf-8")

    components.html(
        f"""
        <audio id="{element_id}" controls>
            <source src="data:audio/wav;base64,{b64}" type="audio/wav">
            Your browser does not support the audio element.
        </audio>
        <script>
        (function() {{
            const audio = document.getElementById("{element_id}");
            let plays = 0;
            if (audio) {{
                audio.addEventListener("play", () => {{
                    plays += 1;
                    if (plays > {max_plays}) {{
                        audio.pause();
                        audio.currentTime = 0;
                        audio.controls = false;
                        alert("You have reached the maximum of {max_plays} plays for this item.");
                    }}
                }});
            }}
        }})();
        </script>
        """,
        height=90,
    )


# --------------------------------------
# Build audio index from Drive
# --------------------------------------


@st.cache_resource
def get_audio_index():
    """
    Build a mapping: (folder_key, filename) -> file_id
    where folder_key is 'sentences' or 'isolated_words'.
    """
    service = get_drive_service()
    drive_cfg = st.secrets["drive"]

    folder_ids = {
        "sentences": drive_cfg["sentences_folder_id"],
        "isolated_words": drive_cfg["isolated_words_folder_id"],
    }

    index = {}

    for folder_key, folder_id in folder_ids.items():
        query = (
            f"'{folder_id}' in parents and "
            "mimeType contains 'audio/' and trashed = false"
        )
        page_token = None

        while True:
            results = (
                service.files()
                .list(
                    q=query,
                    fields="files(id, name), nextPageToken",
                    pageSize=1000,
                    pageToken=page_token,
                )
                .execute()
            )
            files = results.get("files", [])
            for f in files:
                name = f["name"]
                index[(folder_key, name)] = f["id"]

            page_token = results.get("nextPageToken")
            if not page_token:
                break

    return index


# --------------------------------------
# CSV header resolver helper
# --------------------------------------


def _resolve_header(fieldnames, logical_name):
    """
    Return the actual fieldname key that matches logical_name
    (ignoring BOM, leading/trailing spaces, and case).
    """
    if not fieldnames:
        return logical_name

    target = logical_name.lower()
    for name in fieldnames:
        clean = name.strip().lstrip("\ufeff")
        if clean.lower() == target:
            return name
    return logical_name


# --------------------------------------
# Meta-data readers: sentences & words
# --------------------------------------


@st.cache_resource
def get_sentence_items():
    drive_cfg = st.secrets["drive"]
    meta_file_id = drive_cfg["meta_data_sentences_file_id"]

    csv_bytes = download_file_bytes(meta_file_id)
    csv_text = csv_bytes.decode("utf-8", errors="replace")

    f = io.StringIO(csv_text)
    reader = csv.DictReader(f)

    if reader.fieldnames is None:
        st.error("Could not read header row from meta_data_sentences.csv.")
        st.stop()

    path_col = _resolve_header(reader.fieldnames, "current_path")
    group_col = _resolve_header(reader.fieldnames, "_group")

    audio_index = get_audio_index()
    items = []

    for row in reader:
        raw_path = (row.get(path_col) or "").strip()
        group = (row.get(group_col) or "").strip()
        if not raw_path or not group:
            continue

        normalized_path = raw_path.replace("\\", "/")
        p = PurePosixPath(normalized_path)
        parts = p.parts

        if len(parts) < 2:
            continue

        folder_key = parts[0]
        filename = p.name

        file_id = audio_index.get((folder_key, filename))
        if not file_id:
            continue

        items.append(
            {
                "audio_id": normalized_path,
                "group": group,
                "drive_file_id": file_id,
            }
        )

    return items


@st.cache_resource
def get_word_items():
    drive_cfg = st.secrets["drive"]
    meta_file_id = drive_cfg["meta_data_words_file_id"]

    csv_bytes = download_file_bytes(meta_file_id)
    csv_text = csv_bytes.decode("utf-8", errors="replace")

    f = io.StringIO(csv_text)
    reader = csv.DictReader(f)

    if reader.fieldnames is None:
        st.error("Could not read header row from meta_data_words.csv.")
        st.stop()

    path_col = _resolve_header(reader.fieldnames, "current_path")
    group_col = _resolve_header(reader.fieldnames, "_group")

    audio_index = get_audio_index()
    items = []

    for row in reader:
        raw_path = (row.get(path_col) or "").strip()
        group = (row.get(group_col) or "").strip()
        if not raw_path or not group:
            continue

        normalized_path = raw_path.replace("\\", "/")
        p = PurePosixPath(normalized_path)
        parts = p.parts

        if len(parts) < 2:
            continue

        folder_key = parts[0]
        filename = p.name

        file_id = audio_index.get((folder_key, filename))
        if not file_id:
            continue

        items.append(
            {
                "audio_id": normalized_path,
                "group": group,
                "drive_file_id": file_id,
            }
        )

    return items


# --------------------------------------
# Build per-participant main_items with blocks
# --------------------------------------


@st.cache_data
def build_main_items_for_participant(participant_id: str):
    """
    Build the ordered items dict for a participant.
    """
    rng = random.Random(participant_id)

    sentence_items = list(get_sentence_items())
    word_items = list(get_word_items())

    sent_groups = {}
    for it in sentence_items:
        g = it["group"]
        sent_groups.setdefault(g, []).append(it)

    required_sent = {"G0": 15, "G1": 10, "G2": 10, "G3": 15}
    for g, need in required_sent.items():
        have = len(sent_groups.get(g, []))
        if have < need:
            raise ValueError(
                f"Sentence group {g} has {have} usable items, but {need} are required. "
                "Check meta_data_sentences.csv and the files in your 'sentences' folder."
            )

    word_groups = {}
    for it in word_items:
        g = it["group"]
        word_groups.setdefault(g, []).append(it)

    required_word = {"WER0": 30, "WER>0": 20}
    for g, need in required_word.items():
        have = len(word_groups.get(g, []))
        if have < need:
            raise ValueError(
                f"Word group {g} has {have} usable items, but {need} are required. "
                "Check meta_data_words.csv and the files in your 'isolated_words' folder."
            )

    for g_list in sent_groups.values():
        rng.shuffle(g_list)
    for g_list in word_groups.values():
        rng.shuffle(g_list)

    blocks = []

    for block_idx in range(10):
        if block_idx % 2 == 0:
            sent_pattern = ["G0", "G0", "G1", "G2", "G3"]
        else:
            sent_pattern = ["G0", "G1", "G2", "G3", "G3"]

        word_pattern = ["WER0", "WER0", "WER0", "WER>0", "WER>0"]

        rng.shuffle(sent_pattern)
        rng.shuffle(word_pattern)

        block_items = []

        for g in sent_pattern:
            item = sent_groups[g].pop()
            block_items.append(
                {
                    "kind": "sentence",
                    "group": g,
                    "audio_id": item["audio_id"],
                    "drive_file_id": item["drive_file_id"],
                }
            )

        for g in word_pattern:
            item = word_groups[g].pop()
            block_items.append(
                {
                    "kind": "word",
                    "group": g,
                    "audio_id": item["audio_id"],
                    "drive_file_id": item["drive_file_id"],
                }
            )

        rng.shuffle(block_items)
        blocks.append(block_items)

    main_items = {}
    counter = 1
    for block in blocks:
        for item in block:
            page_name = f"item_{counter}"
            main_items[page_name] = {
                "audio_id": item["audio_id"],
                "drive_file_id": item["drive_file_id"],
                "kind": item["kind"],
                "group": item["group"],
            }
            counter += 1

    return main_items


def get_pages():
    """
    Compute the list of pages for the current session, given participant_id.
    """
    pid = st.session_state.get("participant_id", "")
    if not pid:
        return [LOGIN_PAGE]

    main_items = build_main_items_for_participant(pid)
    item_pages = list(main_items.keys())
    return BASE_PAGES + item_pages + [FINAL_PAGE]


# --------------------------------------
# Headphone items
# --------------------------------------


HEADPHONE_ITEMS = [
    {
        "id": "hp1",
        "label": "Headphone check item 1",
        "drive_file_id": "14rLe5MuNfyjUbZJ5_6iBGa8PGSXKgsWO",
        "options": ["feed", "seed"],
    },
    {
        "id": "hp2",
        "label": "Headphone check item 2",
        "drive_file_id": "1lh3QSGNc58oef5HTYNh1Q0C7vP6TRydo",
        "options": ["lift", "left"],
    },
    {
        "id": "hp3",
        "label": "Headphone check item 3",
        "drive_file_id": "12Qyd7Td5hkESJ6ehbfn8ApAoLNozfSHU",
        "options": ["storm", "swarm"],
    },
    {
        "id": "hp4",
        "label": "Headphone check item 4",
        "drive_file_id": "1YL5KBMBHc0H3afH4A8mRVzdn-CKlPOLm",
        "options": ["hair", "here"],
    },
]


# --------------------------------------
# Session state initialization & flow
# --------------------------------------
if "participant_id" not in st.session_state:
    st.session_state.participant_id = ""

if "new_id_ready" not in st.session_state:
    st.session_state.new_id_ready = False

if "screening_answers" not in st.session_state:
    st.session_state.screening_answers = None

if "survey_saved" not in st.session_state:
    st.session_state.survey_saved = False

if "item_start_times" not in st.session_state:
    st.session_state.item_start_times = {}

if "item_audio_shown" not in st.session_state:
    st.session_state.item_audio_shown = {}

if "page_index" not in st.session_state:
    st.session_state.page_index = 0


def go_next_page():
    """Move to the next page in the flow (no going back)."""
    pages = get_pages()
    if st.session_state.page_index < len(pages) - 1:
        st.session_state.page_index += 1
        st.rerun()


# --------------------------------------
# Login / resume page
# --------------------------------------
def render_login():
    st.title("Dysarthric Speech Transcription Study")
    st.subheader("Participant Login / Resume")

    st.markdown(
        """
        Please choose how you would like to proceed:

        - If this is your **first time**, we will create a new participant ID for you.  
        - If you are **returning**, you can enter your existing participant ID to resume from where you left off.
        """
    )

    mode = st.radio(
        "How would you like to proceed?",
        ["I am new here", "I already have a participant ID"],
        key="login_mode",
    )

    if mode == "I am new here":
        if not st.session_state.new_id_ready:
            if st.button("Generate my participant ID", key="btn_generate_id"):
                new_id = generate_unique_participant_id()
                st.session_state.participant_id = new_id
                st.session_state.screening_answers = None
                st.session_state.survey_saved = False
                st.session_state.item_start_times = {}
                st.session_state.item_audio_shown = {}
                st.session_state.new_id_ready = True

                try:
                    survey_ws = get_worksheet("survey")
                    stub_row = ["", new_id] + [""] * 11  # keep existing 13-column sheet
                    survey_ws.append_row(stub_row)
                except Exception as e:
                    st.error("Error creating a record for your participant ID in the survey sheet.")
                    st.exception(e)

                st.rerun()
        else:
            pid = st.session_state.participant_id
            st.success(
                f"Your participant ID is: `{pid}`\n\n"
                "Please write this down or take a screenshot. "
                "You will need this ID if you come back later."
            )
            if st.button("Start the study", key="btn_start_study"):
                pages = get_pages()
                if "intro" in pages:
                    st.session_state.page_index = pages.index("intro")
                else:
                    st.session_state.page_index = 0
                st.session_state.new_id_ready = False
                st.rerun()
    else:
        pid = st.text_input(
            "Enter your participant ID exactly as given before:",
            key="login_pid",
        )
        if st.button("Resume with this ID", key="btn_resume"):
            pid = pid.strip()
            if not pid:
                st.error("Please enter your participant ID.")
                return

            st.session_state.participant_id = pid

            try:
                survey_ws = get_worksheet("survey")
                transcript_ws = get_worksheet("transcript")

                survey_rows = survey_ws.get_all_values()
                trans_rows = transcript_ws.get_all_values()

                row_for_pid = None
                for r in survey_rows[1:]:
                    if len(r) > 1 and r[1] == pid:
                        row_for_pid = r
                        break

                has_survey_row = row_for_pid is not None
                is_complete_survey = False
                if has_survey_row:
                    # Keep existing 13-column sheet layout:
                    # [ts, pid, q1, q2, q3, race(blank now), q4, q5, q6, hp1, hp2, hp3, hp4]
                    padded = row_for_pid + [""] * max(0, 13 - len(row_for_pid))

                    required_cells = [
                        padded[2],   # q1
                        padded[3],   # q2
                        padded[4],   # q3
                        padded[6],   # q4
                        padded[7],   # q5
                        padded[8],   # q6
                        padded[9],   # hp1
                        padded[10],  # hp2
                        padded[11],  # hp3
                        padded[12],  # hp4
                    ]
                    is_complete_survey = all((c or "").strip() != "" for c in required_cells)

                has_transcripts = any(
                    len(r) > 1 and r[1] == pid for r in trans_rows[1:]
                )

                if not (has_survey_row or has_transcripts):
                    st.error(
                        "We could not find that participant ID in our records. "
                        "Please check for typos or choose 'I am new here' to start as a new participant."
                    )
                    return

                completed_audio_ids = set()
                for r in trans_rows[1:]:
                    if len(r) > 2 and r[1] == pid:
                        completed_audio_ids.add(r[2])

                st.session_state.survey_saved = is_complete_survey
                st.session_state.screening_answers = None

                main_items = build_main_items_for_participant(pid)
                pages = get_pages()
                item_pages = [p for p in pages if p in main_items]

                if not is_complete_survey:
                    next_page = "screening" if "screening" in pages else "intro"
                else:
                    if not has_transcripts:
                        next_page = item_pages[0] if item_pages else FINAL_PAGE
                    else:
                        next_audio_page = None
                        for p in item_pages:
                            aid = main_items[p]["audio_id"]
                            if aid not in completed_audio_ids:
                                next_audio_page = p
                                break

                        next_page = FINAL_PAGE if next_audio_page is None else next_audio_page

                if next_page not in pages:
                    st.error(
                        "Could not determine where to resume. "
                        "Please double-check your participant ID or start as a new participant."
                    )
                else:
                    st.success(f"Resuming participant `{pid}`.")
                    st.session_state.page_index = pages.index(next_page)
                    st.session_state.new_id_ready = False
                    st.rerun()

            except Exception as e:
                st.error("Error while trying to resume from your previous progress.")
                st.exception(e)


# --------------------------------------
# Other page render functions
# --------------------------------------
def render_intro():
    st.header("Introduction")

    st.markdown(
        """
        ### Welcome

        Thank you for your interest in this study.

        In this study, you will:

        - Answer a few brief screening questions  
        - Complete a short headphone/speaker check  
        - Read instructions about how to transcribe  
        - Transcribe a series of **short spoken items** (single words, sentences, or phrases), one at a time

        Your responses will be stored anonymously.
        Please follow the instructions carefully and answer honestly.
        You may leave and continue the study at any time using your participant ID.
        Please keep your participant ID for your record.
        """
    )

    if st.button("Next", key="intro_next"):
        go_next_page()


def render_screening():
    st.header("Screening Questions")

    st.write("All questions on this page are **required**.")

    with st.form("screening_form"):
        q1 = st.radio(
            "1. Is English your first language?",
            ["Yes", "No"],
            index=None,
            key="q1_english_first",
        )

        q2 = st.radio(
            "2. What is your age range?",
            ["Under 18", "18–24", "25–34", "35–44", "45–54", "55–64", "65+"],
            index=None,
            key="q2_age_range",
        )

        q3 = st.text_input("3. What is your gender?", key="q3_gender")

        q4 = st.radio(
            "4. What is the highest education level you have completed?",
            ["Some high school", "High school", "Some college", "College", "Advanced degree"],
            index=None,
            key="q4_education",
        )

        q5 = st.radio(
            "5. Have you ever had a speech disability?",
            ["Yes", "No"],
            index=None,
            key="q5_speech_disability",
        )

        q6 = st.radio(
            "6. Please choose which of the following best describes your previous experience communicating with individuals who have a disability that impacts speech.",
            [
                "I do not remember communicating with an individual who has a disability that impacts speech.",
                "I have had passing conversations with individuals who have a disability that impacts speech.",
                "I have regularly interacted with one person who has a disability that impacts speech.",
                "I have regularly interacted with multiple people who have disabilities that impact speech.",
                "I have specific professional training in speech disabilities."
            ],
            index=None,
            key="q6_experience",
        )

        submitted = st.form_submit_button("Submit & Next")

    if submitted:
        missing_radio = any(ans is None for ans in [q1, q2, q4, q5, q6])
        if missing_radio or q3.strip() == "":
            st.error("Please answer **all** questions before continuing.")
            return

        st.session_state.screening_answers = {
            "q1": q1,
            "q2": q2,
            "q3": q3,
            "q4": q4,
            "q5": q5,
            "q6": q6,
        }

        go_next_page()


def render_headphone_instructions():
    st.header("Headphone / Speaker Check")

    st.markdown(
        """
        This is a brief headphone/speaker check.

        - Headphone is recommanded. (Optional, not required)
        - You can adjust your volume accordingly.  
        - For each item, click the audio, listen once, and choose which word you heard.  
        - You are allowed to listen to each item **up to two times**.
        - Please wait until the audio stops before starting your second playback.
        - Please do not press pause on the audio files, clicking pause counts the same as if you listened the whole clip.
        """
    )

    if st.button("Next", key="headphone_instr_next"):
        go_next_page()


def render_headphone_check():
    st.header("Headphone / Speaker Check")

    st.markdown(
        "Please complete the following headphone/speaker check items. "
        "You may listen up to **two times per item** before choosing your answer. "
        "Please do not press pause on the audio files, clicking pause counts the same as if you listened the whole clip."
    )

    if st.session_state.screening_answers is None and not st.session_state.survey_saved:
        st.error("Screening answers not found. Please restart the survey.")
        return

    try:
        survey_ws = get_worksheet("survey")
    except Exception as e:
        st.error("Error accessing the survey sheet. Please try again in a moment.")
        st.exception(e)
        return

    with st.form("headphone_form"):
        hp_responses = {}

        for idx, item in enumerate(HEADPHONE_ITEMS, start=1):
            st.subheader(f"Item {idx}")

            file_id = item["drive_file_id"]
            options = item["options"]

            try:
                audio_bytes = download_file_bytes(file_id)
                render_limited_audio(
                    audio_bytes,
                    element_id=f"headphone_{item['id']}",
                    max_plays=2,
                )
            except Exception as e:
                st.error(f"Could not load headphone audio for item {idx}.")
                st.exception(e)

            answer = st.radio(
                "Which word did you hear?",
                options,
                index=None,
                key=f"hp_radio_{item['id']}",
            )
            hp_responses[item["id"]] = answer

            st.write("---")

        submitted = st.form_submit_button("Submit & Next")

    if submitted:
        if any(v is None for v in hp_responses.values()):
            st.error("Please answer every headphone/speaker check item before continuing.")
            return

        if not st.session_state.survey_saved:
            timestamp = datetime.now(timezone.utc).isoformat()
            p_id = st.session_state.participant_id
            s = st.session_state.screening_answers

            # Keep existing 13-column sheet layout by writing blank race column
            full_row = [
                timestamp,                 # A
                p_id,                      # B
                s["q1"],                   # C
                s["q2"],                   # D
                s["q3"],                   # E
                "",                        # F race/ethnicity intentionally blank
                s["q4"],                   # G
                s["q5"],                   # H
                s["q6"],                   # I
                hp_responses.get("hp1", ""),  # J
                hp_responses.get("hp2", ""),  # K
                hp_responses.get("hp3", ""),  # L
                hp_responses.get("hp4", ""),  # M
            ]

            try:
                try:
                    cell = survey_ws.find(p_id, in_column=2)
                    row_idx = cell.row
                    survey_ws.update(f"A{row_idx}:M{row_idx}", [full_row])
                except Exception:
                    survey_ws.append_row(full_row)

                st.session_state.survey_saved = True
            except Exception as e:
                st.error("Error saving survey responses to Google Sheets.")
                st.exception(e)
                return

        go_next_page()


def render_instructions():
    st.header("Instructions")

    pid = st.session_state.get("participant_id", "")
    if not pid:
        st.error("Participant ID not found. Please go back to the login page.")
        return

    main_items = build_main_items_for_participant(pid)
    total_items = len(main_items)

    st.markdown(
        f"""
        You will transcribe **{total_items} short spoken items**, one at a time.

        For each item:
        
        1. Click **Start & show audio** to begin.
        2. Click **Play** in the audio player to hear the item.  
           - You may listen **up to two times**.
        3. After you finish listening (once or twice), type exactly what you think the speaker said in the text box **"Transcript"**.  
        4. When finished, click **"Save & Next"** to move to the next item.

        **Important notes:**
        
        - Headphone is recommanded. (Optional, not required)
        - The audio clips (word/phrase/sentence) you will listen to are the speech of individuals who have dysarthria, or a disability that affects the clarity of their speech.
        - Many of the spoken words/phrases/sentences may be difficult to understand. It is OK not to be sure what you heard. 
        - Please listen carefully, follow the instructions, and write your best guess.
        - If there are unrecognizable words in between two words you want to write down, do not worry about how many words are missing. Leave a place holder (e.g. "...", "_", or any mark you like) as a placeholder.  
          - Example: write `"I want to _ water."` or `"I want to ... water."` for `"I want to [buy a bottle of] water."`
        """
    )

    if st.button("Next", key="instructions_next"):
        go_next_page()


def render_item_page(page_name: str, item_config: dict):
    try:
        transcript_ws = get_worksheet("transcript")
    except Exception as e:
        st.error("Error accessing the transcription sheet. Please try again in a moment.")
        st.exception(e)
        return

    participant_id = st.session_state.participant_id
    pid = participant_id

    if page_name not in st.session_state.item_audio_shown:
        st.session_state.item_audio_shown[page_name] = False

    main_items = build_main_items_for_participant(pid)
    keys = list(main_items.keys())
    idx = keys.index(page_name) + 1
    total = len(main_items)

    st.subheader(f"Transcription item {idx} of {total}")

    st.markdown(
        """
        - Click **Start & show audio** when you are ready.
        - You may listen to this item **up to two times**.
        - After listening, provide your transcript in the text box below.
        - Please do not press pause on the audio files, clicking pause counts the same as if you listened the whole clip.
        """
    )

    file_id = item_config["drive_file_id"]
    audio_id = item_config["audio_id"]

    if not st.session_state.item_audio_shown[page_name]:
        if st.button("▶️ Start & show audio", key=f"start_audio_{page_name}"):
            st.session_state.item_start_times[page_name] = datetime.now(timezone.utc)
            st.session_state.item_audio_shown[page_name] = True
            st.rerun()
    else:
        st.info("Audio started. You may listen up to two times.")

    if st.session_state.item_audio_shown[page_name]:
        try:
            audio_bytes = download_file_bytes(file_id)
            render_limited_audio(
                audio_bytes,
                element_id=f"main_{page_name}",
                max_plays=2,
            )
        except Exception as e:
            st.error("Could not load the audio file for this item.")
            st.exception(e)

    st.markdown(
        "<hr style='margin-top:0.3rem; margin-bottom:0.3rem;'>",
        unsafe_allow_html=True,
    )

    with st.form(f"transcription_form_{page_name}"):
        transcript = st.text_area(
            "Transcript (after listening; you may listen up to two times):",
            height=30,
            key=f"transcript_{page_name}",
        )

        submitted = st.form_submit_button("💾 Save & Next")

    if submitted:
        if page_name not in st.session_state.item_start_times:
            st.error("Please click 'Start & show audio' before submitting your transcript.")
            return

        if not transcript.strip():
            st.error("Transcript cannot be empty. Please type something you understood.")
            return

        start_time = st.session_state.item_start_times[page_name]
        end_time = datetime.now(timezone.utc)
        duration_sec = (end_time - start_time).total_seconds()
        timestamp = datetime.now(timezone.utc).isoformat()

        row = [
            timestamp,
            participant_id,
            audio_id,
            start_time.isoformat(),
            end_time.isoformat(),
            round(duration_sec, 3),
            transcript,
        ]

        try:
            transcript_ws.append_row(row)
        except Exception as e:
            st.error("Error saving your transcript to Google Sheets.")
            st.exception(e)
            return

        if page_name in st.session_state.item_start_times:
            del st.session_state.item_start_times[page_name]
        st.session_state.item_audio_shown[page_name] = False

        go_next_page()


def render_thank_you():
    st.title("Thank you!")
    st.markdown(
        """
        Thank you for participating in this study.  
        Your responses have been recorded.
        Please keep your participant ID for your record.

        You may now **close this window** after save the ID.
        """
    )

    if st.session_state.participant_id:
        st.write(f"Your participant ID: `{st.session_state.participant_id}`")


# --------------------------------------
# Main router
# --------------------------------------
def main():
    inject_layout_css()

    pages = get_pages()
    if st.session_state.page_index >= len(pages):
        st.session_state.page_index = 0

    current_page = pages[st.session_state.page_index]

    pid = st.session_state.get("participant_id", "")
    main_items = build_main_items_for_participant(pid) if pid else {}

    if current_page == LOGIN_PAGE:
        render_login()
    elif current_page == "intro":
        render_intro()
    elif current_page == "screening":
        render_screening()
    elif current_page == "headphone_instructions":
        render_headphone_instructions()
    elif current_page == "headphone":
        render_headphone_check()
    elif current_page == "instructions":
        render_instructions()
    elif current_page == FINAL_PAGE:
        render_thank_you()
    elif current_page in main_items:
        render_item_page(current_page, main_items[current_page])
    else:
        st.error("Unknown page state. Please refresh the app.")


if __name__ == "__main__":
    main()