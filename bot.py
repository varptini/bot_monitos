import os
import io
import sys
import asyncio
import logging

import requests
from PIL import Image, ImageColor, ImageDraw, ImageFont
from rembg import remove as rembg_remove, new_session
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import anthropic

load_dotenv()

# ── Backend de segmentación: NPU preferido, rembg como fallback ──────────────
sys.path.insert(0, os.path.expanduser("~/"))   # permite importar procesa_moño.py
_USE_NPU = False
try:
    from procesa_moño import procesar_bytes_moño as _npu_segment
    _USE_NPU = True
    logging.getLogger(__name__).info("Segmentación: NPU (Allwinner AIPU) activo")
except Exception as _npu_err:
    logging.getLogger(__name__).warning(
        "NPU no disponible (%s). Usando rembg como fallback.", _npu_err
    )

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
REMOVEBG_API_KEY = os.getenv("REMOVEBG_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
FB_PAGE_ID       = os.getenv("FB_PAGE_ID")
FB_ACCESS_TOKEN  = os.getenv("FB_ACCESS_TOKEN")
IG_ACCOUNT_ID    = os.getenv("IG_ACCOUNT_ID")

BACKGROUNDS = {
    "perla":      ("#FFFFFF",  "#EDE0CC"),
    "rosa_gold":  ("#FFE4E1",  "#C49A9A"),
    "lila":       ("#EDE0F5",  "#B89AC8"),
    "champagne":  ("#F5F0E8",  "#C8A878"),
    "carbon":     ("#4A4A4A",  "#1A1A1A"),
}
DEFAULT_BG = "perla"
STORE_NAME = "Novias y Novedades Nuquito"
FONT_PATH  = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

_rembg_session = None


def get_rembg_session():
    global _rembg_session
    if _rembg_session is None:
        _rembg_session = new_session("birefnet-general")
    return _rembg_session


# ── Procesado de imagen ──────────────────────────────────────────────────────

def remove_background(image_bytes: bytes) -> bytes:
    """
    Elimina el fondo de la imagen.
    Usa el NPU (AIPU) si está disponible; si no, cae a rembg en CPU.
    Siempre devuelve PNG bytes con canal alfa.
    """
    if _USE_NPU:
        try:
            result = _npu_segment(image_bytes)
            if result is not None:
                return result
            logger.warning("NPU no detectó moño, usando rembg como fallback…")
        except Exception as npu_exc:
            logger.warning("NPU falló en tiempo de ejecución (%s), usando rembg…", npu_exc)
    return rembg_remove(image_bytes, session=get_rembg_session())


def make_gradient(size: tuple, color1: str, color2: str) -> Image.Image:
    w, h = size
    c1 = ImageColor.getrgb(color1)
    c2 = ImageColor.getrgb(color2)
    pixels = []
    for y in range(h):
        t = y / (h - 1) if h > 1 else 0
        r = int(c1[0] + (c2[0] - c1[0]) * t)
        g = int(c1[1] + (c2[1] - c1[1]) * t)
        b = int(c1[2] + (c2[2] - c1[2]) * t)
        pixels.extend([(r, g, b)] * w)
    img = Image.new("RGB", size)
    img.putdata(pixels)
    return img


def add_watermark(img: Image.Image) -> Image.Image:
    w, h = img.size
    font_size = max(20, w // 28)
    try:
        font = ImageFont.truetype(FONT_PATH, font_size)
    except Exception:
        font = ImageFont.load_default()

    draw = ImageDraw.Draw(img, "RGBA")
    bbox = draw.textbbox((0, 0), STORE_NAME, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    margin = font_size // 2
    x = (w - tw) // 2
    y = h - th - margin * 2

    # franja semitransparente de fondo
    draw.rectangle([0, y - margin, w, h], fill=(0, 0, 0, 100))
    # sombra
    draw.text((x + 1, y + 1), STORE_NAME, font=font, fill=(0, 0, 0, 180))
    # texto blanco
    draw.text((x, y), STORE_NAME, font=font, fill=(255, 255, 255, 230))
    return img


def add_background(foreground_png: bytes, colors: tuple) -> bytes:
    fg = Image.open(io.BytesIO(foreground_png)).convert("RGBA")
    bg = make_gradient(fg.size, colors[0], colors[1]).convert("RGBA")
    bg.paste(fg, mask=fg.split()[3])
    result = add_watermark(bg.convert("RGB"))
    out = io.BytesIO()
    result.save(out, format="JPEG", quality=95)
    return out.getvalue()


# ── IA: texto vendedor ───────────────────────────────────────────────────────

def generate_sales_text() -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": (
                "Genera un texto vendedor corto y atractivo para publicar en redes sociales "
                "sobre un moño artesanal hecho a mano. "
                "Incluye emojis, 2-3 hashtags relevantes y un llamado a la acción. "
                "Máximo 120 palabras. Tono: elegante, femenino, artesanal."
            ),
        }],
    )
    return msg.content[0].text


