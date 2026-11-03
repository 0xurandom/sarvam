# EchoProof — Voice Proof of Delivery

A small local web app for capturing a delivery document photo and recipient voice confirmation, then checking the consignee name and visible signature mark with Sarvam.

## Run locally

1. Install Python 3.9 or newer and `ffprobe` (included with FFmpeg; needed to route recordings longer than 30 seconds to Sarvam Batch).
2. Install the Python packages:

   ```bash
   python -m pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env` and set `SARVAM_API_KEY` in `.env`.
4. Start the app:

   ```bash
   python server.py
   ```

5. Open <http://127.0.0.1:8000>.

The app saves original photos, voice recordings, and verification metadata under `records/`. That directory and `.env` are ignored by Git. Submitted media is sent to Sarvam for transcription and document extraction.

## What it checks

- Saaras transcribes voice confirmation; recordings over 30 seconds use Sarvam's Batch API.
- Sarvam Vision extracts the consignee name and whether a signature mark appears on the document.
- A name mismatch, missing name, or missing/unclear signature mark is flagged for manual review.
- Original uploads are stored unchanged with SHA-256 hashes in the local record manifest.

A detected mark does not establish signer identity or legal admissibility. The dashboard counts are illustrative. This is a local prototype without user authentication or a retention workflow.

## Deployment

The local `server.py` uses a long-running HTTP server and local file storage. It is not a Vercel deployment target as-is. A hosted deployment needs serverless request handlers and durable private storage for delivery evidence.
