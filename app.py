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
# Fixed pages; item pages are added dynamically later per participant
# NOTE: headphone_instructions inserted between screening and headphone
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

    # survey sheet
    try:
        survey_ws = get_worksheet("survey")
        survey_rows = survey_ws.get_all_values()
        # Columns: timestamp_utc, participant_id, ...
        for r in survey_rows[1:]:
            if len(r) > 1 and r[1].strip():
                ids.add(r[1].strip())
    except Exception:
        pass

    # transcript sheet
    try:
        transcript_ws = get_worksheet("transcript")
        trans_rows = transcript_ws.get_all_values()
        # Columns: timestamp_utc, participant_id, audio_id, ...
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
        # uuid4().hex is 32 hex chars with no hyphens; take first 10
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

    Includes a simple retry (2 attempts) to handle transient SSL errors,
    especially under concurrent access.
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
            else:
                raise
        except Exception as e:
            last_err = e
            if attempt == 0:
                continue
            else:
                raise

    if last_err:
        raise last_err


def render_limited_audio(audio_bytes: bytes, element_id: str, max_plays: int = 2):
    """
    Render an HTML5 audio player that disables itself after max_plays.
    element_id must be unique per audio on the page.
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
    Given the list of CSV fieldnames and a logical column name like
    'current_path' or '_group', return the actual fieldname key
    that matches (ignoring BOM, leading/trailing spaces, and case).
    """
    if not fieldnames:
        return logical_name

    target = logical_name.lower()
    for name in fieldnames:
        clean = name.strip().lstrip("\ufeff")  # remove spaces + BOM
        if clean.lower() == target:
            return name  # return the original exact key used by DictReader
    # Fallback: just return the logical name (if it's already exact)
    return logical_name


# --------------------------------------
# Meta-data readers: sentences & words
# --------------------------------------


