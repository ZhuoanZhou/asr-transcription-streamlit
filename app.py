import streamlit as st
import gspread
from datetime import datetime, timezone
import uuid
import io
import base64
import csv
from pathlib import PurePosixPath

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import streamlit.components.v1 as components

# --------------------------------------
# Page configuration
# --------------------------------------
st.set_page_config(page_title="Dysarthric Speech Transcription Study", page_icon="🎧")

# Compact CSS for smaller screens
def inject_compact_css():
    st.markdown(
        """
        <style>
        .block-container {
            padding-top: 0.5rem;
            padding-bottom: 0.5rem;
        }
        h1, h2, h3, h4, h5, h6 {
            margin-bottom: 0.4rem;
        }
        p, li {
            margin-bottom: 0.25rem !important;
            line-height: 1.25 !important;
        }
        textarea {
            line-height: 1.2 !important;
        }
        .stButton>button {
            margin-top: 0.25rem;
            margin-bottom: 0.25rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

LOGIN_PAGE = "login"
FINAL_PAGE = "thank_you"
# Fixed pages; item pages are added dynamically later
BASE_PAGES = [LOGIN_PAGE, "intro", "screening", "headphone", "instructions"]


# --------------------------------------
# Google Sheets helpers
# --------------------------------------


@st.cache_resource
def get_gspread_client():
    sa_info = st.secrets["gcp_service_account"]
    gc = gspread.service_account_from_dict(sa_info)
    return gc


@st.cache_resource
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


@st.cache_data
def get_existing_participant_ids():
    """
    Return a set of all participant_ids present in survey and transcript sheets.
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


@st.cache_resource
def get_drive_service():
    """Create a Drive API service client using the same service account."""
    sa_info = st.secrets["gcp_service_account"]
    creds = Credentials.from_service_account_info(
        sa_info,
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    service = build("drive", "v3", credentials=creds)
    return service


def download_file_bytes(file_id: str) -> bytes:
    """Download a file from Google Drive and return its raw bytes."""
    service = get_drive_service()
    request = service.files().get_media(fileId=file_id)

    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)

    done = False
    while not done:
        _, done = downloader.next_chunk()

    fh.seek(0)
    return fh.read()


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
# Build audio index from Drive + meta_data.csv
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


@st.cache_resource
def get_main_items():
    """
    Reads meta_data.csv from Drive, uses column 'current_path' for order,
    and returns a dict:

        {
            "item_1": {"audio_id": normalized_path, "drive_file_id": file_id},
            ...
        }

    where current_path is like 'sentences\\M03_Session2_179.wav'
    or 'isolated_words\\M09_B1_UW27_M8.wav'.
    """
    drive_cfg = st.secrets["drive"]
    meta_file_id = drive_cfg["meta_data_file_id"]

    # Download meta_data.csv
    csv_bytes = download_file_bytes(meta_file_id)
    csv_text = csv_bytes.decode("utf-8", errors="replace")

    reader = csv.DictReader(io.StringIO(csv_text))
    audio_index = get_audio_index()

    main_items = {}
    counter = 1

    for row in reader:
        raw_path = (row.get("current_path") or "").strip()
        if not raw_path:
            continue

        # Normalize Windows-style backslashes to POSIX-style slashes
        normalized_path = raw_path.replace("\\", "/")

        p = PurePosixPath(normalized_path)
        parts = p.parts

        if len(parts) >= 2:
            folder_key = parts[0]   # 'sentences' or 'isolated_words'
            filename = p.name       # e.g., 'M03_Session2_179.wav'
        else:
            continue

        file_id = audio_index.get((folder_key, filename))
        if not file_id:
            # Skip if no matching file in Drive index
            continue

        page_name = f"item_{counter}"
        main_items[page_name] = {
            "audio_id": normalized_path,  # stored in transcript sheet
            "drive_file_id": file_id,
        }
        counter += 1

    return main_items


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

if "main_items" not in st.session_state:
    # Build dynamic items from meta_data.csv
    st.session_state.main_items = get_main_items()

if "pages" not in st.session_state:
    # Build dynamic page list: login -> intro -> screening -> headphone -> instructions -> items -> thank_you
    item_pages = list(st.session_state.main_items.keys())
    st.session_state.pages = BASE_PAGES + item_pages + [FINAL_PAGE]

if "page_index" not in st.session_state:
    st.session_state.page_index = 0  # start at "login"


def go_next_page():
    """Move to the next page in the flow (no going back)."""
    pages = st.session_state.pages
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

    pages = st.session_state.pages
    main_items = st.session_state.main_items

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
                # Jump to intro
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
                # Columns: timestamp_utc, participant_id, q1..q6, hp1..hp4
                has_survey = any(
                    len(r) > 1 and r[1] == pid for r in survey_rows[1:]
                )

                trans_rows = transcript_ws.get_all_values()
                # Columns: timestamp_utc, participant_id, audio_id, ...
                completed_audio_ids = set()
                for r in trans_rows[1:]:
                    if len(r) > 2 and r[1] == pid:
                        completed_audio_ids.add(r[2])

                st.session_state.survey_saved = has_survey
                st.session_state.screening_answers = None  # not needed later

                pages = st.session_state.pages
                item_pages = [p for p in pages if p in main_items]

                # Decide next page
                if not has_survey:
                    next_page = "intro"
                else:
                    # Find the first audio_id not yet completed
                    next_audio_page = None
                    for p in item_pages:
                        aid = main_items[p]["audio_id"]
                        if aid not in completed_audio_ids:
                            next_audio_page = p
                            break

                    if next_audio_page is None:
                        # All items completed
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
        
        You will copy and paste the code in the body of an email to Christine Holyfield at ceholyfi@uark.edu to receive a gift card after completing the study. (Note: We need to discuss how to modify this.)
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
            key="q1_english_first",
        )

        q2 = st.radio(
            "2. What is your age range?",
            ["Under 18", "18–24", "25–34", "35–44", "45–54", "55–64", "65+"],
            key="q2_age_range",
        )

        q3 = st.text_input("3. What is your gender?", key="q3_gender")

        q4 = st.radio(
            "4. What is the highest education level you have completed?",
            ["Some high school", "High school", "Some college", "College", "Advanced degree"],
            key="q4_education",
        )

        q5 = st.radio(
            "5. Have you ever had a speech disability?",
            ["Yes", "No"],
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
            key="q6_experience",
        )

        submitted = st.form_submit_button("Submit & Next")

    if submitted:
        if q3.strip() == "":
            st.error("Please answer all questions (gender cannot be empty).")
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


