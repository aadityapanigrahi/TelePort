from setuptools import setup

setup(
    name='teleport-cli',
    version='0.1.0',
    description='A lightweight, powerful CLI tool for interacting with Telegram.',
    author='Your Name',
    author_email='your.email@example.com',
    package_dir={'': 'src'},
    py_modules=['telegram_tool'],
    install_requires=[
        'python-telegram-bot',
        'typer[all]',
        'python-dotenv',
        'rich',
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
    python_requires='>=3.7',
)