@st.cache_resource
def get_sentence_items():
    """
    Read meta_data_sentences.csv and return a list of items:
        {
            "audio_id": normalized_path,
            "group": "_group" value (e.g., "G0"),
            "drive_file_id": file_id,
        }
    """
    drive_cfg = st.secrets["drive"]
    meta_file_id = drive_cfg["meta_data_sentences_file_id"]

    # Download the CSV
    csv_bytes = download_file_bytes(meta_file_id)
    csv_text = csv_bytes.decode("utf-8", errors="replace")

    f = io.StringIO(csv_text)
    reader = csv.DictReader(f)

    if reader.fieldnames is None:
        st.error("Could not read header row from meta_data_sentences.csv.")
        st.stop()

    # Resolve real column names (handle BOM, spaces, case)
    path_col = _resolve_header(reader.fieldnames, "current_path")
    group_col = _resolve_header(reader.fieldnames, "_group")

    audio_index = get_audio_index()
    items = []

    for row in reader:
        raw_path = (row.get(path_col) or "").strip()
        group = (row.get(group_col) or "").strip()
        if not raw_path or not group:
            continue

        # Normalize Windows-style backslashes to POSIX-style slashes
        normalized_path = raw_path.replace("\\", "/")

        p = PurePosixPath(normalized_path)
        parts = p.parts

        if len(parts) < 2:
            # e.g. just "foo.wav" without "sentences/"
            continue

        folder_key = parts[0]   # expected to be 'sentences'
        filename = p.name       # e.g., 'M03_Session2_179.wav'

        file_id = audio_index.get((folder_key, filename))
        if not file_id:
            # If there is no matching file in Drive index, skip
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
    """
    Read meta_data_words.csv and return a list of items:
        {
            "audio_id": normalized_path,
            "group": "_group" value (e.g., "WER0", "WER>0"),
            "drive_file_id": file_id,
        }
    """
    drive_cfg = st.secrets["drive"]
    meta_file_id = drive_cfg["meta_data_words_file_id"]

    # Download the CSV
    csv_bytes = download_file_bytes(meta_file_id)
    csv_text = csv_bytes.decode("utf-8", errors="replace")

    f = io.StringIO(csv_text)
    reader = csv.DictReader(f)

    if reader.fieldnames is None:
        st.error("Could not read header row from meta_data_words.csv.")
        st.stop()

    # Resolve real column names (handle BOM, spaces, case)
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

        folder_key = parts[0]   # expected to be 'isolated_words'
        filename = p.name       # e.g., 'M09_B1_UW27_M8.wav'

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
    Build the ordered items dict for a participant, using:
      - 50 sentence items from meta_data_sentences.csv
      - 50 word items from meta_data_words.csv

    Constraints:
      - 10 blocks, each block has 5 sentences and 5 words.
      - Sentence blocks alternate:
          Type A: G0=2, G1=1, G2=1, G3=1
          Type B: G0=1, G1=1, G2=1, G3=2
        pattern: A, B, A, B, A, B, A, B, A, B
      - Words per block: 3 WER0, 2 WER>0
      - Within each block, items are shuffled.
      - Each clip is used exactly once overall.
      - Randomization is deterministic per participant_id.
    """
    rng = random.Random(participant_id)

    sentence_items = list(get_sentence_items())
    word_items = list(get_word_items())

    # Group sentence items
    sent_groups = {}
    for it in sentence_items:
        g = it["group"]
        sent_groups.setdefault(g, []).append(it)

    # Sanity-check required counts for sentences
    required_sent = {"G0": 15, "G1": 10, "G2": 10, "G3": 15}
    for g, need in required_sent.items():
        have = len(sent_groups.get(g, []))
        if have < need:
            raise ValueError(
                f"Sentence group {g} has {have} usable items, but {need} are required. "
                "Check meta_data_sentences.csv and the files in your 'sentences' folder."
            )

    # Group word items
    word_groups = {}
    for it in word_items:
        g = it["group"]
        word_groups.setdefault(g, []).append(it)

    # Sanity-check required counts for words
    required_word = {"WER0": 30, "WER>0": 20}
    for g, need in required_word.items():
        have = len(word_groups.get(g, []))
        if have < need:
            raise ValueError(
                f"Word group {g} has {have} usable items, but {need} are required. "
                "Check meta_data_words.csv and the files in your 'isolated_words' folder."
            )

    # Shuffle each group pool (after we know counts are OK)
    for g_list in sent_groups.values():
        rng.shuffle(g_list)
    for g_list in word_groups.values():
        rng.shuffle(g_list)

    blocks = []  # list of lists of items (each block's 10 items)

    for block_idx in range(10):
        # Sentence pattern
        if block_idx % 2 == 0:
            # Type A: 2 G0, 1 G1, 1 G2, 1 G3
            sent_pattern = ["G0", "G0", "G1", "G2", "G3"]
        else:
            # Type B: 1 G0, 1 G1, 1 G2, 2 G3
            sent_pattern = ["G0", "G1", "G2", "G3", "G3"]

        # Words pattern: 3 WER0, 2 WER>0
        word_pattern = ["WER0", "WER0", "WER0", "WER>0", "WER>0"]

        # Shuffle order of groups within each block
        rng.shuffle(sent_pattern)
        rng.shuffle(word_pattern)

        block_items = []

        # Draw sentences for this block
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

        # Draw words for this block
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

        # Shuffle within block to mix sentences & words
        rng.shuffle(block_items)
        blocks.append(block_items)

    # Flatten blocks into ordered dict of page_name -> config
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
    If no participant_id yet, only show the login page.
    """
    pid = st.session_state.get("participant_id", "")
    if not pid:
        return [LOGIN_PAGE]

    main_items = build_main_items_for_participant(pid)
    item_pages = list(main_items.keys())
    return BASE_PAGES + item_pages + [FINAL_PAGE]


# --------------------------------------
# Headphone items (manual for now)
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
    # Will be overwritten when they generate or enter an ID
    st.session_state.participant_id = ""

