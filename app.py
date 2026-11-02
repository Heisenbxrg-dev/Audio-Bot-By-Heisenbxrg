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


# Rotta API con Paginazione per leggere TUTTI i brani da R2 (senza limiti a 1000)
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
    print(f"Errore listing R2 con paginazione: {e}")
    return jsonify([]), 500


# Rotta API per estrarre i metadati ID3 (Album, Anno/Era, Titolo) dai primi byte del file su R2
@app.route("/api/metadata")
def api_metadata():
  file_key = request.args.get("file")
  if not file_key:
    return jsonify({"error": "File non specificato"}), 400

  try:
    # Scarica solo i primi 512KB per leggere i tag ID3 senza consumare banda inutile
    response = s3_client.get_object(
        Bucket=R2_BUCKET_NAME, Key=file_key, Range="bytes=0-524288"
    )
    file_bytes = io.BytesIO(response["Body"].read())

    audio = MP3(file_bytes, ID3=ID3)
    title = os.path.splitext(os.path.basename(file_key))[0]
    album = "Cloudflare R2"
    year = "Streaming"
    has_cover = False

    if audio.tags:
      if "TIT2" in audio.tags:
        title = str(audio.tags["TIT2"])
      if "TALB" in audio.tags:
        album = str(audio.tags["TALB"])
      if "TDRC" in audio.tags:
        year = str(audio.tags["TDRC"])
      elif "TYER" in audio.tags:
        year = str(audio.tags["TYER"])

      for tag_key in audio.tags.keys():
        if tag_key.startswith("APIC"):
          has_cover = True
          break

    return jsonify({
        "title": title,
        "album": album,
        "year": year,
        "has_cover": has_cover,
    })
  except Exception as e:
    filename = os.path.basename(file_key)
    return jsonify({
        "title": os.path.splitext(filename)[0],
        "album": "Cloudflare R2",
        "year": "Streaming",
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