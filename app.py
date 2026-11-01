import os
from flask import Flask, send_from_directory, render_template_string, abort

app = Flask(__name__)
CANZONI_FOLDER = os.path.abspath("canzoni")

# HTML minimale per la pagina principale con la lista delle canzoni e il download forzato
HOME_TEMPLATE = """
<!DOCTYPE html>
<html lang="it">
<head>
    <meta charset="UTF-8">
    <title>Libreria Audio</title>
    <style>
        body { font-family: sans-serif; max-width: 600px; margin: 40px auto; padding: 0 20px; background: #f4f4f9; color: #333; }
        h1 { color: #5865F2; }
        ul { list-style: none; padding: 0; }
        li { background: white; margin: 10px 0; padding: 15px; border-radius: 8px; display: flex; justify-content: space-between; align-items: center; box-shadow: 0 2px 5px rgba(0,0,0,0.05); }
        a { background: #5865F2; color: white; padding: 8px 15px; border-radius: 5px; text-decoration: none; font-weight: bold; }
        a:hover { background: #4752c4; }
    </style>
</head>
<body>
    <h1>🎵 Libreria MP3</h1>
    <p>Clicca su una canzone per avviare il download automatico:</p>
    <ul>
        {% for song in songs %}
            <li>
                <span>{{ song }}</span>
                <a href="/download/{{ song }}">📥 Scarica</a>
            </li>
        {% endfor %}
    </ul>
</body>
</html>
"""

@app.route("/")
def index():
    if not os.path.exists(CANZONI_FOLDER):
        os.makedirs(CANZONI_FOLDER)
    songs = [f for f in os.listdir(CANZONI_FOLDER) if f.lower().endswith(".mp3")]
    return render_template_string(HOME_TEMPLATE, songs=songs)

@app.route("/download/<path:filename>")
def download_file(filename):
    # L'as_attachment=True forza il browser a scaricare il file anziché aprirlo nel player!
    try:
        return send_from_directory(CANZONI_FOLDER, filename, as_attachment=True)
    except FileNotFoundError:
        abort(404)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)