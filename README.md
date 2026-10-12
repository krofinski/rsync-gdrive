# gd_rsync: Sincronizador tipo rsync compatible con Google Drive

`gd_rsync` es una herramienta en Python que emula el comportamiento y la sintaxis del comando estándar UNIX `rsync`, permitiendo sincronizar directorios locales con carpetas de **Google Drive** de forma bidireccional.

Está especialmente optimizado para servidores **Ubuntu / Debian / Raspberry Pi**, soportando transferencias resumibles en bloques, comparación por suma de comprobación (MD5), modo de prueba (`--dry-run`), sincronización diferencial y autenticación OAuth 2.0 en entornos remotos sin entorno gráfico (vía túnel SSH).

---

## 1. Características principales

- **Sintaxis estilo rsync**: Argumentos y banderas estándar (`-r`, `-u`, `-c`, `-n`, `-v`, `-q`, `--delete`, `--exclude`).
- **Bidireccional**:
  - Local $\rightarrow$ Google Drive (subidas / copias de seguridad).
  - Google Drive $\rightarrow$ Local (descargas / restauración).
- **Subida y descarga en bloques (Chunked & Resumable)**: No carga archivos enteros en RAM; gestiona transferencias de archivos grandes sin saturar la memoria del servidor.
- **Comparación inteligente**:
  - Por defecto: compara tamaño y fecha de modificación (`mtime`).
  - Modo `--checksum` (`-c`): compara utilizando el hash MD5 nativo que Google Drive calcula automáticamente.
  - Modo `--update` (`-u`): no sobreescribe si el destino es más reciente.
- **Eliminación sincronizada (`--delete`)**:
  - Borrado de archivos remotos o locales que hayan sido eliminados en el origen.
  - Soporte para mover a la papelera de Drive con `--trash` para mayor seguridad.
- **Tolerancia a fallos de red y límites**: Reintentos automáticos con retroceso exponencial (*exponential backoff*) ante cuotas temporales o errores 5xx de Google.
- **Autenticación persistente y desatendida**: Tras autorizar una sola vez, el token de acceso se renueva de forma automática en segundo plano mediante `refresh_token`.

---

## 2. Requisitos previos en Google Cloud Console

Para conectar con tu cuenta personal de Google Drive:

