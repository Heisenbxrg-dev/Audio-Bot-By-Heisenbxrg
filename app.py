import os
import boto3
from flask import Flask, jsonify, request, send_file, render_template

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


# Rotta API che interroga Cloudflare R2 e restituisce la lista dei brani in JSON
@app.route("/api/list")
def api_list():
  try:
    response = s3_client.list_objects_v2(Bucket=R2_BUCKET_NAME)
    files = [
        obj["Key"]
        for obj in response.get("Contents", [])
        if obj["Key"].lower().endswith(".mp3")
    ]
    return jsonify(files)
  except Exception as e:
    print(f"Errore listing R2: {e}")
    return jsonify([]), 500


@app.route("/download")
def download_file():
  file_key = request.args.get("file")
  if not file_key:
    return "File non specificato", 400

  try:
    # Scarica temporaneamente il file da R2 per servirlo con Content-Disposition: attachment
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