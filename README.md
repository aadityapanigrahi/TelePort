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

2.  **Install the tool:**
    This will install dependencies and register the `teleport` command globally (or in your virtual environment).
    ```bash
    pip install .
    ```
    *For development (editable mode):*
    ```bash
    pip install -e .
    ```

3.  **Configure credentials:**
    Copy `.env.example` to `.env` and add your Telegram Bot Token and Chat ID.
    ```bash
    cp .env.example .env
    # Edit .env with your credentials
    ```

## Usage

Once installed, you can use the `teleport` command from anywhere in your terminal.

**Send a message:**
```bash
teleport send-text "Hello from TelePort!"
```

**Send an image:**
```bash
teleport send-image path/to/image.jpg --caption "Cool photo"
```

**Send a file:**
```bash
teleport send-file path/to/document.pdf
```

**Listen for messages:**
```bash
teleport listen
```

## License

MIT
