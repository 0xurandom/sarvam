import base64
import hashlib
import json
import mimetypes
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
from sarvamai import SarvamAI

ROOT = Path(__file__).resolve().parent
RECORDS = ROOT / "records"
MAX_REQUEST = 24 * 1024 * 1024
TERMINAL_STATES = {"completed", "partially_completed", "failed", "rejected"}


def load_env():
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        value = value.strip().strip("\"'")
        os.environ.setdefault(name.strip(), value)


def file_from_data_url(data_url, allowed_types):
    if not isinstance(data_url, str) or "," not in data_url:
        raise ValueError("A photo and voice recording are required.")
    header, encoded = data_url.split(",", 1)
    mime = header[5:].split(";", 1)[0].lower()
    if mime not in allowed_types:
        raise ValueError("Unsupported upload format.")
    try:
        content = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise ValueError("The uploaded file could not be read.") from exc
    if not content:
        raise ValueError("The uploaded file is empty.")
    return mime, content


def wait_for_extract(client, job_id, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.doc_ai.get_status(job_id=job_id)
        state = status.status.lower()
        if state in TERMINAL_STATES:
            if state not in {"completed", "partially_completed"}:
                raise RuntimeError("Sarvam Vision could not process the proof document.")
            result = client.doc_ai.get_results(job_id=job_id)
            return result.result
        time.sleep(3)
    raise TimeoutError("Document processing is taking longer than expected.")


def start_document_extract(api_key, photo_path, photo_mime, schema):
    # sarvamai 0.1.43 does not expose the current `language` and `content_type`
    # multipart fields, so submit this one job with the SDK's httpx dependency.
    with photo_path.open("rb") as photo_file:
        response = httpx.post(
            "https://api.sarvam.ai/doc-ai/v1/job/extract",
            headers={"api-subscription-key": api_key},
            data={
                "schema": json.dumps(schema),
                "language": "en-IN",
                "content_type": "mixed",
                "output_format": "json",
                "model": "sarvam-vision-v1",
            },
            files={"file": (photo_path.name, photo_file, photo_mime)},
            timeout=90,
        )
    response.raise_for_status()
    return response.json()["job_id"]


def normalized(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def audio_duration(path):
    try:
        output = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            check=True, capture_output=True, text=True, timeout=8,
        ).stdout.strip()
        return float(output)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def transcribe_long_audio(client, audio_path):
    job = client.speech_to_text_job.create_job(model="saaras:v4", mode="transcribe")
    if not job.upload_files(file_paths=[str(audio_path)]):
        raise RuntimeError("Sarvam could not upload the long voice recording.")
    job.start()
    job.wait_until_complete(timeout=600)
    results = job.get_file_results()
    if results.get("failed") or not results.get("successful"):
        raise RuntimeError("Sarvam batch transcription could not process the voice recording.")
    with tempfile.TemporaryDirectory(dir=audio_path.parent) as output_dir:
        if not job.download_outputs(output_dir=output_dir):
            raise RuntimeError("Sarvam could not return the voice transcript.")
        for output_file in Path(output_dir).glob("*.json"):
            result = json.loads(output_file.read_text())
            if result.get("transcript"):
                return result["transcript"]
    raise RuntimeError("Sarvam returned no transcript for the voice recording.")


class AppHandler(BaseHTTPRequestHandler):
    def respond(self, status, payload, content_type="application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path not in {"/", "/index.html"}:
            return self.respond(404, {"error": "Not found"})
        self.respond(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")

    def do_POST(self):
        if self.path != "/api/verify":
            return self.respond(404, {"error": "Not found"})
        stage = "reading upload"
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_REQUEST:
                return self.respond(413, {"error": "Upload is empty or too large."})
            data = json.loads(self.rfile.read(length))
            if not data.get("consent_confirmed"):
                return self.respond(400, {"error": "Confirm recipient consent before submitting."})
            expected_name = str(data.get("consignee_name", "")).strip()
            if not expected_name:
                return self.respond(400, {"error": "Enter the expected consignee name."})

            image_mime, image_bytes = file_from_data_url(
                data.get("photo"), {"image/jpeg", "image/png", "image/webp"}
            )
            audio_mime, audio_bytes = file_from_data_url(
                data.get("audio"),
                {"audio/webm", "audio/ogg", "audio/wav", "audio/mpeg", "audio/mp4", "audio/aac", "audio/x-m4a"},
            )
            record_id = uuid.uuid4().hex[:12].upper()
            record_dir = RECORDS / record_id
            record_dir.mkdir(parents=True, exist_ok=False)
            photo_path = record_dir / ("proof" + (mimetypes.guess_extension(image_mime) or ".img"))
            audio_path = record_dir / ("voice" + (mimetypes.guess_extension(audio_mime) or ".audio"))
            photo_path.write_bytes(image_bytes)
            audio_path.write_bytes(audio_bytes)
            created_at = datetime.now(timezone.utc).isoformat()
            manifest = {
                "record_id": record_id,
                "created_at": created_at,
                "shipment_id": str(data.get("shipment_id", "")).strip(),
                "expected_consignee": expected_name,
                "source_files": {
                    "photo": {"name": photo_path.name, "sha256": hashlib.sha256(image_bytes).hexdigest()},
                    "voice": {"name": audio_path.name, "sha256": hashlib.sha256(audio_bytes).hexdigest()},
                },
                "consent_confirmed": True,
                "processing": "in_progress",
            }
            (record_dir / "record.json").write_text(json.dumps(manifest, indent=2))

            load_env()
            api_key = os.environ.get("SARVAM_API_KEY")
            if not api_key:
                raise RuntimeError("SARVAM_API_KEY is missing from .env.")
            stage = "transcribing voice"
            client = SarvamAI(api_subscription_key=api_key)

            duration = audio_duration(audio_path)
            if duration is not None and duration > 30:
                transcript = transcribe_long_audio(client, audio_path)
            else:
                with audio_path.open("rb") as voice_file:
                    transcript_response = client.speech_to_text.transcribe(
                        file=(audio_path.name, voice_file, audio_mime),
                        model="saaras:v4", mode="transcribe"
                    )
                transcript = transcript_response.transcript

            schema = {
                "type": "object",
                "properties": {
                    "consignee_name": {
                        "type": "string",
                        "description": "The recipient or consignee name printed or handwritten on the proof-of-delivery document. Return an empty string if absent.",
                    },
                    "signature_present": {
                        "type": "boolean",
                        "description": "Whether a handwritten recipient signature mark is visibly present in the signature area. Do not infer the signer's identity.",
                    },
                },
            }
            stage = "extracting proof document"
            job_id = start_document_extract(api_key, photo_path, image_mime, schema)
            extracted = wait_for_extract(client, job_id)
            document_name = str(extracted.get("consignee_name", "")).strip()
            signature_present = extracted.get("signature_present")
            name_matches = bool(document_name) and normalized(document_name) == normalized(expected_name)
            flags = []
            if not document_name:
                flags.append("Consignee name not found on document")
            elif not name_matches:
                flags.append("Consignee name differs from delivery record")
            if signature_present is not True:
                flags.append("Signature missing or needs manual review")
            status = "verified" if not flags else "review"
            result = {
                **manifest,
                "processing": "complete",
                "status": status,
                "transcript": transcript,
                "document": extracted,
                "name_matches": name_matches,
                "exception_flags": flags,
            }
            (record_dir / "record.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
            return self.respond(200, result)
        except (ValueError, json.JSONDecodeError) as exc:
            return self.respond(400, {"error": str(exc)})
        except Exception as exc:
            # Return a useful stage and safe summary without exposing credentials.
            status_code = getattr(exc, "status_code", None) or getattr(
                getattr(exc, "response", None), "status_code", None
            )
            if status_code in {401, 403}:
                error = "Sarvam rejected the API key or account access. Check the key and account permissions in .env."
            elif status_code == 402:
                error = "Sarvam reports that this account has no available API credits."
            elif status_code == 429:
                error = "Sarvam rate limited this request. Wait briefly, then try again."
            elif status_code in {400, 413, 422}:
                error = f"Sarvam rejected the { 'voice recording' if stage == 'transcribing voice' else 'proof document' }. Check its format and try again."
            elif isinstance(exc, (TimeoutError,)):
                error = "Sarvam processing timed out. The original files are saved; try again shortly."
            else:
                error = f"Verification failed while {stage}. Check the server connection and try again."
            if "record_dir" in locals() and record_dir.exists():
                manifest_path = record_dir / "record.json"
                try:
                    manifest = json.loads(manifest_path.read_text())
                    manifest["processing"] = "failed"
                    manifest["failure_stage"] = stage
                    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
                except Exception:
                    pass
            detail = str(exc).replace(locals().get("api_key", "__no_key__"), "[redacted]")[:240]
            print(f"Verification failed at {stage}: {type(exc).__name__}: {detail}", file=sys.stderr, flush=True)
            return self.respond(502, {
                "error": error,
                "stage": stage,
                "record_id": locals().get("record_id"),
            })

    def log_message(self, format, *args):
        # Keep request bodies and credential-bearing headers out of console logs.
        if self.path.startswith("/api/"):
            print(f"{self.address_string()} {args[0] if args else ''}")
        else:
            super().log_message(format, *args)


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 8000), AppHandler).serve_forever()