if "new_id_ready" not in st.session_state:
    st.session_state.new_id_ready = False  # used for the new-participant flow

if "screening_answers" not in st.session_state:
    st.session_state.screening_answers = None  # will store dict

if "survey_saved" not in st.session_state:
    st.session_state.survey_saved = False

if "item_start_times" not in st.session_state:
    # per-item start times, set when participant first clicks "Start & show audio"
    st.session_state.item_start_times = {}

if "item_audio_shown" not in st.session_state:
    # per-item flag for showing audio widget
    st.session_state.item_audio_shown = {}

if "page_index" not in st.session_state:
    st.session_state.page_index = 0  # start at "login"


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

    # --- New participant flow ---
    if mode == "I am new here":
        if not st.session_state.new_id_ready:
            if st.button("Generate my participant ID", key="btn_generate_id"):
                # Generate new unique ID and reset per-participant state
                new_id = generate_unique_participant_id()
                st.session_state.participant_id = new_id
                st.session_state.screening_answers = None
                st.session_state.survey_saved = False
                st.session_state.item_start_times = {}
                st.session_state.item_audio_shown = {}
                st.session_state.new_id_ready = True

                # Immediately create a stub row in survey sheet:
                # [timestamp_utc, participant_id, q1..q6, hp1..hp4]
                # We'll fill the rest later.
                try:
                    survey_ws = get_worksheet("survey")
                    stub_row = ["", new_id] + [""] * 10  # total 12 columns
                    survey_ws.append_row(stub_row)
                except Exception as e:
                    st.error("Error creating a record for your participant ID in the survey sheet.")
                    st.exception(e)

                st.rerun()
        else:
            # ID already generated; show it clearly
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

    # --- Returning participant flow ---
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

                # --- Survey row lookup and completeness check ---
                row_for_pid = None
                for r in survey_rows[1:]:
                    if len(r) > 1 and r[1] == pid:
                        row_for_pid = r
                        break

                has_survey_row = row_for_pid is not None
                is_complete_survey = False
                if has_survey_row:
                    # Pad to 12 columns: [ts, pid, q1..q6, hp1..hp4]
                    padded = row_for_pid + [""] * (12 - len(row_for_pid))
                    q_hp_cells = padded[2:12]  # q1..q6 (2–7), hp1..hp4 (8–11)
                    is_complete_survey = all((c or "").strip() != "" for c in q_hp_cells)

                # --- Transcript presence + completed audio ids ---
                has_transcripts = any(
                    len(r) > 1 and r[1] == pid for r in trans_rows[1:]
                )

                # If no record anywhere, show error and stop
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

                # Persist survey completion status into session
                st.session_state.survey_saved = is_complete_survey
                st.session_state.screening_answers = None  # not needed for resume

                main_items = build_main_items_for_participant(pid)
                pages = get_pages()
                item_pages = [p for p in pages if p in main_items]

                # Decide next page based on completeness
                if not is_complete_survey:
                    # Has some row (stub or partial), but screening+headphone not complete.
                    # Send them to screening to (re)complete it.
                    if "screening" in pages:
                        next_page = "screening"
                    else:
                        # Fallback – shouldn't normally happen
                        next_page = "intro"
                else:
                    # Survey fully completed (screening + headphone)
                    if not has_transcripts:
                        # No transcription yet: start with the first item page
                        next_page = item_pages[0] if item_pages else FINAL_PAGE
                    else:
                        # Some transcripts exist, resume from first unfinished item
                        next_audio_page = None
                        for p in item_pages:
                            aid = main_items[p]["audio_id"]
                            if aid not in completed_audio_ids:
                                next_audio_page = p
                                break

                        if next_audio_page is None:
                            next_page = FINAL_PAGE
                        else:
                            next_page = next_audio_page

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
        
        You will copy and paste the participant ID in the body of an email to Christine Holyfield at ceholyfi@uark.edu to receive a gift card after completing the study.
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
            index=None,  # no default
            key="q1_english_first",
        )

        q2 = st.radio(
            "2. What is your age range?",
            ["Under 18", "18–24", "25–34", "35–44", "45–54", "55–64", "65+"],
            index=None,  # no default
            key="q2_age_range",
        )

        q3 = st.text_input("3. What is your gender?", key="q3_gender")

        q4 = st.radio(
            "4. What is the highest education level you have completed?",
            ["Some high school", "High school", "Some college", "College", "Advanced degree"],
            index=None,  # no default
            key="q4_education",
        )

        q5 = st.radio(
            "5. Have you ever had a speech disability?",
            ["Yes", "No"],
            index=None,  # no default
            key="q5_speech_disability",
        )

        q6 = st.radio(
            "6. Please choose which of the following best describes your previous experience communicating with individuals who have a disability that impacts speech.",
            [
                "I have one or more close friends or family members with a disability that impacts speech.",
                "I work in a field that supports people who have disabilities that impact speech.",
                "I have had passing conversations with individuals who have a disability that impacts speech.",
                "I do not remember communicating with an individuals who has a disability that impacts speech.",
            ],
            index=None,  # no default
            key="q6_experience",
        )

        submitted = st.form_submit_button("Submit & Next")

    if submitted:
        # Validate all radios answered + gender not empty
        missing_radio = any(
            ans is None for ans in [q1, q2, q4, q5, q6]
        )
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
    """New page: instructions for the headphone/speaker check (your existing text)."""
    st.header("Headphone / Speaker Check")

    st.markdown(
        """
        This is a brief headphone/speaker check.

        - You can adjust your volume accordingly.  
        - For each item, click the audio, listen once, and choose which word you heard.  
        - You are allowed to listen to each item **up to two times**.
        - Please wait until the audio stops before starting your second playback.
        """
    )

    if st.button("Next", key="headphone_instr_next"):
        go_next_page()


