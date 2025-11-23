# TelePort 🚀

**TelePort** is a lightweight, powerful CLI tool for interacting with Telegram. Send messages, images, and files directly from your terminal, or listen for incoming media with ease.

## Features

- 📨 **Send Text**: Instantly send messages to your configured chat.
- 📸 **Send Images**: Share photos with optional captions.
- 📁 **Send Files**: Transfer documents while preserving their original format.
- 👂 **Listen Mode**: Real-time monitoring for incoming messages and media downloads.

## Installation

1.  **Clone the repository:**
    ```bash
    git clone <your-repo-url>
    cd TelePort
    ```

2.  **Install dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

3.  **Configure credentials:**
    Copy `.env.example` to `.env` and add your Telegram Bot Token and Chat ID.
    ```bash
    cp .env.example .env
    # Edit .env with your credentials
    ```

## Usage

You can run the tool directly using Python or the provided wrapper script.

### Using the Wrapper Script

Make the script executable (first time only):
```bash
chmod +x bin/teleport
```

**Send a message:**
```bash
./bin/teleport send-text "Hello from TelePort!"
```

**Send an image:**
```bash
./bin/teleport send-image path/to/image.jpg --caption "Cool photo"
```

**Send a file:**
```bash
./bin/teleport send-file path/to/document.pdf
```

**Listen for messages:**
```bash
./bin/teleport listen
```

### Using Python Directly

```bash
python src/telegram_tool.py send-text "Hello World"
```

## License

MIT
