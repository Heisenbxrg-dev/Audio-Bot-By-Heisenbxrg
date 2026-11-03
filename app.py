"""Proxy Flask per la libreria musicale su Cloudflare R2.

Variabili d'ambiente (da impostare su Render, MAI nel codice):
  R2_ENDPOINT_URL        es. https://<account>.r2.cloudflarestorage.com
  R2_ACCESS_KEY_ID       token R2 con permesso "Object Read only"
  R2_SECRET_ACCESS_KEY
  BOT_API_KEY            stringa casuale lunga, uguale a quella del bot
  LINK_SECRET            stringa casuale lunga, serve a firmare i link di download
  R2_BUCKET_NAME         (opzionale) default "jw-music"
  LINK_TTL               (opzionale) validita' dei link in secondi, default 86400
  PUBLIC_BASE_URL        (opzionale) es. https://tuo-sito.onrender.com

Avvio su Render:
  gunicorn app:app --workers 1 --threads 4 --timeout 60

Dipendenze: Flask, boto3, mutagen, gunicorn
"""

import hashlib
import hmac
import io
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlencode
import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from flask import Flask, abort, jsonify, redirect, request, send_file
from mutagen.id3 import ID3
from werkzeug.middleware.proxy_fix import ProxyFix
try:
  from dotenv import load_dotenv
  load_dotenv()
except ImportError:
  pass
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("audio-proxy")


# ==================== CONFIGURAZIONE ====================
def require_env(name):
  value = os.environ.get(name)
  if not value:
    raise RuntimeError(f"Variabile d'ambiente mancante: {name}")
  return value


R2_ENDPOINT_URL = require_env("R2_ENDPOINT_URL")
R2_ACCESS_KEY_ID = require_env("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = require_env("R2_SECRET_ACCESS_KEY")
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME", "jw-music")
BOT_API_KEY = require_env("BOT_API_KEY")
LINK_SECRET = require_env("LINK_SECRET").encode("utf-8")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
LINK_TTL = int(os.environ.get("LINK_TTL", "86400"))  # validita' link download

LIST_TTL = 300  # secondi prima di rileggere la lista da R2
INITIAL_RANGE = 256 * 1024  # primi byte letti per trovare il tag ID3
MAX_ID3_BYTES = 20 * 1024 * 1024  # tag piu' grandi vengono ignorati
COVER_CACHE_MAX_BYTES = 64 * 1024 * 1024  # tetto RAM per le copertine
WARMUP_WORKERS = 4
DOWNLOAD_URL_TTL = 300  # validita' dell'URL presigned verso R2 (secondi)
YEAR_FRAMES = ("TDRC", "TYER", "TDRL", "TDOR", "TORY")

s3 = boto3.client(
    "s3",
    endpoint_url=R2_ENDPOINT_URL,
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    region_name="auto",
    config=Config(
        signature_version="s3v4",
        retries={"max_attempts": 3, "mode": "standard"},
        connect_timeout=5,
        read_timeout=20,
    ),
)

app = Flask(__name__)
# Render sta dietro un proxy: serve per avere https negli URL generati.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

for _code, _text in (
    (400, "Richiesta non valida"),
    (403, "Accesso negato"),
    (404, "Non trovato"),
    (502, "Errore del servizio di storage"),
):
  app.register_error_handler(_code, lambda e, t=_text, c=_code: (t, c))


# ==================== STATO IN MEMORIA ====================
_lock = threading.RLock()
_file_index = {}  # chiave -> ETag
_file_list = []  # chiavi ordinate
_list_ts = 0.0
_meta_cache = {}  # chiave -> dict metadati
_cover_cache = OrderedDict()  # chiave -> (bytes, mime), ordine LRU
_cover_bytes = 0


def _drop_cache(key):
  """Elimina i dati in cache di un file. Chiamare con _lock acquisito."""
  global _cover_bytes
  _meta_cache.pop(key, None)
  old = _cover_cache.pop(key, None)
  if old is not None:
    _cover_bytes -= len(old[0])


def _store_cover(key, cover):
  """Salva una copertina nella cache LRU. Chiamare con _lock acquisito."""
  global _cover_bytes
  data, mime = cover
  if len(data) > COVER_CACHE_MAX_BYTES:
    return
  old = _cover_cache.pop(key, None)
  if old is not None:
    _cover_bytes -= len(old[0])
  _cover_cache[key] = (data, mime)
  _cover_bytes += len(data)
  while _cover_bytes > COVER_CACHE_MAX_BYTES and _cover_cache:
    _, (evicted, _) = _cover_cache.popitem(last=False)
    _cover_bytes -= len(evicted)


def is_missing(exc):
  if isinstance(exc, ClientError):
    code = str(exc.response.get("Error", {}).get("Code", ""))
    return code in {"NoSuchKey", "404", "NotFound"}
  return False


# ==================== LISTA FILE ====================
def get_file_list(force=False):
  """Lista degli mp3 nel bucket, con cache a TTL. Invalida i file modificati."""
  global _file_list, _file_index, _list_ts
  with _lock:
    fresh = _list_ts and (time.time() - _list_ts) < LIST_TTL
    if fresh and not force:
      return list(_file_list)

  try:
    index = {}
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=R2_BUCKET_NAME):
      for obj in page.get("Contents", []):
        key = obj["Key"]
        if key.lower().endswith(".mp3"):
          index[key] = obj.get("ETag", "")
  except (ClientError, BotoCoreError) as exc:
    log.warning("Errore lettura lista da R2: %s", exc)
    with _lock:
      return list(_file_list)  # meglio una lista vecchia che nessuna lista

  with _lock:
    for key, etag in index.items():
      previous = _file_index.get(key)
      if previous is not None and previous != etag:
        _drop_cache(key)  # file sostituito: rilegge i tag
    for key in set(_file_index) - set(index):
      _drop_cache(key)  # file rimosso
    _file_index = index
    _file_list = sorted(index, key=str.lower)
    _list_ts = time.time()
    return list(_file_list)