def render_headphone_check():
    st.header("Headphone / Speaker Check")

    # Short reminder text; main instructions are on previous page
    st.markdown(
        "Please complete the following headphone/speaker check items. "
        "You may listen up to **two times per item** before choosing your answer."
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
                index=None,  # no default
                key=f"hp_radio_{item['id']}",
            )
            hp_responses[item["id"]] = answer

            st.write("---")

        submitted = st.form_submit_button("Submit & Next")

    if submitted:
        # Require answers for all headphone items
        if any(v is None for v in hp_responses.values()):
            st.error("Please answer every headphone/speaker check item before continuing.")
            return

        # Save screening + headphone to the same sheet (one row per participant)
        if not st.session_state.survey_saved:
            timestamp = datetime.now(timezone.utc).isoformat()
            p_id = st.session_state.participant_id
            s = st.session_state.screening_answers

            # Full row: timestamp_utc, participant_id, q1..q6, hp1..hp4
            full_row = [
                timestamp,
                p_id,
                s["q1"],
                s["q2"],
                s["q3"],
                s["q4"],
                s["q5"],
                s["q6"],
                hp_responses.get("hp1", ""),
                hp_responses.get("hp2", ""),
                hp_responses.get("hp3", ""),
                hp_responses.get("hp4", ""),
            ]

            try:
                # Find existing stub row by participant_id in column 2
                try:
                    cell = survey_ws.find(p_id, in_column=2)
                    row_idx = cell.row
                    # Update the existing row with full data
                    survey_ws.update(f"A{row_idx}:L{row_idx}", [full_row])
                except Exception:
                    # If not found for some reason, append as new row
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
           - Your time will start from that moment (as a proxy for your first listen).  
        2. Click **Play** in the audio player to hear the item.  
        3. After the first listen, type exactly what you think the speaker said in the text box **"First transcript"**.  
        4. You may then listen **one more time** (the player allows at most two plays).  
        5. After the second listen, you may edit or correct your transcript in the text box **"Second transcript"** if you notice new words or corrections.  
           - If not, you can just copy and paste the first transcript.  
        6. When finished, click **"Save & Next"** to move to the next item.  
           - Both transcripts will be saved in the Google Sheet.

        **Important notes:**

        - The sentences you will listen to are the speech of individuals who have dysarthria, or a disability that affects the clarity of their speech.
        - Many of the spoken sentences may be difficult to understand. It is OK not to be sure what you heard. 
        - Please listen carefully, follow the instructions, and write your best guess.
        - If there are unrecognizable words in between two words you want to write down, do not worry about how many words are missing.  
          Just leave a place holder (e.g. "...", "_", or any mark you like) in between two words as a placeholder.  
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

    # No big header here to save vertical space
    main_items = build_main_items_for_participant(pid)
    keys = list(main_items.keys())
    idx = keys.index(page_name) + 1
    total = len(main_items)

    st.subheader(f"Transcription item {idx} of {total}")

    st.markdown(
        """
        - Click **Start & show audio** when you are ready.
        - You may listen to this item **up to two times**. Then provide your first and second transcripts below.
        - Please do not press pause on the audio files, clicking pause counts the same as if you listened the whole clip.
        """
    )

    file_id = item_config["drive_file_id"]
    audio_id = item_config["audio_id"]  # normalized current_path

    # Button to start timing + reveal audio
    if not st.session_state.item_audio_shown[page_name]:
        if st.button("▶️ Start & show audio", key=f"start_audio_{page_name}"):
            st.session_state.item_start_times[page_name] = datetime.now(timezone.utc)
            st.session_state.item_audio_shown[page_name] = True
            st.rerun()
    else:
        st.info("Audio started. You may listen up to two times.")

    # Render audio player
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

    # Tight divider + small gap before transcription section
    st.markdown(
        "<hr style='margin-top:0.3rem; margin-bottom:0.3rem;'>",
        unsafe_allow_html=True,
    )

    with st.form(f"transcription_form_{page_name}"):
        first_transcript = st.text_area(
            "First transcript (after first listen):",
            height=30,  # ~one line
            key=f"first_{page_name}",
        )
        second_transcript = st.text_area(
            "Second transcript (after second listen; you may copy the first or edit):",
            height=30,  # ~one line
            key=f"second_{page_name}",
        )

        submitted = st.form_submit_button("💾 Save & Next")

    if submitted:
        if page_name not in st.session_state.item_start_times:
            st.error("Please click 'Start & show audio' before submitting your transcripts.")
            return

        if not first_transcript.strip():
            st.error("First transcript cannot be empty. Please type something you understood.")
            return
        if not second_transcript.strip():
            st.error("Second transcript cannot be empty. You may copy the first transcript if nothing changed.")
            return

        start_time = st.session_state.item_start_times[page_name]
        end_time = datetime.now(timezone.utc)
        duration_sec = (end_time - start_time).total_seconds()
        timestamp = datetime.now(timezone.utc).isoformat()

        # Columns:
        # [timestamp_utc, participant_id, audio_id, start_time, end_time,
        #  duration_sec, first_transcript, second_transcript]
        row = [
            timestamp,                     # timestamp_utc
            participant_id,                # participant_id
            audio_id,                      # audio_id (normalized current_path)
            start_time.isoformat(),        # start_time
            end_time.isoformat(),          # end_time
            round(duration_sec, 3),        # duration_sec
            first_transcript,              # first_transcript
            second_transcript,             # second_transcript
        ]

        try:
            transcript_ws.append_row(row)
        except Exception as e:
            st.error("Error saving your transcripts to Google Sheets.")
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
        Please copy and paste your participant ID in the body of an email to Christine Holyfield at ceholyfi@uark.edu to receive a gift card.

        You may now **close this window** after save the code.
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
    # Ensure page_index is in range (defensive, in case pages changes shape)
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
