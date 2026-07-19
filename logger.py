from datetime import datetime
from pathlib import Path
import os


APP_NAME = "Beabots"

# User-writable application folder
APP_DATA = Path(os.getenv("LOCALAPPDATA")) / APP_NAME
LOG_DIR = APP_DATA / "logs"

LOG_DIR.mkdir(parents=True, exist_ok=True)


class Logger:
    def __init__(self):
        self.callback = None

        filename = datetime.now().strftime("%Y-%m-%d") + ".log"
        self.logfile = LOG_DIR / filename

        with open(self.logfile, "a", encoding="utf-8") as f:
            f.write("\n")
            f.write("=" * 80 + "\n")
            f.write(
                f"Automation Started : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            )
            f.write("=" * 80 + "\n")

    def set_callback(self, callback):
        self.callback = callback

    def _write(self, level, message):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        text = f"[{timestamp}] [{level}] {message}"

        print(text)

        with open(self.logfile, "a", encoding="utf-8") as f:
            f.write(text + "\n")
            f.flush()

        if self.callback:
            try:
                # Preferred: callback(text, level) — lets UI listeners
                # color-code lines by level (success/warning/error).
                self.callback(text, level)
            except TypeError:
                # Fallback for existing callbacks elsewhere in the app that
                # only accept a single argument (message).
                self.callback(text)

    def info(self, message):
        self._write("INFO", message)

    def success(self, message):
        self._write("SUCCESS", message)

    def warning(self, message):
        self._write("WARNING", message)

    def error(self, message):
        self._write("ERROR", message)


logger = Logger()