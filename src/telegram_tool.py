import os
import asyncio
import typer
from datetime import datetime
from dotenv import load_dotenv
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from rich.console import Console

# Load environment variables from .env file
load_dotenv()

app = typer.Typer()
console = Console()

# Global Config
CONVERSATION_DIR = "conversation"
FILES_DIR = "transaction_files"
LOG_FILE = os.path.join(CONVERSATION_DIR, "messages.log")

def log_message(direction: str, user_name: str, text: str, message_type: str = "text"):
    """Log a message with timestamp and direction (incoming/outgoing)."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    os.makedirs(CONVERSATION_DIR, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(f"[{timestamp}] {direction} | {user_name} | {message_type}: {text}\n")

def get_credentials():
    """Retrieve credentials from environment variables."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    
    if not token:
        console.print("[bold red]Error:[/bold red] TELEGRAM_BOT_TOKEN not found in .env file.")
        raise typer.Exit(code=1)
        
    return token, chat_id

async def send_text_async(text: str):
    token, chat_id = get_credentials()
    if not chat_id:
        console.print("[bold red]Error:[/bold red] TELEGRAM_CHAT_ID not found in .env file.")
        raise typer.Exit(code=1)

    try:
        bot = Bot(token=token)
        await bot.send_message(chat_id=chat_id, text=text)
        console.print(f"[bold green]Message sent successfully![/bold green]")
        log_message("OUTGOING", "You", text, "text")
    except Exception as e:
        console.print(f"[bold red]Failed to send message:[/bold red] {e}")
        raise typer.Exit(code=1)

@app.command()
def send_text(message: str):
    """Send a text message to the configured Chat ID."""
    asyncio.run(send_text_async(message))

async def send_image_async(image_path: str, caption: str = None):
    token, chat_id = get_credentials()
    if not chat_id:
        console.print("[bold red]Error:[/bold red] TELEGRAM_CHAT_ID not found in .env file.")
        raise typer.Exit(code=1)

    if not os.path.exists(image_path):
        console.print(f"[bold red]Error:[/bold red] File not found: {image_path}")
        raise typer.Exit(code=1)

    try:
        bot = Bot(token=token)
        with open(image_path, 'rb') as img:
            await bot.send_photo(chat_id=chat_id, photo=img, caption=caption)
        console.print(f"[bold green]Image sent successfully![/bold green]")
        log_message("OUTGOING", "You", f"Image: {os.path.basename(image_path)}" + (f" | Caption: {caption}" if caption else ""), "image")
    except Exception as e:
        console.print(f"[bold red]Failed to send image:[/bold red] {e}")
        raise typer.Exit(code=1)

@app.command()
def send_image(path: str, caption: str = typer.Option(None, help="Optional caption for the image")):
    """Send an image to the configured Chat ID. Note: Telegram compresses images sent this way."""
    asyncio.run(send_image_async(path, caption))

async def send_file_async(file_path: str, caption: str = None):
    token, chat_id = get_credentials()
    if not chat_id:
        console.print("[bold red]Error:[/bold red] TELEGRAM_CHAT_ID not found in .env file.")
        raise typer.Exit(code=1)

    if not os.path.exists(file_path):
        console.print(f"[bold red]Error:[/bold red] File not found: {file_path}")
        raise typer.Exit(code=1)

    try:
        bot = Bot(token=token)
        with open(file_path, 'rb') as f:
            await bot.send_document(chat_id=chat_id, document=f, caption=caption)
        console.print(f"[bold green]File sent successfully![/bold green]")
        log_message("OUTGOING", "You", f"File: {os.path.basename(file_path)}" + (f" | Caption: {caption}" if caption else ""), "file")
    except Exception as e:
        console.print(f"[bold red]Failed to send file:[/bold red] {e}")
        raise typer.Exit(code=1)

@app.command()
def send_file(path: str, caption: str = typer.Option(None, help="Optional caption for the file")):
    """Send a file (document) to the configured Chat ID. Preserves original format (e.g. PNG)."""
    asyncio.run(send_file_async(path, caption))

# --- Listening Logic ---

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text
    console.print(f"[bold blue]Received message from {user.first_name} (ID: {user.id}):[/bold blue] {text}")
    
    # Save to log
    log_message("INCOMING", user.first_name, text, "text")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    photo_file = await update.message.photo[-1].get_file()
    
    os.makedirs(FILES_DIR, exist_ok=True)
    file_path = os.path.join(FILES_DIR, f"{update.message.id}.jpg")
    
    await photo_file.download_to_drive(file_path)
    
    caption = update.message.caption or ""
    console.print(f"[bold blue]Received photo from {user.first_name}:[/bold blue] Saved to {file_path}. Caption: {caption}")
    log_message("INCOMING", user.first_name, f"Photo saved: {os.path.basename(file_path)}" + (f" | Caption: {caption}" if caption else ""), "photo")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    document = update.message.document
    file_name = document.file_name or f"doc_{update.message.id}"
    
    os.makedirs(FILES_DIR, exist_ok=True)
    file_path = os.path.join(FILES_DIR, file_name)
    
    doc_file = await document.get_file()
    await doc_file.download_to_drive(file_path)
    
    caption = update.message.caption or ""
    console.print(f"[bold blue]Received document from {user.first_name}:[/bold blue] Saved to {file_path}. Caption: {caption}")
    log_message("INCOMING", user.first_name, f"Document saved: {file_name}" + (f" | Caption: {caption}" if caption else ""), "document")

@app.command()
def listen(output_dir: str = typer.Option("transaction_files", help="Directory to save received media")):
    """Listen for incoming messages and images."""
    token, _ = get_credentials()
    
    global FILES_DIR
    FILES_DIR = output_dir
    
    console.print(f"[bold green]Listening for messages... (Saving to '{FILES_DIR}') (Press Ctrl+C to stop)[/bold green]")
    
    application = Application.builder().token(token).build()
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    app()
