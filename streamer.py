# streamer.py
import requests
import base64
import config

class IcecastStreamer:
    def __init__(self):
        self.session = requests.Session()
        self.session.auth = (config.ICECAST_USER, config.ICECAST_PASSWORD)
        self.url = f"{config.ICECAST_HOST}{config.ICECAST_MOUNT}"
        self.headers = {
            "Content-Type": "audio/mpeg", # или audio/ogg
            "Ice-Name": "Office Overlay",
            "Ice-Description": "Radio with announcements",
            "Ice-Genre": "Various",
        }
        self.response = None

    def start(self):
        # Открываем непрерывный PUT-запрос
        self.response = self.session.put(self.url, headers=self.headers, stream=True)

    def send(self, audio_chunk):
        if self.response:
            try:
                self.response.raw.write(audio_chunk)
            except Exception as e:
                print(f"Ошибка отправки на Icecast: {e}")