import sys
import time
import psutil
from collections import defaultdict
import platform
from pymongo import MongoClient, UpdateOne
from pymongo.errors import BulkWriteError
import cv2
from pyzbar.pyzbar import decode
from datetime import datetime
from PyQt5.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                             QMessageBox, QLineEdit, QTabWidget, QListWidget, QComboBox)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QThread
from PyQt5.QtGui import QImage, QPixmap
import matplotlib.pyplot as plt
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas

import os
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import base64

# MongoDB connection details
DB_NAME = "cooked"
COLLECTION_NAME = "user_data"


# Dictionary to classify apps as productive or unproductive
app_classifications = {
    "chrome.exe": "unproductive",
    "firefox.exe": "unproductive",
    "safari.exe": "unproductive",
    "msedge.exe": "unproductive",
    "notepad.exe": "productive",
    "word.exe": "productive",
    "excel.exe": "productive",
    "powerpnt.exe": "productive",
    "code.exe": "productive",
    "pycharm.exe": "productive",
    "outlook.exe": "productive",
    # Add more apps and their classifications as needed
}

class EncryptionManager:
    def __init__(self, key_file='encryption_key.key'):
        self.key_file = key_file
        if not os.path.exists(self.key_file):
            self.generate_key()
        self.fernet = Fernet(self.load_key())

    def generate_key(self):
        key = Fernet.generate_key()
        with open(self.key_file, 'wb') as key_file:
            key_file.write(key)

    def load_key(self):
        with open(self.key_file, 'rb') as key_file:
            return key_file.read()

    def encrypt(self, data):
        return self.fernet.encrypt(data.encode())

    def decrypt(self, data):
        return self.fernet.decrypt(data).decode()

class QRScannerThread(QThread):
    qr_detected = pyqtSignal(str)
    frame_ready = pyqtSignal(QImage)

    def run(self):
        cap = cv2.VideoCapture(0)
        while True:
            ret, frame = cap.read()
            if not ret:
                break

            rgb_image = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            h, w, ch = rgb_image.shape
            bytes_per_line = ch * w
            convert_to_Qt_format = QImage(rgb_image.data, w, h, bytes_per_line, QImage.Format_RGB888)
            p = convert_to_Qt_format.scaled(640, 480, Qt.KeepAspectRatio)
            self.frame_ready.emit(p)

            decoded_objects = decode(frame)
            for obj in decoded_objects:
                qr_data = obj.data.decode('utf-8')
                self.qr_detected.emit(qr_data)
                cap.release()
                return


