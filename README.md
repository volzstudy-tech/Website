# IGRIS Study Hub — Full-stack study workspace

A Flask + SQLite study website with a dark, futuristic interface. It starts with **no sample courses, notes, or documents**: your own uploaded study materials are the source of truth.

## Features

- Registration/login with hashed passwords and user-scoped records.
- Upload PDF, DOCX, TXT, Markdown, PNG, JPG, and JPEG. Extract selectable PDF/DOCX/text; OCR for scanned PDFs and images when system dependencies are installed.
- Personal notes vault with create/edit/delete, subjects, and full-text-like search across your uploaded documents and notes.
- AI-generated multiple-choice tests from your own uploads, with saved attempts and explanations.
- AI-generated flashcard decks from a selected uploaded document.
- AI study chat grounded only in your own uploaded documents; it should say when an answer is not present in your materials.
- Subjects, study schedule/quests, Pomodoro/deep-focus timer, daily streak tracking, study analytics, and PDF and CSV exports for notes and test history, plus CSV export for flashcards.
- Reward shop: define personal rewards (chocolate, movie night, outing, etc.) with an estimated rupee budget and Igris coin unlock cost. Coins are earned through saved focus sessions (one per minute, capped at 120 per day) and are not money, cash, or a real payment.
- Optional admin dashboard and role management, enabled by `ADMIN_EMAIL`.
- Deployment files: Dockerfile, Procfile, and Render blueprint (`render.yaml`).
- SQLite database and uploads are stored under `DATA_DIR` (defaults to `instance/`).

## Run locally on Ubuntu

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip tesseract-ocr poppler-utils
unzip igris-fullstack-project.zip
cd igris-fullstack
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Paste the generated value into `FLASK_SECRET_KEY` in `.env`. Add an admin email if desired. Configure **one** AI provider:

```dotenv
AI_PROVIDER=openai
OPENAI_API_KEY=your_api_key_here
# Or use Gemini instead:
# AI_PROVIDER=gemini
# GEMINI_API_KEY=your_gemini_api_key_here
```

Do not commit `.env` or API keys. Start the website:

```bash
python app.py
```

Open `http://127.0.0.1:5000`, create an account, upload your own study material, and explore the navigation. To use the admin dashboard, set `ADMIN_EMAIL` to the email used for your admin account in `.env`; restarting the app grants that account the admin role. Register the admin account with that email, or set the variable and restart after registering.

## OCR notes

The Python packages are installed through `requirements.txt`. OCR additionally needs the system binaries `tesseract-ocr` and `poppler-utils`; the supplied Dockerfile installs them. If a PDF has selectable text, the app uses normal PDF extraction. If it appears image-only, it tries OCR.

## Deploy with Docker

```bash
docker build -t igris-study-hub .
docker run --rm -p 5000:5000 \
  -e FLASK_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')" \
  -e AI_PROVIDER=openai -e OPENAI_API_KEY="your_key" \
  -e DATA_DIR=/data -v igris-study-data:/data igris-study-hub
```

Then open `http://127.0.0.1:5000`. Keep the mounted `/data` volume so the SQLite database and uploads survive container recreation.

## Deploy to Render

1. Push this folder to a private/public Git repository you control.
2. In Render, create a new Blueprint from the repo and let it read `render.yaml`.
3. Enter `OPENAI_API_KEY` and `ADMIN_EMAIL` in the service environment. If using Gemini, set `AI_PROVIDER=gemini` and `GEMINI_API_KEY` instead.
4. Keep the persistent disk mounted at `/var/data`; the blueprint sets `DATA_DIR=/var/data`.
5. After deployment, create your admin account using the `ADMIN_EMAIL` value. The admin role is granted at startup.

A persistent disk is important because local SQLite files and uploaded documents must not be placed on an ephemeral filesystem. Check your host's current pricing, disk availability, and service limits before deploying; this project does not provision hosting or API credits for you.

## Important limitations and safety

- AI features require an API key and may incur provider usage charges. A ChatGPT subscription does not include OpenAI API usage; Gemini app plans and Gemini API billing are also separate.
- For AI chat, quizzes, and flashcards, extracted text from the selected user's documents is sent to the configured AI provider. Do not upload sensitive material unless you are comfortable with that provider's data handling.
- The timer runs in the browser tab; save the session when finished to record time and earn coins. Coins are a motivational game mechanic, not rupees or cash.
- The app is a starter project, not a security-audited production service. Before public deployment, add CSRF protection, rate limiting, email verification/password recovery, stronger upload validation and malware scanning, structured logging, backups, and operational monitoring. Use HTTPS and a strong secret key.
- Admin access is controlled by `ADMIN_EMAIL`; do not expose the admin email or API keys in client-side code.
- No sample course material or default notes are included. Only material a user uploads or writes is used for their study content.
# Website
