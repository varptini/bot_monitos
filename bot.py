import os
import io
import logging

import requests
from PIL import Image, ImageColor
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import anthropic

load_dotenv()

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
    "blanco": "#FFFFFF",
    "crema":  "#F5F0E8",
    "rosa":   "#FFE4E1",
    "gris":   "#F0F0F0",
    "negro":  "#1A1A1A",
}
DEFAULT_BG = "crema"


# ── Procesado de imagen ──────────────────────────────────────────────────────

def remove_background(image_bytes: bytes) -> bytes:
    response = requests.post(
        "https://api.remove.bg/v1.0/removebg",
        files={"image_file": ("photo.jpg", image_bytes, "image/jpeg")},
        data={"size": "auto"},
        headers={"X-Api-Key": REMOVEBG_API_KEY},
        timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(f"remove.bg error {response.status_code}: {response.text}")
    return response.content  # PNG con transparencia


def add_background(foreground_png: bytes, hex_color: str) -> bytes:
    fg = Image.open(io.BytesIO(foreground_png)).convert("RGBA")
    bg = Image.new("RGBA", fg.size, ImageColor.getrgb(hex_color) + (255,))
    bg.paste(fg, mask=fg.split()[3])
    out = io.BytesIO()
    bg.convert("RGB").save(out, format="JPEG", quality=95)
    return out.getvalue()


# ── IA: texto vendedor ───────────────────────────────────────────────────────

def generate_sales_text() -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model="claude-sonnet-4-6",
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
        "Envíame una foto y yo:\n"
        "1️⃣ Elimino el fondo y pongo uno elegante\n"
        "2️⃣ Genero un texto vendedor con IA\n"
        "3️⃣ Publico en Facebook e Instagram\n\n"
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
    context.user_data["bg_name"] = nombre
    context.user_data["bg_hex"]  = BACKGROUNDS[nombre]
    await update.message.reply_text(f"✅ Fondo cambiado a *{nombre}*", parse_mode="Markdown")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    status = await update.message.reply_text("📸 Foto recibida, procesando…")
    try:
        # Descargar foto en máxima resolución
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        image_bytes = bytes(await file.download_as_bytearray())

        await status.edit_text("🎨 Eliminando fondo…")
        no_bg = remove_background(image_bytes)

        hex_color = context.user_data.get("bg_hex", BACKGROUNDS[DEFAULT_BG])
        final_jpg = add_background(no_bg, hex_color)

        await status.edit_text("✍️ Generando texto vendedor…")
        caption = generate_sales_text()

        await status.edit_text("📤 Publicando en Facebook e Instagram…")
        image_url = post_to_facebook(final_jpg, caption)
        post_to_instagram(image_url, caption)

        await context.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=final_jpg,
            caption=f"✅ ¡Publicado!\n\n{caption}",
        )
        await status.delete()

    except Exception as exc:
        logger.exception("Error procesando foto")
        await status.edit_text(f"❌ Error: {exc}\n\nInténtalo de nuevo.")


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("fondo", cmd_fondo))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    logger.info("Bot arrancado ✅")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