class AppTracker(QWidget):
    def __init__(self):
        super().__init__()
        self.encryption_manager = EncryptionManager()
        self.load_mongodb_uri()
        self.initUI()
        self.user_id = None
        self.floor_number = None
        self.room_number = None
        self.tracking = False
        self.usage_stats = defaultdict(int)
        self.start_time = None
        self.client = None
        self.collection = None
        self.qr_scanner_thread = None
        self.unclassified_apps = set()

    def load_mongodb_uri(self):
        encrypted_uri_file = 'encrypted_mongodb_uri.enc'
        if os.path.exists(encrypted_uri_file):
            with open(encrypted_uri_file, 'rb') as file:
                encrypted_uri = file.read()
            self.MONGODB_URI = self.encryption_manager.decrypt(encrypted_uri)
        else:
            # For first-time setup, prompt for the MongoDB URI
            uri = input("Enter your MongoDB URI: ")
            encrypted_uri = self.encryption_manager.encrypt(uri)
            with open(encrypted_uri_file, 'wb') as file:
                file.write(encrypted_uri)
            self.MONGODB_URI = uri

    def initUI(self):
        self.layout = QVBoxLayout()

        privacy_text = """
        <b>Privacy Notice</b><br><br>
        This application tracks your app usage during the hackathon to measure productivity. 
        It collects data on which applications you use and for how long. This data is associated 
        with a unique ID from your QR code and stored securely in our database.<br><br>
        We do not collect any personal information beyond your assigned ID. 
        The data will be used solely for hackathon productivity analysis and will be deleted 
        after the event.<br><br>
        By proceeding, you consent to this data collection.<br><br>
        For any questions or concerns, please contact:<br>
        Hack the North Team<br>
        Email: privacy@hackthenorth.com<br>
        Phone: (123) 456-7890
        """

        self.privacy_label = QLabel(privacy_text)
        self.privacy_label.setWordWrap(True)
        self.privacy_label.setAlignment(Qt.AlignJustify)

        self.start_button = QPushButton('I Understand, Start Tracking')
        self.start_button.clicked.connect(self.start_scanning)

        self.layout.addWidget(self.privacy_label)
        self.layout.addWidget(self.start_button)

        self.setLayout(self.layout)
        self.setWindowTitle('Hack the North Productivity Tracker')
        self.setGeometry(100, 100, 800, 600)

        self.camera_feed = QLabel()
        self.status_label = QLabel('Click the button to start')
        self.status_label.setAlignment(Qt.AlignCenter)

        # Create a Figure and FigureCanvas for the bar graph
        self.figure, self.ax = plt.subplots(figsize=(5, 4))
        self.canvas = FigureCanvas(self.figure)

        # Create input fields for floor and room number
        self.floor_input = QLineEdit()
        self.room_input = QLineEdit()
        self.location_button = QPushButton('Submit Location')
        self.location_button.clicked.connect(self.submit_location)

        # Create tab widget
        self.tab_widget = QTabWidget()
        self.tracking_tab = QWidget()
        self.classification_tab = QWidget()
        self.setup_tracking_tab()
        self.setup_classification_tab()

    def setup_tracking_tab(self):
        layout = QVBoxLayout()
        layout.addWidget(self.status_label)
        layout.addWidget(self.canvas)
        self.tracking_tab.setLayout(layout)
        self.tab_widget.addTab(self.tracking_tab, "Tracking")

    def setup_classification_tab(self):
        layout = QVBoxLayout()
        self.unclassified_list = QListWidget()
        self.classification_combo = QComboBox()
        self.classification_combo.addItems(["Productive", "Unproductive"])
        self.classify_button = QPushButton("Classify")
        self.classify_button.clicked.connect(self.classify_app)

        layout.addWidget(QLabel("Unclassified Apps:"))
        layout.addWidget(self.unclassified_list)
        layout.addWidget(QLabel("Classification:"))
        layout.addWidget(self.classification_combo)
        layout.addWidget(self.classify_button)

        self.classification_tab.setLayout(layout)
        self.tab_widget.addTab(self.classification_tab, "Classify Apps")

    def start_scanning(self):
        self.start_button.setEnabled(False)
        self.privacy_label.hide()
        self.layout.addWidget(self.camera_feed)
        self.layout.addWidget(self.status_label)
        self.status_label.setText('Scanning QR Code... Please show your QR code to the camera.')
        self.qr_scanner_thread = QRScannerThread()
        self.qr_scanner_thread.qr_detected.connect(self.on_qr_detected)
        self.qr_scanner_thread.frame_ready.connect(self.update_camera_feed)
        self.qr_scanner_thread.start()

    def update_camera_feed(self, image):
        self.camera_feed.setPixmap(QPixmap.fromImage(image))

    def on_qr_detected(self, qr_data):
        self.user_id = self.extract_user_id(qr_data)
        self.qr_scanner_thread.quit()
        self.qr_scanner_thread.wait()
        self.show_location_input()

    def extract_user_id(self, qr_data):
        path = qr_data.split('/')[-1]
        id_parts = path.split('-')[-4:]
        return '-'.join(id_parts)

    def show_location_input(self):
        self.camera_feed.hide()
        self.status_label.setText('Please enter your location information:')

        location_layout = QHBoxLayout()
        location_layout.addWidget(QLabel('Floor Number:'))
        location_layout.addWidget(self.floor_input)
        location_layout.addWidget(QLabel('Nearest Room Number:'))
        location_layout.addWidget(self.room_input)

        self.layout.addLayout(location_layout)
        self.layout.addWidget(self.location_button)

    def submit_location(self):
        self.floor_number = self.floor_input.text()
        self.room_number = self.room_input.text()

        if not self.floor_number or not self.room_number:
            QMessageBox.warning(self, 'Input Error', 'Please enter both floor number and room number.')
            return

        self.start_tracking()

    def start_tracking(self):
        self.tracking = True
        self.start_time = time.time()
        self.client = MongoClient(self.MONGODB_URI)
        db = self.client[DB_NAME]
        self.collection = db[COLLECTION_NAME]

        # Remove location input fields
        for i in reversed(range(self.layout.count())):
            item = self.layout.itemAt(i)
            if item.widget():
                item.widget().setParent(None)

        self.status_label.setText(
            f'Tracking started for user: {self.user_id}\nFloor: {self.floor_number}, Near Room: {self.room_number}')

        self.layout.addWidget(self.tab_widget)

        QTimer.singleShot(1000, self.update_stats)

    def update_stats(self):
        if not self.tracking:
            return

        active_app = self.get_active_window()
        if active_app:
            self.usage_stats[active_app] += 1
            if active_app.lower() not in app_classifications:
                self.unclassified_apps.add(active_app)
                if active_app not in [self.unclassified_list.item(i).text() for i in
                                      range(self.unclassified_list.count())]:
                    self.unclassified_list.addItem(active_app)

        if time.time() - self.start_time >= 60:
            self.send_stats_to_db()
            self.update_graph()
            self.start_time = time.time()
            self.usage_stats.clear()

        QTimer.singleShot(1000, self.update_stats)

    def update_graph(self):
        total_time = sum(self.usage_stats.values())
        productive_time = sum(duration for app, duration in self.usage_stats.items()
                              if app_classifications.get(app.lower()) == "productive")
        unproductive_time = sum(duration for app, duration in self.usage_stats.items()
                                if app_classifications.get(app.lower()) == "unproductive")
        unknown_time = total_time - productive_time - unproductive_time

        self.ax.clear()
        bars = self.ax.bar(['Productive', 'Unproductive', 'Unknown'],
                           [productive_time, unproductive_time, unknown_time],
                           color=['green', 'red', 'gray'])

        self.ax.set_ylabel('Time (seconds)')
        self.ax.set_title('Last Minute Productivity')

        # Add value labels on top of each bar
        for bar in bars:
            height = bar.get_height()
            self.ax.text(bar.get_x() + bar.get_width() / 2., height,
                         f'{int(height)}s',
                         ha='center', va='bottom')

        self.canvas.draw()

    def get_active_window(self):
        if platform.system() == "Darwin":  # macOS
            try:
                from AppKit import NSWorkspace
                active_app = NSWorkspace.sharedWorkspace().activeApplication()
                return active_app['NSApplicationName']
            except ImportError:
                print("AppKit not found. Please install pyobjc.")
                return None
        elif platform.system() == "Windows":
            import win32gui
            import win32process

            window = win32gui.GetForegroundWindow()
            _, pid = win32process.GetWindowThreadProcessId(window)
            try:
                process = psutil.Process(pid)
                return process.name()
            except psutil.NoSuchProcess:
                return None
        else:
            print("Unsupported operating system")
            return None

    def send_stats_to_db(self):
        total_time = sum(self.usage_stats.values())
        productive_time = sum(duration for app, duration in self.usage_stats.items()
                              if app_classifications.get(app.lower()) == "productive")
        unproductive_time = sum(duration for app, duration in self.usage_stats.items()
                                if app_classifications.get(app.lower()) == "unproductive")

        update_operation = UpdateOne(
            {"user_id": self.user_id},
            {
                "$set": {
                    "floor_number": self.floor_number,
                    "room_number": self.room_number,
                    "app_classifications": app_classifications
                },
                "$inc": {
                    "total_time": total_time,
                    "productive_time": productive_time,
                    "unproductive_time": unproductive_time
                },
                "$push": {
                    "sessions": {
                        "timestamp": datetime.now(),
                        "duration": total_time,
                        "app_details": [{"name": app, "duration": duration} for app, duration in
                                        self.usage_stats.items()]
                    }
                },
                "$setOnInsert": {
                    "created_at": datetime.now()
                }
            },
            upsert=True
        )

        try:
            result = self.collection.bulk_write([update_operation])
            print(f"MongoDB update successful. Modified {result.modified_count} document(s).")
            self.status_label.setText(
                f'Tracking active for user: {self.user_id}\nFloor: {self.floor_number}, Near Room: {self.room_number}\nLast update: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
        except BulkWriteError as bwe:
            print(f"Error updating MongoDB: {bwe.details}")
            self.status_label.setText(f'Error updating data. Please check your connection.')

    def classify_app(self):
        selected_items = self.unclassified_list.selectedItems()
        if not selected_items:
            QMessageBox.warning(self, 'Selection Error', 'Please select an app to classify.')
            return

        app_name = selected_items[0].text()
        classification = self.classification_combo.currentText().lower()

        app_classifications[app_name.lower()] = classification
        self.unclassified_apps.remove(app_name)
        self.unclassified_list.takeItem(self.unclassified_list.row(selected_items[0]))

        QMessageBox.information(self, 'Classification Update', f'{app_name} has been classified as {classification}.')

    def closeEvent(self, event):
        if self.client:
            self.client.close()
        if self.qr_scanner_thread and self.qr_scanner_thread.isRunning():
            self.qr_scanner_thread.quit()
            self.qr_scanner_thread.wait()
        event.accept()

def main():
    app = QApplication(sys.argv)
    ex = AppTracker()
    ex.show()
    sys.exit(app.exec_())

if __name__ == '__main__':
    main()