def is_known(key):
  get_file_list()
  with _lock:
    return key in _file_index


# ==================== LETTURA TAG ID3 (solo l'inizio del file) ====================
def _fetch_id3_bytes(key):
  """Scarica SOLO il tag ID3 (Range request) invece dell'mp3 intero."""
  try:
    resp = s3.get_object(
        Bucket=R2_BUCKET_NAME, Key=key, Range=f"bytes=0-{INITIAL_RANGE - 1}"
    )
  except ClientError as exc:
    if exc.response.get("Error", {}).get("Code") == "InvalidRange":
      return None  # file vuoto
    raise
  data = resp["Body"].read()
  if len(data) < 10 or data[:3] != b"ID3":
    return None

  size = (
      ((data[6] & 0x7F) << 21)
      | ((data[7] & 0x7F) << 14)
      | ((data[8] & 0x7F) << 7)
      | (data[9] & 0x7F)
  )
  total = 10 + size + (10 if data[5] & 0x10 else 0)  # header + corpo + footer
  if total > MAX_ID3_BYTES:
    return None
  if total > len(data) and len(data) >= INITIAL_RANGE:
    rest = s3.get_object(
        Bucket=R2_BUCKET_NAME, Key=key, Range=f"bytes={len(data)}-{total - 1}"
    )["Body"].read()
    data += rest
  return io.BytesIO(data[:total])


def _fetch_tags(key):
  raw = _fetch_id3_bytes(key)
  if raw is None:
    return None
  try:
    return ID3(raw)
  except Exception as exc:  # tag corrotto o non standard
    log.debug("Tag ID3 illeggibili per %s: %s", key, exc)
    return None


def _extract_era(tags, key):
  if tags is not None:
    for frame_id in YEAR_FRAMES:
      frame = tags.get(frame_id)
      if frame is not None and getattr(frame, "text", None):
        match = re.search(r"\d{4}", str(frame.text[0]))
        if match:
          return match.group(0)
  match = re.search(r"\b(19\d{2}|20\d{2})\b", key)  # ultimo tentativo: percorso
  return match.group(1) if match else "N/D"


def _extract_cover(tags):
  if tags is None:
    return None
  pictures = tags.getall("APIC")
  if not pictures:
    return None
  picture = next((p for p in pictures if p.type == 3), pictures[0])  # 3 = front
  data = picture.data
  if not data:
    return None
  mime = (picture.mime or "").lower()
  if mime == "image/jpg":
    mime = "image/jpeg"
  if not mime.startswith("image/"):
    mime = "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"
  return data, mime


def load_track_info(key):
  """Metadati di un brano (con cache). Solleva ClientError se R2 non risponde."""
  with _lock:
    cached = _meta_cache.get(key)
  if cached is not None:
    return cached

  parts = key.split("/")
  album = parts[-2] if len(parts) > 1 else "Cloudflare R2"
  title = os.path.splitext(parts[-1])[0]

  tags = _fetch_tags(key)
  cover = _extract_cover(tags)
  info = {
      "title": title,
      "album": album,
      "era": _extract_era(tags, key),
      "has_cover": cover is not None,
  }
  with _lock:
    _meta_cache[key] = info
    if cover is not None:
      _store_cover(key, cover)
  return info


