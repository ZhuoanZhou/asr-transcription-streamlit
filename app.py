"""
#command line: python -m streamlit run app.py

import streamlit as st

st.title("Choose an Audio Clip 🎧")

choice = st.selectbox(
    "Select an audio clip:",
    ["Clip 1", "Clip 2"]
)

if choice == "Clip 1":
    file_path = "clip1.wav"
else:
    file_path = "clip2.wav"

st.write(f"You selected: **{choice}**")

with open(file_path, "rb") as f:
    audio_bytes = f.read()

st.audio(audio_bytes, format="audio/wav")
"""

import streamlit as st
import gspread
from datetime import datetime, timezone
from pathlib import Path
import uuid

# -----------------------------
# Google Sheets helper
# -----------------------------

@st.cache_resource
def get_worksheet():
    """Connect to Google Sheets and return the first worksheet."""
    # Load service account info from Streamlit secrets
    sa_info = st.secrets["gcp_service_account"]

    # Create gspread client directly from dict
    gc = gspread.service_account_from_dict(sa_info)

    # Open spreadsheet by URL (stored in secrets)
    sh = gc.open_by_url(st.secrets["gsheets"]["spreadsheet_url"])

    # Use the first sheet (Sheet1)
    return sh.sheet1

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
       - If you cannot understand a word, just skip it (no need to mark blanks).  
    4. When you are satisfied with your transcript, click **"Save transcript"**.  
       - Your time will stop when you save.
    """
)

# -----------------------------
# Session state
# -----------------------------

if "start_time" not in st.session_state:
    st.session_state.start_time = None

if "participant_id" not in st.session_state:
    # Anonymous random ID; you can replace with your own scheme
    st.session_state.participant_id = str(uuid.uuid4())[:8]

if "num_plays" not in st.session_state:
    st.session_state.num_plays = 0

# For this simple demo, we use a single fixed audio file
AUDIO_ID = "audio_001"
audio_path = Path("audio") / "sample.wav"  # adjust name if needed

worksheet = get_worksheet()

# -----------------------------
# Start button
# -----------------------------

st.subheader("Step 1: Start and reveal the audio")

if st.session_state.start_time is None:
    if st.button("▶️ Start task & show audio"):
        st.session_state.start_time = datetime.now(timezone.utc)
        st.success("Task started! Scroll down to listen to the audio and type your transcript.")
else:
    st.info("Task is already started. Scroll down to listen to the audio and type your transcript.")

# -----------------------------
# Show audio + transcription form
# -----------------------------

if st.session_state.start_time is not None:
    st.subheader("Step 2: Listen and transcribe")

    st.markdown("You may listen **up to two times**. Please follow the instruction honestly.")

    # We cannot programmatically detect play events from st.audio,
    # but we can provide a manual counter button if you want.

    # Optional: add a button to keep track of how many times they *say* they played
    if st.button("I am playing the audio now (manual counter)"):
        st.session_state.num_plays += 1
        st.write(f"You have indicated **{st.session_state.num_plays}** play(s). Please do not exceed 2.")

    if not audio_path.exists():
        st.error(f"Audio file not found: {audio_path}")
    else:
        with open(audio_path, "rb") as f:
            audio_bytes = f.read()
        st.audio(audio_bytes, format="audio/wav")

    st.write("---")

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

            # Prepare row for Google Sheets
            row = [
                datetime.now(timezone.utc).isoformat(),       # timestamp_utc
                st.session_state.participant_id,              # participant_id
                AUDIO_ID,                                     # audio_id
                st.session_state.start_time.isoformat(),      # start_time
                end_time.isoformat(),                         # end_time
                round(duration_sec, 3),                       # duration_sec
                transcript,                                   # transcript
                st.session_state.num_plays,                   # num_plays (manual count)
            ]

            try:
                worksheet.append_row(row)
                st.success("Thank you! Your response has been saved.")
                st.write(f"⏱ Time taken: **{round(duration_sec, 1)} seconds**")

                # Reset for safety (so user can't accidentally resubmit)
                st.session_state.start_time = None
                st.session_state.num_plays = 0

            except Exception as e:
                st.error("Something went wrong while saving your response.")
                st.exception(e)

else:
    st.warning("Click **'Start task & show audio'** above when you are ready to begin.")