# ── Meta: Facebook e Instagram ───────────────────────────────────────────────

def post_to_facebook(image_bytes: bytes, caption: str) -> str:
    """Publica foto en la Página de Facebook. Devuelve la URL pública de la imagen."""
    resp = requests.post(
        f"https://graph.facebook.com/v19.0/{FB_PAGE_ID}/photos",
        files={"source": ("photo.jpg", image_bytes, "image/jpeg")},
        data={"caption": caption, "access_token": FB_ACCESS_TOKEN},
        timeout=30,
    )
    resp.raise_for_status()
    photo_id = resp.json()["id"]

    # Obtener URL pública para reutilizarla en Instagram
    url_resp = requests.get(
        f"https://graph.facebook.com/v19.0/{photo_id}",
        params={"fields": "images", "access_token": FB_ACCESS_TOKEN},
        timeout=15,
    )
    url_resp.raise_for_status()
    return url_resp.json()["images"][0]["source"]


def post_to_instagram(image_url: str, caption: str) -> None:
    """Publica en Instagram Business usando la URL pública obtenida de Facebook."""
    # Paso 1: crear contenedor de media
    container = requests.post(
        f"https://graph.facebook.com/v19.0/{IG_ACCOUNT_ID}/media",
        data={
            "image_url": image_url,
            "caption": caption,
            "access_token": FB_ACCESS_TOKEN,
        },
        timeout=30,
    )
    container.raise_for_status()
    container_id = container.json()["id"]

    # Paso 2: publicar el contenedor
    publish = requests.post(
        f"https://graph.facebook.com/v19.0/{IG_ACCOUNT_ID}/media_publish",
        data={"creation_id": container_id, "access_token": FB_ACCESS_TOKEN},
        timeout=30,
    )
    publish.raise_for_status()


# ── Handlers de Telegram ─────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    opciones = "\n".join(f"  /fondo {k}" for k in BACKGROUNDS)
    await update.message.reply_text(
        "¡Hola! Soy el bot de moñitos 🎀\n\n"
        "Envíame una foto (o como archivo para máxima calidad) y yo:\n"
        "1️⃣ Elimino el fondo y pongo un degradado elegante\n"
        "2️⃣ Genero un texto vendedor con IA\n"
        f"3️⃣ Publico en {'Facebook e Instagram' if IG_ACCOUNT_ID else 'Facebook'}\n\n"
        f"Fondos disponibles:\n{opciones}\n\n"
        f"Fondo actual: *{context.user_data.get('bg_name', DEFAULT_BG)}*",
        parse_mode="Markdown",
    )


async def cmd_fondo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or context.args[0] not in BACKGROUNDS:
        opciones = "  ".join(f"/fondo {k}" for k in BACKGROUNDS)
        await update.message.reply_text(f"Opciones: {opciones}")
        return
    nombre = context.args[0]
    context.user_data["bg_name"]   = nombre
    context.user_data["bg_colors"] = BACKGROUNDS[nombre]
    await update.message.reply_text(f"✅ Fondo cambiado a *{nombre}*", parse_mode="Markdown")


async def process_and_publish(image_bytes: bytes, status, context) -> None:
    await status.edit_text("🎨 Procesando imagen y generando texto…")

    loop = asyncio.get_event_loop()
    colors = context.user_data.get("bg_colors", BACKGROUNDS[DEFAULT_BG])

    no_bg, caption = await asyncio.gather(
        loop.run_in_executor(None, remove_background, image_bytes),
        loop.run_in_executor(None, generate_sales_text),
    )

    final_jpg = add_background(no_bg, colors)

    await status.edit_text("📤 Publicando en Facebook…")
    image_url = post_to_facebook(final_jpg, caption)

    redes = "Facebook"
    if IG_ACCOUNT_ID:
        post_to_instagram(image_url, caption)
        redes = "Facebook e Instagram"

    await context.bot.send_photo(
        chat_id=status.chat_id,
        photo=final_jpg,
        caption=f"✅ ¡Publicado en {redes}!\n\n{caption}",
    )
    await status.delete()


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    status = await update.message.reply_text("📸 Foto recibida, procesando…")
    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        image_bytes = bytes(await file.download_as_bytearray())
        await process_and_publish(image_bytes, status, context)
    except Exception as exc:
        logger.exception("Error procesando foto")
        await status.edit_text(f"❌ Error: {exc}\n\nInténtalo de nuevo.")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    doc = update.message.document
    if not doc.mime_type or not doc.mime_type.startswith("image/"):
        return
    status = await update.message.reply_text("📸 Imagen recibida (alta calidad), procesando…")
    try:
        file = await context.bot.get_file(doc.file_id)
        image_bytes = bytes(await file.download_as_bytearray())
        await process_and_publish(image_bytes, status, context)
    except Exception as exc:
        logger.exception("Error procesando documento")
        await status.edit_text(f"❌ Error: {exc}\n\nInténtalo de nuevo.")


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("fondo", cmd_fondo))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    logger.info("Bot arrancado ✅")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
