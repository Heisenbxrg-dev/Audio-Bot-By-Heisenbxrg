
import io
import os
import threading
import time
import boto3
from flask import Flask, jsonify, request, send_file
from mutagen.id3 import APIC, ID3
from mutagen.mp3 import MP3

app = Flask(__name__)

# Configurazione Cloudflare R2
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

# DIZIONARI DI CACHE IN MEMORIA (RAM)
metadata_cache = {}
cover_cache = {}
file_list_cache = None
file_list_timestamp = 0
CACHE_TTL = 3600  # 1 ora


# FUNZIONE DI PRE-CACHING TOTALE (Eseguita in background all'avvio)
def warm_up_cache():
  global file_list_cache, file_list_timestamp
  print("🔄 [Warm-up] Avvio scansione e pre-caricamento totale da R2...")
  tmp_path = "temp_warmup_audio.mp3"
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

    file_list_cache = files
    file_list_timestamp = time.time()
    print(f"📂 [Warm-up] Trovati {len(files)} brani. Analisi metadati in corso...")

    # Pre-scarica e memorizza i dati di ogni brano in RAM
    for file_key in files:
      if file_key in metadata_cache:
        continue
      try:
        s3_client.download_file(R2_BUCKET_NAME, file_key, tmp_path)
        audio = MP3(tmp_path, ID3=ID3)

        parts = file_key.split("/")
        album_name = parts[-2] if len(parts) > 1 else "Cloudflare R2"
        filename = parts[-1]
        title = os.path.splitext(filename)[0]

        era_str = "Sconosciuta"
        if audio.tags:
          for tag_key in audio.tags.keys():
            if tag_key.startswith("TDRC") or tag_key.startswith("TYER"):
              era_str = str(audio.tags[tag_key].text[0])[:4]
              break

        has_cover = False
        cover_data = None
        mime_type = "image/jpeg"

        if audio.tags:
          for tag_key in audio.tags.keys():
            if tag_key.startswith("APIC"):
              has_cover = True
              apic = audio.tags[tag_key]
              cover_data = apic.data
              mime_type = apic.mime
              break

        if cover_data:
          cover_cache[file_key] = {"data": cover_data, "mime": mime_type}

        metadata_cache[file_key] = {
            "title": title,
            "album": album_name,
            "era": era_str,
            "has_cover": has_cover,
        }
      except Exception as ex:
        print(f"⚠️ Errore pre-cache per {file_key}: {ex}")

    if os.path.exists(tmp_path):
      os.remove(tmp_path)

    print("🚀 [Warm-up] Pre-caricamento completato! Server fulmineo e pronto.")
  except Exception as e:
    if os.path.exists(tmp_path):
      os.remove(tmp_path)
    print(f"❌ Errore durante il warm-up: {e}")


# Avvia il riscaldamento in un thread separato
threading.Thread(target=warm_up_cache, daemon=True).start()


@app.route("/")
def home():
  return "Audio Bot Proxy Server is running 24/7 (Pre-cached Warm)!"


@app.route("/api/list")
def api_list():
  global file_list_cache, file_list_timestamp
  
  # Se la cache è vuota o non pronta, effettua una scansione di emergenza per evitare errori 500
  if file_list_cache is None or len(file_list_cache) == 0:
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
      
      file_list_cache = files
      file_list_timestamp = time.time()
    except Exception as e:
      print(f"⚠️ Errore recupero lista R2: {e}")

  return jsonify(file_list_cache if file_list_cache is not None else [])


@app.route("/api/metadata")
def api_metadata():
  file_key = request.args.get("file")
  if not file_key:
    return jsonify({"error": "File non specificato"}), 400

  if file_key in metadata_cache:
    return jsonify(metadata_cache[file_key])

  # Fallback di sicurezza se un file è stato aggiunto dopo l'avvio
  tmp_path = "temp_meta_audio.mp3"
  try:
    s3_client.download_file(R2_BUCKET_NAME, file_key, tmp_path)
    audio = MP3(tmp_path, ID3=ID3)

    parts = file_key.split("/")
    album_name = parts[-2] if len(parts) > 1 else "Cloudflare R2"
    filename = parts[-1]
    title = os.path.splitext(filename)[0]

    era_str = "Sconosciuta"
    if audio.tags:
      for tag_key in audio.tags.keys():
        if tag_key.startswith("TDRC") or tag_key.startswith("TYER"):
          era_str = str(audio.tags[tag_key].text[0])[:4]
          break

    has_cover = False
    cover_data = None
    mime_type = "image/jpeg"

    if audio.tags:
      for tag_key in audio.tags.keys():
        if tag_key.startswith("APIC"):
          has_cover = True
          apic = audio.tags[tag_key]
          cover_data = apic.data
          mime_type = apic.mime
          break

    if os.path.exists(tmp_path):
      os.remove(tmp_path)

    if cover_data:
      cover_cache[file_key] = {"data": cover_data, "mime": mime_type}

    result = {
        "title": title,
        "album": album_name,
        "era": era_str,
        "has_cover": has_cover,
    }

    metadata_cache[file_key] = result
    return jsonify(result)
  except Exception as e:
    if os.path.exists(tmp_path):
      os.remove(tmp_path)
    filename = os.path.basename(file_key)
    parts = file_key.split("/")
    album_name = parts[-2] if len(parts) > 1 else "Cloudflare R2"
    return jsonify({
        "title": os.path.splitext(filename)[0],
        "album": album_name,
        "era": "N/D",
        "has_cover": False,
    })


@app.route("/cover")
def get_cover():
  file_key = request.args.get("file")
  if not file_key:
    return "File non specificato", 400

  if file_key in cover_cache:
    c_info = cover_cache[file_key]
    return send_file(
        io.BytesIO(c_info["data"]), mimetype=c_info["mime"], as_attachment=False
    )

  return "Copertina non trovata", 404


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