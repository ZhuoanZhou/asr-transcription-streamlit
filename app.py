import streamlit as st
import gspread
from datetime import datetime, timezone
import uuid
import io
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# -----------------------------
# Google Sheets helper
# -----------------------------

@st.cache_resource
def get_worksheet():
    """Connect to Google Sheets and return the first worksheet."""
    sa_info = st.secrets["gcp_service_account"]
    gc = gspread.service_account_from_dict(sa_info)
    sh = gc.open_by_url(st.secrets["gsheets"]["spreadsheet_url"])
    return sh.sheet1

# -----------------------------
# Google Drive helpers
# -----------------------------

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


def load_audio_from_drive(file_id: str) -> bytes:
    """Download an audio file from Google Drive and return raw bytes."""
    service = get_drive_service()
    request = service.files().get_media(fileId=file_id)

    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)

    done = False
    while not done:
        status, done = downloader.next_chunk()

    fh.seek(0)
    return fh.read()

# -----------------------------
# App setup
# -----------------------------

st.set_page_config(page_title="Audio Transcription Study", page_icon="🎧")

st.title("Audio Transcription Study")
st.write(
    """
    **Instructions:**

    1. Click **"Start task & show audio"** when you are ready.  
       - Your time will start from that moment.  
    2. You may listen to the audio **up to two times**.  
       - Please *do not* replay more than twice.  
    3. Type **only what you can understand**.  
       - If you cannot understand a word, just skip it.  
    4. When you are satisfied with your transcript, click **"Save transcript"**.  
       - Your time will stop when you save.
    """
)

# Session state
if "start_time" not in st.session_state:
    st.session_state.start_time = None

if "participant_id" not in st.session_state:
    st.session_state.participant_id = str(uuid.uuid4())[:8]

if "num_plays" not in st.session_state:
    st.session_state.num_plays = 0

if "show_audio" not in st.session_state:
    st.session_state.show_audio = False

worksheet = get_worksheet()

# For now, a single audio clip from Drive
AUDIO_ID = "audio_001"
AUDIO_FILE_IDS = {
    "audio_001": "1ROTCqC5n3JCX9PgFvbjp0cd8sHHO6ETe",
}

# -----------------------------
# Step 1: start button
# -----------------------------

st.subheader("Step 1: Start and reveal the audio")

if st.session_state.start_time is None:
    if st.button("▶️ Start task & show audio", key="start_task_button"):
        st.session_state.start_time = datetime.now(timezone.utc)
        st.success("Task started! Scroll down to listen to the audio and type your transcript.")
else:
    st.info("Task is already started. Scroll down to listen to the audio and type your transcript.")

# -----------------------------
# Step 2: audio + plays
# -----------------------------

if st.session_state.start_time is not None:
    st.subheader("Step 2: Listen and transcribe")
    st.markdown("You may listen **up to two times**. Please follow the instruction honestly.")

    # Play button with 2-play limit
    if st.session_state.num_plays < 2:
        play_label = f"▶️ Play audio (play #{st.session_state.num_plays + 1} of 2)"
        if st.button(play_label, key="play_audio_button"):
            st.session_state.num_plays += 1
            st.session_state.show_audio = True
    else:
        st.warning("You have reached the maximum of 2 plays for this audio.")
        st.session_state.show_audio = False

    # Only load and show audio when requested
    if st.session_state.show_audio:
        try:
            drive_file_id = AUDIO_FILE_IDS[AUDIO_ID]
        except KeyError:
            st.error(f"Audio ID '{AUDIO_ID}' not found. Check AUDIO_FILE_IDS mapping.")
        else:
            try:
                audio_bytes = load_audio_from_drive(drive_file_id)
                st.audio(audio_bytes, format="audio/wav")
                st.info(
                    "Use the player controls to listen. "
                    "Please remember this still counts as ONE play, even if you scrub back."
                )
            except Exception as e:
                st.error("Could not load the audio file from Google Drive.")
                st.exception(e)

    st.write("---")

    # -----------------------------
    # Step 3: transcription form
    # -----------------------------
    st.subheader("Step 3: Type your transcript")

    with st.form("transcription_form"):
        transcript = st.text_area(
            "Please type what you hear (only what you can understand):",
            height=200,
        )
        submitted = st.form_submit_button("💾 Save transcript")

    if submitted:
        if not transcript.strip():
            st.error("Transcript is empty. Please type something before saving.")
        elif st.session_state.start_time is None:
            st.error("Start time not found. Please click 'Start task & show audio' first.")
        else:
            end_time = datetime.now(timezone.utc)
            duration_sec = (end_time - st.session_state.start_time).total_seconds()

            row = [
                datetime.now(timezone.utc).isoformat(),       # timestamp_utc
                st.session_state.participant_id,              # participant_id
                AUDIO_ID,                                     # audio_id
                st.session_state.start_time.isoformat(),      # start_time
                end_time.isoformat(),                         # end_time
                round(duration_sec, 3),                       # duration_sec
                transcript,                                   # transcript
                st.session_state.num_plays,                   # num_plays
            ]

            try:
                worksheet.append_row(row)
                st.success("Thank you! Your response has been saved.")
                st.write(f"⏱ Time taken: **{round(duration_sec, 1)} seconds**")

                # Reset for safety
                st.session_state.start_time = None
                st.session_state.num_plays = 0
                st.session_state.show_audio = False

            except Exception as e:
                st.error("Something went wrong while saving your response.")
                st.exception(e)

else:
    st.warning("Click **'Start task & show audio'** above when you are ready to begin.")
