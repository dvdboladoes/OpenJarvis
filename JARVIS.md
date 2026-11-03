# Tu Jarvis por voz

Jarvis es tu asistente personal en el ordenador: le hablas, te contesta al
momento con voz y **hace las cosas por ti** en Windows (abre webs y apps, pone
música, cambia el fondo o el volumen, busca en internet, escribe un correo o
maneja el ratón y el teclado). Recuerda lo que le cuentas de ti y se queda en
segundo plano hasta que lo llamas con **dos palmadas** o diciendo
**«Jarvis, enciéndete»**.

## Abrirlo

Haz doble clic en **Jarvis** (escritorio o menú Inicio). Si ya estaba abierto,
lo trae al frente. No hace falta PowerShell.

- **Botón de apagar** (abajo a la izquierda) o di **«apágate»** /
  **«ponte en segundo plano»**: Jarvis se oculta y sigue escuchando. Vuelve
  con dos palmadas o «Jarvis, enciéndete». **«Ciérrate del todo»** lo cierra.
- **Interrumpirle:** si hablas mientras él habla, se calla y te escucha.
- **Palmadas:** funcionan siempre que Jarvis no esté hablando (también con la
  ventana abierta). Si no las oye bien: Ajustes → Calibrar palmadas y da 3.
- **Icono junto al reloj de Windows:** Mostrar, Segundo plano o Salir.
- **Arrancar con Windows:** Ajustes → Aplicación. Se inicia oculto.
- **Actualizar:** Ajustes → Actualizar Jarvis. Descarga la última versión y se
  reinicia solo.

La primera vez, Edge puede pedir permiso para el micrófono: pulsa **Permitir**.

## Qué le puedes pedir

- «Abre YouTube con un vídeo de música lofi», «pon mi canción favorita»
- «Abre Stripe», «abre Visual Studio Code», «abre la carpeta de descargas»
- «Dime cuál es la empresa que más factura en España y por qué» (busca en
  internet y te lo resume)
- «Enséñame una imagen de Pikachu» (abre imágenes en el navegador)
- «Pon un fondo de pantalla de montañas», «vuelve a poner mi fondo»,
  «baja el brillo al 40 %»,
  «activa el modo oscuro», «sube el volumen», «abre los ajustes de bluetooth»
- «Mira mi pantalla y dime qué ves», «¿qué ves?» (cámara)
- «No sé qué poner aquí» → te pregunta si te lo explica o lo hace él. Si le
  dices «hazlo tú», maneja el ratón y el teclado (código en VS Code,
  formularios, configuraciones…). Mientras trabaja, la ventana de Jarvis se
  aparta sola.
- «Manda un correo a Ana diciendo que mañana llego tarde» → prepara el
  borrador en Gmail y **te pregunta antes de enviarlo**.
- «Recuerda que mi comida favorita es la paella» → lo guarda para siempre.

**Seguridad:** antes de enviar correos o ejecutar comandos del sistema, Jarvis
te pide un «sí». Nunca escribe contraseñas, códigos ni datos de pago: te pide
que los pongas tú.

## Los apartados

- **Inicio:** el núcleo holográfico, lo que dices tú y lo que contesta, y una
  barra para escribirle (Ctrl + K). **Espacio** para hablar, **Esc** para
  cortarle.
- **Conversación:** todo lo hablado.
- **Memoria:** lo que Jarvis sabe de ti. Puedes editar o borrar recuerdos y
  traer lo que sabe de ti otra IA (ChatGPT, Gemini…): copias la frase que te
  indica, se la pegas a esa IA y pegas su respuesta en Jarvis.
- **Ajustes:** voz (tócala para oírla), velocidad y tono, modelo de IA,
  silencio antes de responder, palmadas, control del ordenador y más. Todo se
  guarda al momento.
- **Remoto:** usar Jarvis desde el Chromebook.

## Desde el Chromebook

1. En el PC: Remoto → activa el acceso y pon un PIN de al menos 4 cifras.
   Cierra Jarvis y vuelve a abrirlo.
2. En el Chromebook (misma wifi): abre en Chrome la dirección que muestra
   Remoto (por ejemplo `http://192.168.1.139:8765`) y escribe el PIN.
3. Para hablarle con la voz: entra en
   `chrome://flags/#unsafely-treat-insecure-origin-as-secure`, pega esa
   dirección, ponlo en **Enabled** y pulsa **Relaunch**.

Jarvis te oye y te contesta en el Chromebook, pero trabaja en el PC (que tiene
que estar encendido). No puede tocar los ajustes del Chromebook.

## Velocidad

- En cuanto dejas de hablar, Jarvis suelta un «Mmm…» ya preparado (a los
  ~0,7 s) y la respuesta llega enseguida (normalmente 1,5–3 s). Puedes
  quitar el «Mmm…» en Ajustes → Voz.
- **Te deja terminar:** mientras prepara la respuesta te sigue escuchando. Si
  continúas la frase antes de que empiece a hablar, responde a la frase
  entera. Si aun así te corta, sube «Silencio antes de responder».
- La voz depende del servicio gratuito de Microsoft: si una frase tarda, Jarvis
  pide otra copia y usa la primera que llega. William es natural y rápida;
  Álvaro y Elvira (España) son las más rápidas.
- Debajo de los subtítulos verás `⏱ total · cerebro · voz`.
- Tu portátil influye: con juegos u otros programas pesados abiertos, todo va
  más lento. La animación de Jarvis está aligerada y se para en segundo plano.

## Si algo falla

- **No me oye:** comprueba que el botón del micrófono está encendido y que
  Edge tiene permiso de micrófono. El reconocimiento de voz necesita internet.
- **No reacciona a las palmadas:** sube «Sensibilidad de las palmadas» en
  Ajustes, o da palmadas más secas y cerca del micrófono.
- **No contesta:** comprueba que la clave `ANTHROPIC_API_KEY` sigue puesta y
  que la cuenta tiene saldo.
- **Registro de errores:** `C:\Users\<tú>\.openjarvis\jarvis-hud.log`.

## Para desarrolladores

```powershell
cd $env:LOCALAPPDATA\OpenJarvis\src
uv sync --extra desktop --extra hud --extra inference-cloud --group desktop-native
uv run python -m openjarvis.hud            # arrancar (rápido)
uv run pytest tests/hud -q                 # pruebas
powershell -ExecutionPolicy Bypass -File packaging\windows\build_exe.ps1   # Jarvis.exe
```