def get_cover(key):
  """Restituisce (bytes, mime) oppure None."""
  with _lock:
    entry = _cover_cache.get(key)
    if entry is not None:
      _cover_cache.move_to_end(key)
      return entry

  info = load_track_info(key)
  if not info["has_cover"]:
    return None
  with _lock:
    entry = _cover_cache.get(key)
  if entry is not None:
    return entry

  # La copertina e' stata espulsa dalla cache LRU: la rileggo.
  cover = _extract_cover(_fetch_tags(key))
  if cover is not None:
    with _lock:
      _store_cover(key, cover)
  return cover


# ==================== SICUREZZA ====================
def require_bot_key():
  supplied = request.headers.get("X-Bot-Key", "").encode("utf-8")
  if not hmac.compare_digest(supplied, BOT_API_KEY.encode("utf-8")):
    abort(403)


def make_sig(key, exp):
  message = f"{key}|{exp}".encode("utf-8")
  return hmac.new(LINK_SECRET, message, hashlib.sha256).hexdigest()


def base_url():
  return PUBLIC_BASE_URL or request.url_root.rstrip("/")


def content_disposition(filename):
  ascii_name = re.sub(r"[^A-Za-z0-9._ -]", "_", filename) or "track.mp3"
  return (
      f'attachment; filename="{ascii_name}"; '
      f"filename*=UTF-8''{quote(filename)}"
  )


# ==================== PRE-CARICAMENTO ====================
def _safe_load(key):
  try:
    load_track_info(key)
  except Exception as exc:
    log.warning("Pre-cache fallita per %s: %s", key, exc)


def warm_up():
  try:
    keys = get_file_list(force=True)
    log.info("Warm-up: %d brani, lettura tag in corso", len(keys))
    with ThreadPoolExecutor(max_workers=WARMUP_WORKERS) as pool:
      list(pool.map(_safe_load, keys))
    log.info("Warm-up completato")
  except Exception:
    log.exception("Errore durante il warm-up")


_warm_started = False


def start_warm_up():
  global _warm_started
  with _lock:
    if _warm_started:
      return
    _warm_started = True
  threading.Thread(target=warm_up, daemon=True, name="warm-up").start()


start_warm_up()


# ==================== ROUTE ====================
@app.get("/")
def home():
  return "Audio proxy online"


@app.get("/api/list")
def api_list():
  require_bot_key()
  return jsonify(get_file_list(force=request.args.get("refresh") == "1"))


@app.get("/api/metadata")
def api_metadata():
  require_bot_key()
  key = request.args.get("file", "")
  if not key or not is_known(key):
    abort(404)
  try:
    info = load_track_info(key)
  except (ClientError, BotoCoreError) as exc:
    if is_missing(exc):
      abort(404)
    log.warning("Errore metadati per %s: %s", key, exc)
    abort(502)

  expires_at = int(time.time()) + LINK_TTL
  query = urlencode(
      {"file": key, "exp": expires_at, "sig": make_sig(key, expires_at)}
  )
  payload = dict(info)
  payload["download_url"] = f"{base_url()}/download?{query}"
  payload["expires_at"] = expires_at
  if info["has_cover"]:
    payload["cover_url"] = f"{base_url()}/cover?{urlencode({'file': key})}"
  return jsonify(payload)


@app.get("/cover")
def cover():
  # Pubblico (serve a Discord per la miniatura) ma solo per brani esistenti.
  key = request.args.get("file", "")
  if not key or not is_known(key):
    abort(404)
  try:
    result = get_cover(key)
  except (ClientError, BotoCoreError) as exc:
    if is_missing(exc):
      abort(404)
    log.warning("Errore copertina per %s: %s", key, exc)
    abort(502)
  if result is None:
    abort(404)
  data, mime = result
  return send_file(io.BytesIO(data), mimetype=mime, max_age=86400)


@app.get("/download")
def download():
  key = request.args.get("file", "")
  exp = request.args.get("exp", "")
  sig = request.args.get("sig", "")

  if not key or not exp.isdigit() or int(exp) < time.time():
    abort(403)
  if not hmac.compare_digest(sig.encode("utf-8"), make_sig(key, exp).encode("utf-8")):
    abort(403)
  if not key.lower().endswith(".mp3"):
    abort(403)

  # Il download parte direttamente da R2: Render non trasporta il file.
  url = s3.generate_presigned_url(
      "get_object",
      Params={
          "Bucket": R2_BUCKET_NAME,
          "Key": key,
          "ResponseContentDisposition": content_disposition(
              os.path.basename(key)
          ),
          "ResponseContentType": "audio/mpeg",
      },
      ExpiresIn=DOWNLOAD_URL_TTL,
  )
  return redirect(url, code=302)


if __name__ == "__main__":
  app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
