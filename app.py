import urllib.parse
import requests
from flask import Flask, Response, request

app = Flask(__name__)

# Il tuo URL base di Cloudflare R2
CLOUD_BASE_URL = "https://pub-6a4f25a7900647a89e517a6563265213.r2.dev"


@app.route("/download")
def download_file():
  file_path = request.args.get("file")
  if not file_path:
    return "File non specificato", 400

  target_url = f"{CLOUD_BASE_URL}/songs/{file_path}"

  r = requests.get(target_url, stream=True)
  if r.status_code != 200:
    return "File non trovato su R2", 404

  filename = file_path.split("/")[-1]
  decoded_filename = urllib.parse.unquote(filename)

  def generate():
    for chunk in r.iter_content(chunk_size=8192):
      yield chunk

  response = Response(generate(), content_type=r.headers.get("content-type"))
  # Questa intestazione forza il browser a scaricare il file
  response.headers["Content-Disposition"] = (
      f"attachment; filename*=UTF-8''{urllib.parse.quote(decoded_filename)}"
  )
  return response


if __name__ == "__main__":
  app.run(host="0.0.0.0", port=5000)