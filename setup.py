from setuptools import setup, find_packages

setup(
    name='teleport-cli',
    version='0.2.0',
    description='Remote-control your Mac via Telegram with AI-powered task execution.',
    long_description=open('README.md').read(),
    long_description_content_type='text/markdown',
    author='Aaditya Panigrahi',
    author_email='aadityaapanigrahi@gmail.com',
    url='https://github.com/aadityapanigrahi/TelePort',
    package_dir={'': 'src'},
    py_modules=['telegram_tool', 'daemon', 'ai_router', 'task_runner', 'mcp_server'],
    install_requires=[
        'python-telegram-bot',
        'typer[all]',
        'python-dotenv',
        'rich',
        'mcp[cli]',
    ],
    entry_points={
        'console_scripts': [
            'teleport=telegram_tool:app',
        ],
    },
    classifiers=[
        'Programming Language :: Python :: 3',
        'License :: OSI Approved :: MIT License',
        'Operating System :: OS Independent',
    ],
    python_requires='>=3.10',
)

