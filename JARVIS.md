# Tu Jarvis por voz (modo HUD)

Jarvis se queda escuchando en segundo plano. Se activa con **dos palmadas** o
diciendo **«Hey Jarvis»**, te saluda con un briefing hablado de tu agenda, tu
correo y tus métricas, y después conversa contigo sin tocar el teclado. Todo
se ve en una interfaz holográfica a pantalla completa.

## Instalar (Windows)

En PowerShell, dentro de la carpeta de OpenJarvis:

```powershell
cd $env:LOCALAPPDATA\OpenJarvis\src
git remote set-url origin https://github.com/dvdboladoes/OpenJarvis.git
git pull
uv sync --extra desktop --extra hud --group desktop-native
```

## Usar

| Comando | Qué hace |
|---|---|
| `jarvis hud` | Arranca Jarvis y abre la interfaz |
| `jarvis hud --demo` | Ver la interfaz con datos de ejemplo, sin micro ni IA |
| `jarvis hud --pantalla-completa` | Interfaz a pantalla completa |
| `jarvis hud --configurar` | Abre el archivo de configuración |
| `jarvis hud --autoarranque` | Jarvis se enciende solo al iniciar Windows |
| `jarvis hud --quitar-autoarranque` | Quita el arranque automático |
| `jarvis connect gdrive` | Conecta Gmail, Calendar y Tasks (una sola vez) |

Para mandarlo a dormir: «Gracias, Jarvis», «Eso es todo» o «Descansa». Si no
le hablas durante un rato, vuelve a reposo solo. También puedes pulsar el
núcleo de la interfaz o escribirle abajo.

## La interfaz

- **Núcleo 3D:** pulsa la esfera para el briefing. Cambia de color según la
  emoción con la que habla Jarvis.
- **Espacio** (o el botón del micro): hablar sin palmadas.
- **Ctrl + K:** paleta de comandos (temporizadores, notas, voz, pantalla…).
- **C:** conversación completa · **V:** voz y ajustes · **H:** modo cine ·
  **F:** pantalla completa · **Esc:** interrumpir.
- **Temporizadores y notas:** dile «pon un temporizador de 10 minutos» o
  «apunta que…», o úsalos desde los paneles.
- **Voz:** en el panel de voz eliges entre varias voces gratuitas y las
  pruebas al momento; tu elección se guarda.

Jarvis recuerda la conversación entre sesiones (puedes borrarla desde
Ajustes) y adapta su tono al tuyo: si le hablas de broma, bromea; si vas en
serio, se pone serio.

## Configuración

El archivo es `C:\Users\<tú>\.openjarvis\jarvis.toml` y se crea solo la
primera vez. Ahí cambias cómo te llama, tu ciudad (para el tiempo), la voz,
la sensibilidad de las palmadas y de dónde salen tus métricas.

### Tus métricas y leads

1. Crea una hoja de Google Sheets con tres columnas: `nombre`, `valor` y `cambio`.

   | nombre | valor | cambio |
   |---|---|---|
   | Leads hoy | 23 | +18% |
   | Ventas ayer | 1.240 € | +6% |

2. Ve a **Archivo → Compartir → Publicar en la web**, elige la hoja y el
   formato **CSV**, y copia el enlace.
3. Pégalo en `jarvis.toml`:

   ```toml
   [metricas]
   fuente = "https://docs.google.com/spreadsheets/d/e/.../pub?output=csv"
   titulo = "Negocio"
   ```

Jarvis vuelve a leer la hoja cada 15 minutos y cada vez que lo despiertas.

### Voz

Por defecto usa `edge`, voces neuronales de Microsoft gratuitas
(`es-ES-AlvaroNeural`, masculina, o `es-ES-ElviraNeural`, femenina). Para una
voz como la de las películas, usa ElevenLabs: pon `motor = "elevenlabs"`, el
ID de la voz en `voz`, y define la variable de entorno `ELEVENLABS_API_KEY`.

## Si algo falla

- **No reacciona a las palmadas:** sube `sensibilidad_palmadas` (por ejemplo
  a `0.7`) o da palmadas más secas y cerca del micrófono.
- **Se activa solo:** baja `sensibilidad_palmadas` o `sensibilidad_palabra`.
- **No contesta:** comprueba que Ollama está abierto (`jarvis doctor`).
- **Arrancado solo con Windows y sin ventana:** el registro está en
  `.openjarvis\jarvis-hud.log`.
