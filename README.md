# Infinity Translate

Translate text-based PDFs from English to **Telugu** or **Hindi** while keeping the original layout.
The PDF is edited in place: original text is removed with redactions (images, table borders and charts
are kept), and each translated paragraph is written back into its original box with the bundled Noto fonts.

* `translate_pdf.py`: rendering core + CLI
* `providers.py`: translation providers (Google Cloud Translation v2, mock)
* `app/`: FastAPI web app (uploads, jobs, magic-link login, credit ledger, Razorpay)
* `static/`: single-page UI (plain HTML + vanilla JS)
* `fonts/`: Noto Sans Telugu, Noto Sans Devanagari and Noto Sans (Latin fallback), SIL OFL (`fonts/OFL.txt`)

## Setup

Requires Python 3.11+.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then edit .env
```

## Command line

```bash
python translate_pdf.py input.pdf output.pdf --target te      # Telugu
python translate_pdf.py input.pdf output.pdf --target hi --max-pages 2
```

The provider comes from `TRANSLATION_PROVIDER` (`google` needs `GOOGLE_TRANSLATE_API_KEY`;
`mock` returns fixed Telugu/Hindi sample text so you can test layout without a key).
It prints a JSON report: pages processed, blocks translated/skipped, overflow blocks per page and
characters sent to the API. Encrypted and scanned PDFs are rejected with exit code 2.

## Web app

```bash
uvicorn app.main:app --port 8000
```

Open http://localhost:8000. Run **one** process (no `--workers N`): the job worker is a thread inside
the app and the credit reservation lock is per-process.

Flow:
1. Drop a PDF: it is validated server-side (PDF only, max 25 MB, max 50 pages, not encrypted, not scanned)
   and the page count and credit estimate are shown.
2. **Free sample**: anyone can translate the first 2 pages, no account needed.
3. **Full document**: sign in with your email (magic link), buy credits, translate.
   Cost = pages × `CREDITS_PER_PAGE`.
4. Progress shows the stages (extracting, translating, rebuilding, done/failed). The result is previewed as
   page images and downloaded through a signed link that expires (`DOWNLOAD_URL_TTL_SECONDS`).

### API

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/upload` | multipart `file`; returns `file_id`, `pages`, `credit_estimate` |
| POST | `/api/jobs` | `{file_id, target: "te"\|"hi", mode: "sample"\|"full"}` |
| GET | `/api/jobs/{id}` | status, stage, `overflow_count`, error |
| GET | `/api/jobs/{id}/download` | signed, time-limited `url` (+ `preview_url` for page PNGs) |
| POST | `/api/auth/request` | `{email}`: sends a magic link |
| GET | `/auth/verify?token=` | magic link target; sets the HTTP-only session cookie |
| GET | `/api/me` | email, credit balance, reserved credits |
| POST | `/api/checkout` | creates a Razorpay order for the Basic Plan |
| POST | `/api/razorpay/webhook` | Razorpay webhook (`payment.captured`) |

### Credits

Credits live in an append-only ledger (`ledger` table); the balance is always `SUM(amount)`.
Starting a full job writes a `hold` (−cost). Success writes `release` (+cost) and `charge` (−cost);
failure writes only `release`. A unique `(job_id, kind)` constraint means each step happens at most once.
Purchases are keyed by Razorpay payment id (unique), so a webhook delivered twice adds credits once.
Credits are **only** added by the verified webhook, never by the browser callback.

### Razorpay setup

1. Put `RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET` in `.env` (test keys work).
2. In the Razorpay dashboard add a webhook to `https://<your host>/api/razorpay/webhook`, select the
   `payment.captured` event and set a secret; put the same secret in `RAZORPAY_WEBHOOK_SECRET`.
3. The "Buy credits" button appears once the keys are set.

### Email

Set `SMTP_*` to send magic links by email. Without `SMTP_HOST` the link is printed to the server log,
which is handy for local development.

### Housekeeping

* Uploads are deleted after a full translation. After a free sample the upload is kept (so the user can
  go on to the full document) and removed after `UPLOAD_RETENTION_HOURS` (24 h).
* Translated outputs are deleted after 7 days. Cleanup runs on startup and every hour.
* Rate limit: 10 jobs per hour per signed-in user (per IP address for anonymous free samples).
* Each job logs pages, characters translated and processing seconds (also stored on the job row).

## Tests

```bash
python -m pytest -q
```

Covers the credit ledger (reserve/deduct/release, insufficient credits, single settlement), webhook
idempotency and signature checks, scanned/encrypted PDF rejection, the end-to-end sample and full flows,
rate limiting, magic-link login, cleanup, and the Google provider's request format (mocked HTTP).

`python tests/sample_pdfs.py out_dir` writes the 3-page sample used for layout checks
(headings, paragraphs, two columns, a bordered table, a bar chart, an image, a shaded box, lists).

## Known limitations

* **Text-based PDFs only.** Scanned pages are rejected; there is no OCR.
* **One style per paragraph.** Inline bold/italic/colour changes inside a paragraph are lost; the
  paragraph uses its dominant style. There is no italic Telugu/Devanagari font, so italic text may render
  upright.
* **Paragraph detection is heuristic.** Lines are grouped by font size, style, spacing, alignment and
  table rules. Unusual layouts (borderless tables with tight columns, drop caps, text on curves) can be
  merged or split incorrectly.
* **Fit.** Translations are often longer than English. Text is scaled down to 60% if needed, then the box
  grows downward into empty space. Single-line boxes may also grow sideways into empty space. If the
  translation still does not fit, it is drawn smaller than 60% and counted as an "overflow" block.
* Rotated/vertical text, text in form fields and annotations, and text inside images are not translated.
* Links stay clickable, but their target areas no longer match the translated words exactly.
* Hyperlinks, URLs and emails inside sentences are sent to the translator as plain text.
* The mock provider produces placeholder text; it is only for layout testing.
* Single process only (in-process worker, SQLite).