1. Accede a [Google Cloud Console](https://console.cloud.google.com/).
2. Crea un nuevo proyecto (por ejemplo, `Ubuntu-Backups` o `Rsync-GDrive`).
3. En el menú de navegación, ve a **APIs y servicios** $\rightarrow$ **Biblioteca** (*Library*).
4. Busca **Google Drive API** y pulsa **Habilitar** (*Enable*).
5. Configura la **Pantalla de consentimiento de OAuth** (*OAuth Consent Screen*):
   - Tipo de usuario: **Externo** (*External*).
   - Rellena el nombre de la app (ej: `gd-rsync`) y tu correo electrónico de contacto.
   - En la sección **Usuarios de prueba** (*Test Users*): **Añade tu propia dirección de Gmail / Google Workspace**. *(Paso imprescindible para cuentas personales)*.
6. Ve a **APIs y servicios** $\rightarrow$ **Credenciales** (*Credentials*):
   - Pulsa en **Crear credenciales** $\rightarrow$ **ID de cliente de OAuth** (*OAuth Client ID*).
   - Tipo de aplicación: selecciona **Aplicación de escritorio** (*Desktop app*).
   - Asigna un nombre (ej. `rsync-gdrive-pi`) y pulsa **Crear**.
7. Descarga el archivo JSON generado y guárdalo como `credentials.json`.

---

## 3. Instalación en el servidor (Ubuntu / Debian / Raspberry Pi)

Ejecuta en tu terminal remota:

```bash
# 1. Instalar paquetes necesarios
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git

# 2. Clonar el repositorio
git clone https://github.com/krofinski/rsync-gdrive.git ~/rsync-gdrive
cd ~/rsync-gdrive

# 3. Crear y activar el entorno virtual
python3 -m venv venv
source venv/bin/activate

# 4. Instalar librerías oficiales de Google
pip install -r requirements.txt

# 5. Dar permisos de ejecución
chmod +x gd_rsync.py
```

### Ubicación del archivo de credenciales

Copia tu archivo descargado `credentials.json` en la carpeta del proyecto o en el directorio global:

```bash
# Opción local al proyecto:
cp /ruta/a/credentials.json ~/rsync-gdrive/credentials.json

# O bien en la ruta global de configuración:
mkdir -p ~/.config/gd-rsync
cp /ruta/a/credentials.json ~/.config/gd-rsync/credentials.json
```

---

## 4. Autenticación inicial sin interfaz gráfica (Túnel SSH / Headless)

Como un servidor Linux o Raspberry Pi habitualmente no tiene entorno gráfico con navegador web, la autenticación se realiza reenviando el puerto del callback mediante un túnel SSH.

### Paso 1: Abrir el túnel SSH desde tu ordenador local

Desde tu ordenador habitual (donde tienes navegador web), abre la conexión SSH reenviando el puerto de callback (por ejemplo, el puerto **8090**, que evita conflictos si ya tienes **Nextcloud** u otro servidor web escuchando en el 8080):

```bash
ssh -L 8090:localhost:8090 usuario@ip-de-tu-servidor
```

### Paso 2: Lanzar el proceso de autenticación en el servidor

Dentro de la sesión SSH en el servidor:

```bash
cd ~/rsync-gdrive
source venv/bin/activate
python3 gd_rsync.py --auth --port 8090
```

El script imprimirá una URL de Google:
1. Copia y abre esa URL en el navegador de tu ordenador local.
2. Inicia sesión con tu cuenta de Google y concede los permisos de acceso.
3. El navegador te redirigirá a `http://localhost:8090/` y mostrará:
   `¡Autenticación completada con éxito! Puedes cerrar esta ventana.`
4. En la terminal del servidor verás:
   ```text
   Token guardado exitosamente en: /home/usuario/rsync-gdrive/token.json
   ¡Autenticación completada con éxito!
   ```

> **Nota sobre avisos en terminal**:
> - Si ves un mensaje como `channel 3: open failed: connect failed: Connection refused`, es completamente inofensivo: se debe a que el navegador solicita `/favicon.ico` una fracción de segundo después de que el servidor local de autenticación ya se ha cerrado tras recibir el código.
> - `gd_rsync` gestiona automáticamente la compatibilidad de PKCE para evitar el error `(invalid_grant) code_verifier or verifier is not needed`.

---

## 5. Ejemplos de uso

### Sintaxis básica

```bash
python3 gd_rsync.py [OPCIONES] ORIGEN DESTINO
```

> **Regla de la barra final `/` (comportamiento idéntico a rsync clásico)**:
> - `gd_rsync.py /var/data/ gdrive:/backups/` copia el **contenido** de `data` directamente dentro de `backups`.
> - `gd_rsync.py /var/data gdrive:/backups/` crea la subcarpeta `data` dentro de `backups` (`/backups/data/...`).

### 1. Simulación previa (*Dry Run*)
Comprueba exactamente qué archivos se van a transferir, omitir o eliminar sin realizar ningún cambio real:
```bash
python3 gd_rsync.py -avn --delete /var/www/ gdrive:/backups/www/
```

### 2. Copia de seguridad completa (Local a Google Drive)
```bash
python3 gd_rsync.py -av /var/www/ gdrive:/backups/www/
```

### 3. Copia espejo con borrado de archivos obsoletos (`--delete`)
Elimina del Google Drive aquellos archivos que ya no existan localmente:
```bash
# Borrado permanente en Drive
python3 gd_rsync.py -av --delete /var/www/ gdrive:/backups/www/

# Borrado seguro (mueve los archivos a la papelera de Drive)
python3 gd_rsync.py -av --delete --trash /var/www/ gdrive:/backups/www/
```

### 4. Sincronización verificada por Checksum MD5
Garantiza integridad total de datos comparando hashes MD5 calculados por Google Drive:
```bash
python3 gd_rsync.py -avc /var/backups/ gdrive:/servidor/backups/
```

### 5. Excluir carpetas o archivos
```bash
python3 gd_rsync.py -av \
  --exclude ".git" \
  --exclude "*.tmp" \
  --exclude "cache/*" \
  /var/www/ gdrive:/backups/www/
```

### 6. Restauración / Descarga (Google Drive a Local)
```bash
python3 gd_rsync.py -av gdrive:/backups/www/ /var/www/
```

### 7. Consultar espacio ocupado en Google Drive (`--size` / `--du` / `-s`)
Calcula y muestra únicamente el espacio total ocupado en una carpeta de Google Drive o en la raíz de la unidad, sin realizar transferencias. Incluye conteo de archivos, subcarpetas y resumen de cuota global:

```bash
# Consultar el espacio de una carpeta específica:
python3 gd_rsync.py --size gdrive:/backups/www/

# O usando el alias estilo unix 'du':
python3 gd_rsync.py --du gdrive:/backups/

# Consultar el espacio de todo tu Google Drive:
python3 gd_rsync.py --size
```

---

## 6. Automatización periódica con Cron

Como `token.json` se renueva solo, puedes programar copias periódicas totalmente desatendidas:

1. Abre el crontab del usuario:
```bash
crontab -e
```

2. Añade la tarea (por ejemplo, todos los días a las 04:00 AM con modo silencioso `-q`):
```cron
0 4 * * * /home/usuario/rsync-gdrive/venv/bin/python /home/usuario/rsync-gdrive/gd_rsync.py -q --delete /var/backups/ gdrive:/backups/diarios/ >> /var/log/gd_rsync.log 2>&1
```

---

## 7. Tabla de referencia de opciones

| Opción | Equivalente rsync | Descripción |
| :--- | :--- | :--- |
| `-r`, `--recursive` | `-r` | Recorre directorios recursivamente (activado por defecto). |
| `-a`, `--archive` | `-a` | Modo archivo (recursivo y preserva fechas de modificación). |
| `-u`, `--update` | `-u` | Omite archivos si el destino es más reciente o igual. |
| `-c`, `--checksum` | `-c` | Compara archivos por hash MD5 en lugar de tamaño y fecha. |
| `-n`, `--dry-run` | `-n` | Simula la sincronización sin realizar cambios reales. |
| `-v`, `--verbose` | `-v` | Muestra salida detallada del proceso. |
| `-q`, `--quiet` | `-q` | Modo silencioso (ideal para scripts en cron). |
| `-s`, `--size`, `--du [RUTA]`| `du -sh` | Calcula y muestra el espacio ocupado y conteo de archivos en Google Drive. |
| `--delete` | `--delete` | Elimina elementos en destino que no existan en el origen. |
| `--trash` | *(Específico)* | Al eliminar en Google Drive, envía a la papelera en vez de borrado definitivo. |
| `--exclude PATRON`| `--exclude` | Omite archivos o carpetas que coincidan con el patrón glob. |
| `--port PUERTO` | *(Específico)* | Puerto local para el flujo OAuth (por defecto `8080`, ej: `--port 8090`). |
| `--credentials RUTA`| *(Específico)* | Ruta a `credentials.json` (por defecto busca en local o `~/.config/gd-rsync/`). |
| `--token RUTA` | *(Específico)* | Ruta a `token.json` guardado. |
| `--auth` | *(Específico)* | Ejecuta exclusivamente el flujo interactivo de autenticación OAuth. |

