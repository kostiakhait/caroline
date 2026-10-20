##################################################################################
#### Camerlengo Universal Swiss Toolkit and Web Server                        ####
#### Copyright (C) Konstantin Khait @ Partners Solutions, 2015-2025           ####
#### All rights reserved                                                      ####
#### Must be licensed before use                                              ####
##################################################################################

import json
import time
import os
from datetime import datetime
import inspect
import Config

class JsonLogger:
    def __init__(self, log_file: str):
        self.base_name = os.path.splitext(log_file)[0]
        self.base_name = self.base_name.strip().replace(" ", "_")
        self.log_folder = os.path.join(Config.STORAGE, Config.LOGGING_FOLDER)
        if not os.path.exists(self.log_folder):
            os.makedirs(self.log_folder)

    def get_daily_log_path(self) -> str:
        date_str = datetime.now().strftime("%d.%m.%Y")
        filename = f"{self.base_name}_{date_str}.json"
        return os.path.join(self.log_folder, filename)

    def log(self):
        log_file = self.get_daily_log_path()
        # Get caller information
        # Ensure the log file exists
        if not os.path.exists(log_file):
            with open(log_file, 'w', encoding='utf-8') as file:
                pass  # Create an empty file
        
        frame = inspect.currentframe().f_back
        func_name = frame.f_code.co_name
        args, _, _, values = inspect.getargvalues(frame)

        # Filter parameters to include only JSON-serializable values
        def is_json_serializable(value):
            try:
                json.dumps(value, ensure_ascii=False)
                return True
            except (TypeError, OverflowError):
                return False

        # Prepare parameter structure with Unicode and UTF-8 fields for strings
        def format_param(value):
            if isinstance(value, str):
                try:
                    return {
                        "unicode": value.encode('unicode_escape').decode('ascii'),
                        "utf8": value.encode('utf-8').decode('utf-8')  # Ensure valid UTF-8 encoding
                    }
                except (UnicodeEncodeError, UnicodeDecodeError):
                    return {"unicode": "Error encoding value", "utf8": "Error encoding value"}
            return value

        serializable_params = {
            arg: format_param(values[arg])
            for arg in args if arg in values and is_json_serializable(values[arg])
        }

        # Prepare log entry
        log_entry = {
            "time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "epoch": time.time(),
            "function": func_name,
            "parameters": serializable_params,
        }

        # Append the log entry as a new line in the file
        with open(log_file, 'a', encoding='utf-8') as file:
            file.write(json.dumps(log_entry, indent=2, ensure_ascii=False) + ',\n')

    def finalize_log(self, today: bool = False):
        """
        Finalize the log file by wrapping the objects into a JSON array.
        Call this method if you need the log file to be a valid JSON array.
        """
        
        target_date = datetime.now() if today else (datetime.now() - timedelta(days=1))
        log_file = self._get_log_path_for_date(target_date)

        if not os.path.exists(log_file):
            with open(log_file, 'w', encoding='utf-8') as f:
                f.write("[]")
            return
        
        with open(log_file, 'r+', encoding='utf-8') as file:
            content = file.read().strip()
            if content:
                content = f"[{content.rstrip(',')}]"  # Wrap content in brackets and remove trailing comma
            else:
                content = "[]"  # Empty array if file is empty
            file.seek(0)
            file.write(content)
            file.truncate()  # Remove any leftover content

class EmptyLogger:
    def __init__(self, log_file: str = ""):
        self.base_name = ""
        self.log_folder = ""

    def get_daily_log_path(self) -> str:
        return ""

    def log(self):
        return

    def finalize_log(self, today: bool = False):
        return
