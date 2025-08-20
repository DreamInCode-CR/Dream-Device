# Dream Device  
### Un asistente de voz para adultos mayores basado en Raspberry Pi Zero 2  

**Dream Device** es un asistente de voz diseñado para ayudar a adultos mayores en tareas críticas como la toma de medicamentos, recordatorios y comunicación básica.  
Este sistema funciona en una **Raspberry Pi Zero 2** y utiliza **detección de palabra clave (wake word)**, **STT (Speech-to-Text)**, **TTS (Text-to-Speech)** y un **backend en la nube** para brindar respuestas naturales y recordatorios personalizados.  

---

## Características principales  

-  **Activación por wake word** usando **Hey Dream** (ejemplo: *"Hey Dream"*).  
-  **Grabación automática de frases** con detección de voz y silencios (VAD).  
-  **Conversación fluida** con detección de seguimientos (follow-ups).  
-  **Recordatorios de medicamentos automáticos** con confirmación de *sí / no*.  
-  **Clasificación de confirmaciones local** (sí, no, no entendido).  
-  **Conexión a un MCP (Multi-Channel Processor) en la nube** para procesamiento de audio y respuestas.  
-  **Reproducción robusta de audios en WAV o MP3**, con fallback a conversión vía FFmpeg.  
-  **API REST en la nube** para:  
  - TTS libre  
  - STT  
  - Recordatorios automáticos  
  - Listado de medicamentos  

---

## Requisitos del sistema  

### Hardware  
- Raspberry Pi Zero 2 (recomendado con Raspbian Lite).    
- Audífonos Bluetooth con micrófono (protocolo input/output compatible)  
- Conexión a Internet (WiFi).  

### Software / Dependencias  

#### Python  
- Python **3.9+**  
- Librerías instalables vía `pip`:  
  ```bash
  pip install pvporcupine pyaudio requests pydub statistics
