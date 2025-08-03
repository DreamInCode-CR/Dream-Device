from flask import Flask, request
import os
from datetime import datetime

app = Flask(__name__)
UPLOAD_FOLDER = "received_audio"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

@app.route("/upload", methods=["POST"])
def upload():
    if 'file' not in request.files:
        return "No file part", 400

    file = request.files['file']
    if file.filename == '':
        return "No selected file", 400

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"audio_{timestamp}.wav"
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    file.save(filepath)

    print(f"✅ Received: {filename}, Size: {os.path.getsize(filepath)} bytes")
    return f"File received: {filename}", 200

if __name__ == "__main__":
    print("🚀 Starting mock MCP server on http://0.0.0.0:5000/upload")
    app.run(host="0.0.0.0", port=5000)