def render_headphone_check():
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

    if st.session_state.screening_answers is None and not st.session_state.survey_saved:
        st.error("Screening answers not found. Please restart the survey.")
        return

    survey_ws = get_worksheet("survey")

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
                key=f"hp_radio_{item['id']}",
            )
            hp_responses[item["id"]] = answer

            st.write("---")

        submitted = st.form_submit_button("Submit & Next")

    if submitted:
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

    main_items = st.session_state.main_items
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
          Just leave a place holder (e.g. "...", "_", "X" or any mark you like) in between two words as a placeholder.  
          - Example: write `"I want to _ water."` or `"I want to ... water."` for `"I want to [buy a bottle of] water."`
        """
    )

    if st.button("Next", key="instructions_next"):
        go_next_page()


def render_item_page(page_name: str, item_config: dict):
    transcript_ws = get_worksheet("transcript")
    participant_id = st.session_state.participant_id
    main_items = st.session_state.main_items

    if page_name not in st.session_state.item_audio_shown:
        st.session_state.item_audio_shown[page_name] = False

    st.header("Transcription Task")

    keys = list(main_items.keys())
    idx = keys.index(page_name) + 1
    total = len(main_items)

    st.subheader(f"Item {idx} of {total}")

    st.markdown(
        """
        - Click **Start & show audio** when you are ready.
          Your time will start from that moment.  
        - You may listen to this item **up to two times**.  
        - Then provide your first and second transcripts below.
        - Please wait until the audio stops before starting your second playback.
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

    st.write("---")

    st.subheader("Transcription")

    with st.form(f"transcription_form_{page_name}"):
        first_transcript = st.text_area(
            "First transcript (after first listen):",
            height=120,
            key=f"first_{page_name}",
        )
        second_transcript = st.text_area(
            "Second transcript (after second listen; you may copy the first or edit):",
            height=120,
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
        Your responses have been saved.
        Please copy and paste your participant ID in the body of an email to Christine Holyfield at ceholyfi@uark.edu to receive a gift card.

        You may now **close this window**.
        """
    )
    if st.session_state.participant_id:
        st.write(f"Your participant ID: `{st.session_state.participant_id}`")


# --------------------------------------
# Main router
# --------------------------------------
def main():
    inject_compact_css()

    pages = st.session_state.pages
    current_page = pages[st.session_state.page_index]
    main_items = st.session_state.main_items

    if current_page == LOGIN_PAGE:
        render_login()
    elif current_page == "intro":
        render_intro()
    elif current_page == "screening":
        render_screening()
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
