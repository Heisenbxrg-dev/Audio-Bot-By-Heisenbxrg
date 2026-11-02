import io
import os
import boto3
from flask import Flask, jsonify, render_template, request, send_file
from mutagen.id3 import APIC, ID3
from mutagen.mp3 import MP3

app = Flask(__name__)

# Configurazione Cloudflare R2 (usa le credenziali S3 compatibili di R2)
R2_BUCKET_NAME = "jw-music"
R2_ENDPOINT_URL = "https://5235806c506db968f42f6b4bf16a58e2.r2.cloudflarestorage.com"
R2_ACCESS_KEY_ID = "12717da3e1fde17117a865ecb1b14f87"
R2_SECRET_ACCESS_KEY = "bed6e082d02b745775c5b5540b9105d6c14822e73ca25454b084679a88bab218"
s3_client = boto3.client(
    "s3",
    endpoint_url=R2_ENDPOINT_URL,
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    region_name="auto",
)


@app.route("/")
def home():
  return "Audio Bot Proxy Server is running 24/7!"


@app.route("/api/list")
def api_list():
  try:
    files = []
    paginator = s3_client.get_paginator("list_objects_v2")
    page_iterator = paginator.paginate(Bucket=R2_BUCKET_NAME)

    for page in page_iterator:
      if "Contents" in page:
        for obj in page["Contents"]:
          key = obj["Key"]
          if key.lower().endswith(".mp3"):
            files.append(key)

    return jsonify(files)
  except Exception as e:
    print(f"Errore listing R2: {e}")
    return jsonify([]), 500


@app.route("/api/metadata")
def api_metadata():
  file_key = request.args.get("file")
  if not file_key:
    return jsonify({"error": "File non specificato"}), 400

  tmp_path = "temp_meta_audio.mp3"
  try:
    # Scarica temporaneamente il file in locale (come faceva in locale sul tuo PC)
    # così mutagen può leggerlo con pieno accesso e senza restrizioni di streaming.
    s3_client.download_file(R2_BUCKET_NAME, file_key, tmp_path)

    audio = MP3(tmp_path, ID3=ID3)

    # Album dalla cartella
    parts = file_key.split("/")
    album_name = parts[-2] if len(parts) > 1 else "Cloudflare R2"

    filename = parts[-1]
    title = os.path.splitext(filename)[0]

    # Durata precisa
    duration_str = "Sconosciuta"
    if audio.info and audio.info.length:
      total_seconds = int(audio.info.length)
      mins = total_seconds // 60
      secs = total_seconds % 60
      duration_str = f"{mins}:{secs:02d} min"

    # Copertina APIC
    has_cover = False
    if audio.tags:
      for tag_key in audio.tags.keys():
        if tag_key.startswith("APIC"):
          has_cover = True
          break

    # Pulisce il file temporaneo
    if os.path.exists(tmp_path):
      os.remove(tmp_path)

    return jsonify({
        "title": title,
        "album": album_name,
        "duration": duration_str,
        "has_cover": has_cover,
    })
  except Exception as e:
    if os.path.exists(tmp_path):
      os.remove(tmp_path)
    filename = os.path.basename(file_key)
    parts = file_key.split("/")
    album_name = parts[-2] if len(parts) > 1 else "Cloudflare R2"
    return jsonify({
        "title": os.path.splitext(filename)[0],
        "album": album_name,
        "duration": "N/D",
        "has_cover": False,
    })


@app.route("/download")
def download_file():
  file_key = request.args.get("file")
  if not file_key:
    return "File non specificato", 400

  try:
    file_obj = s3_client.get_object(Bucket=R2_BUCKET_NAME, Key=file_key)
    filename = os.path.basename(file_key)
    return send_file(
        file_obj["Body"],
        as_attachment=True,
        download_name=filename,
        mimetype="audio/mpeg",
    )
  except Exception as e:
    return f"File non trovato su R2: {e}", 404


if __name__ == "__main__":
  app.run(host="0.0.0.0", port=5000)