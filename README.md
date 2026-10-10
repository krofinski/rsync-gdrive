# gd_rsync: Sincronizador tipo rsync compatible con Google Drive

`gd_rsync` es una herramienta en Python que emula el comportamiento y la sintaxis del comando estándar UNIX `rsync`, permitiendo sincronizar directorios locales con carpetas de **Google Drive** de forma bidireccional.

Está especialmente optimizado para servidores **Ubuntu / Linux**, soportando transferencias resumibles en bloques, comparación por suma de comprobación (MD5), modo de prueba (`--dry-run`), sincronización diferencial y autenticación OAuth 2.0 en entornos headless (vía SSH).

---

## 1. Características principales

- **Sintaxis estilo rsync**: Argumentos y banderas estándar (`-r`, `-u`, `-c`, `-n`, `-v`, `-q`, `--delete`, `--exclude`).
- **Bidireccional**:
  - Local $\rightarrow$ Google Drive (subidas / copias de seguridad).
  - Google Drive $\rightarrow$ Local (descargas / restauración).
- **Subida y descarga en bloques (Chunked & Resumable)**: No carga archivos enteros en RAM; gestiona transferencias de archivos grandes sin saturar memoria.
- **Comparación inteligente**:
  - Por defecto: compara tamaño y fecha de modificación (`mtime`).
  - Modo `--checksum` (`-c`): compara utilizando el hash MD5 nativo de Google Drive.
  - Modo `--update` (`-u`): no sobreescribe si el destino es más reciente.
- **Eliminación sincronizada (`--delete`)**:
  - Borrado de archivos remotos o locales que hayan sido eliminados en el origen.
  - Soporte para mover a la papelera de Drive con `--trash` para mayor seguridad.
- **Gestión de fallos de red y límites**: Reintentos automáticos con retroceso exponencial (*exponential backoff*) ante cuotas temporales o errores 5xx de Google.

---

## 2. Requisitos previos en Google Cloud Console

Para conectar con tu cuenta personal de Google Drive:

1. Ve a [Google Cloud Console](https://console.cloud.google.com/).
2. Crea un nuevo proyecto (por ejemplo, `Ubuntu-Backups`).
3. En el menú de navegación, ve a **APIs y servicios** $\rightarrow$ **Biblioteca** (*Library*).
4. Busca **Google Drive API** y pulsa **Habilitar** (*Enable*).
5. Configura la **Pantalla de consentimiento de OAuth** (*OAuth Consent Screen*):
   - Tipo de usuario: **Externo** (*External*).
   - Rellena el nombre de la app (ej: `gd-rsync`) y tu correo electrónico de contacto.
   - En la sección **Usuarios de prueba** (*Test Users*): **Añade tu propia dirección de Gmail / Google Workspace**. *(Paso crucial para cuentas personales sin verificar)*.
6. Ve a **APIs y servicios** $\rightarrow$ **Credenciales** (*Credentials*):
   - Pulsa en **Crear credenciales** $\rightarrow$ **ID de cliente de OAuth** (*OAuth Client ID*).
   - Tipo de aplicación: **Aplicación de escritorio** (*Desktop app*).
   - Asigna un nombre y pulsa **Crear**.
7. Descarga el archivo JSON generado y renómbralo a `credentials.json`.

---

## 3. Instalación en Ubuntu Server

Ejecuta en tu terminal de Ubuntu:

```bash
# 1. Instalar dependencias del sistema
sudo apt update
sudo apt install -y python3 python3-pip python3-venv

# 2. Crear directorio de trabajo o proyecto
mkdir -p ~/gd_rsync
cd ~/gd_rsync

# 3. Crear entorno virtual de Python
python3 -m venv venv
source venv/bin/activate

# 4. Instalar librerías requeridas
pip install -r requirements.txt

# 5. Dar permisos de ejecución al script
chmod +x gd_rsync.py
```

### Ubicación recomendada de credenciales

El script busca por defecto en el directorio actual o en `~/.config/gd-rsync/`:

```bash
mkdir -p ~/.config/gd-rsync
cp /ruta/a/credentials.json ~/.config/gd-rsync/credentials.json
```

---

## 4. Autenticación inicial en servidor remoto (SSH / Headless)

Google OAuth requiere abrir una URL en un navegador para autorizar la cuenta. En un servidor Ubuntu sin entorno gráfico, tienes dos métodos muy sencillos:

### Método A: Túnel SSH (Recomendado)

Desde tu máquina local (ordenador habitual), abre la conexión SSH con redirección de puerto al servidor:

```bash
ssh -L 8080:localhost:8080 usuario@ip-del-servidor-ubuntu
```

Una vez dentro de la sesión SSH en el servidor:

```bash
cd ~/gd_rsync
source venv/bin/activate
python3 gd_rsync.py --auth
```

El script mostrará la URL de autorización. Ábrela en el navegador de tu máquina local. Al finalizar el consentimiento, Google redirigirá a `http://localhost:8080` y el script guardará automáticamente el `token.json`.

### Método B: Autorizar en tu PC y subir el token

Puedes ejecutar `python gd_rsync.py --auth` en tu ordenador personal local. Una vez generado el archivo `token.json`, súbelo al servidor:

```bash
scp token.json usuario@ip-del-servidor-ubuntu:~/.config/gd-rsync/token.json
```

A partir de este momento, el script renovará automáticamente el token sin requerir intervención manual.

---

## 5. Ejemplos de uso

### Sintaxis básica

```bash
python3 gd_rsync.py [OPCIONES] ORIGEN DESTINO
```

> **Nota sobre la barra final `/` (comportamiento rsync clásico)**:
> - `gd_rsync.py /var/data/ gdrive:/backups/` copia el **contenido** de `data` dentro de `backups`.
> - `gd_rsync.py /var/data gdrive:/backups/` crea la carpeta `data` dentro de `backups` (`/backups/data/...`).

### 1. Simulación previa (*Dry Run*)
Comprueba exactamente qué archivos se van a copiar o borrar sin alterar nada:
```bash
python3 gd_rsync.py -avn --delete /var/www/ gdrive:/backups/www/
```

### 2. Copia de seguridad completa (Local a Google Drive)
```bash
python3 gd_rsync.py -av /var/www/ gdrive:/backups/www/
```

### 3. Copia espejo exacta con borrado (`--delete`)
Elimina del Google Drive aquellos archivos que ya no existan localmente:
```bash
# Borrado permanente en Drive
python3 gd_rsync.py -av --delete /var/www/ gdrive:/backups/www/

# Borrado seguro (mueve los archivos a la papelera de Drive)
python3 gd_rsync.py -av --delete --trash /var/www/ gdrive:/backups/www/
```

### 4. Sincronización verificada por Checksum MD5
Garantiza integridad de datos independientemente de si las marcas de tiempo difieren:
```bash
python3 gd_rsync.py -avc /var/backups/ gdrive:/servidor/backups/
```

### 5. Excluir carpetas o tipos de archivos
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

---

## 6. Automatización periódica con Cron (Ubuntu)

Para programar copias de seguridad automáticas (ej. todos los días a las 03:00 AM):

1. Edita el crontab del usuario:
```bash
crontab -e
```

2. Añade la siguiente línea:
```cron
0 3 * * * /home/usuario/gd_rsync/venv/bin/python /home/usuario/gd_rsync/gd_rsync.py -q --delete /var/backups/ gdrive:/backups/diarios/ >> /var/log/gd_rsync.log 2>&1
```

---

## 7. Tabla de referencia de opciones

| Opción | Equivalente rsync | Descripción |
| :--- | :--- | :--- |
| `-r`, `--recursive` | `-r` | Recorre directorios recursivamente (activado por defecto). |
| `-a`, `--archive` | `-a` | Modo archivo (recursivo y preserva fechas de modificación). |
| `-u`, `--update` | `-u` | Omite archivos si el destino es más reciente o igual. |
| `-c`, `--checksum` | `-c` | Compara archivos por hash MD5 en lugar de tamaño y fecha. |
| `-n`, `--dry-run` | `-n` | Simula la sincronización sin realizar cambios. |
| `-v`, `--verbose` | `-v` | Muestra salida detallada del proceso. |
| `-q`, `--quiet` | `-q` | Modo silencioso (ideal para scripts de cron). |
| `--delete` | `--delete` | Elimina elementos en destino que no existan en el origen. |
| `--trash` | *(Específico)* | Al eliminar en Google Drive, envía a la papelera en lugar de borrado definitivo. |
| `--exclude PATRON` | `--exclude` | Omite archivos o carpetas que coincidan con el patrón glob. |
| `--credentials RUTA` | *(Específico)* | Ruta a `credentials.json` (por defecto busca en local o `~/.config/gd-rsync/`). |
| `--token RUTA` | *(Específico)* | Ruta a `token.json` guardado. |
| `--auth` | *(Específico)* | Ejecuta exclusivamente el flujo interactivo de autenticación OAuth. |
