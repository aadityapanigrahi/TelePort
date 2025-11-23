# TelePort

A command-line tool for sending and receiving Telegram messages.

## Features

- Send text messages
- Send images with optional captions
- Send files (documents are sent uncompressed, preserving original format)
- Listen for incoming messages and automatically download media

## Installation

Clone the repository:
```bash
git clone https://github.com/aadityapanigrahi/TelePort.git
cd TelePort
```

Install with pip:
```bash
pip install .
```

For development, install in editable mode:
```bash
pip install -e .
```

## Configuration

Create a `.env` file with your Telegram credentials:
```bash
cp .env.example .env
```

Edit `.env` and add:
- `TELEGRAM_BOT_TOKEN`: Your bot token from BotFather
- `TELEGRAM_CHAT_ID`: The chat ID where messages will be sent

## Usage

Send a text message:
```bash
teleport send-text "Hello"
```

Send an image:
```bash
teleport send-image photo.jpg --caption "My photo"
```

Send a file:
```bash
teleport send-file document.pdf
```

Listen for incoming messages:
```bash
teleport listen
```

Messages and media are logged to `conversation/messages.log` and `transaction_files/` respectively.

## License

MIT
