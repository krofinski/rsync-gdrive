#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gd_rsync: Herramienta de sincronización tipo rsync compatible con Google Drive.
Diseñada para servidores Linux (Ubuntu) y entornos de escritorio.

Sintaxis compatible con rsync:
    python gd_rsync.py [OPCIONES] ORIGEN DESTINO

Ejemplos:
    # Subir a Google Drive
    python gd_rsync.py -av --delete /var/www/ gdrive:/backups/www/

    # Descargar desde Google Drive
    python gd_rsync.py -av gdrive:/backups/db/ /var/backups/db/

    # Simulación previa (dry-run)
    python gd_rsync.py -avn --delete /var/log/ gdrive:/servidor/logs/
"""

import argparse
import fnmatch
import hashlib
import io
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# Google APIs (se importan bajo demanda o se verifican en ejecución)
try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
    GOOGLE_LIBS_AVAILABLE = True
except ImportError:
    GOOGLE_LIBS_AVAILABLE = False


# Alcance (Scope) para acceso completo a Google Drive del usuario
SCOPES = ["https://www.googleapis.com/auth/drive"]
FOLDER_MIME = "application/vnd.google-apps.folder"
DEFAULT_CHUNK_SIZE = 5 * 1024 * 1024  # 5 MB por bloque


@dataclass
class FileItem:
    """Representación común de un archivo para comparación."""
    rel_path: str         # Ruta relativa normalizada (ej: 'docs/report.pdf')
    is_dir: bool          # True si es directorio / carpeta
    size: int             # Tamaño en bytes
    mtime: float          # Timestamp unix de última modificación
    md5: Optional[str]    # Hash MD5 hexadecimal
    remote_id: Optional[str] = None  # ID de Google Drive (si aplica)
    mime_type: Optional[str] = None


def format_bytes(size_bytes: int) -> str:
    """Formatea bytes a formato legible (KB, MB, GB)."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    for unit in ["KB", "MB", "GB", "TB"]:
        size_bytes /= 1024.0
        if size_bytes < 1024:
            return f"{size_bytes:.2f} {unit}"
    return f"{size_bytes:.2f} PB"


def compute_local_md5(filepath: Path) -> str:
    """Calcula el checksum MD5 de un archivo local en bloques de 64KB."""
    hash_md5 = hashlib.md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest().lower()


def retry_api_call(func, max_retries: int = 5, initial_delay: float = 1.0):
    """Ejecuta una llamada a la API con reintentos y retroceso exponencial."""
    delay = initial_delay
    for attempt in range(max_retries):
        try:
            return func()
        except HttpError as err:
            status = err.resp.status
            # Reintentar en rate limit (429), cuotas (403), o errores de servidor (5xx)
            if status in [429, 500, 502, 503, 504] or (status == 403 and "userRateLimitExceeded" in str(err)):
                if attempt == max_retries - 1:
                    raise
                time.sleep(delay)
                delay *= 2
            else:
                raise
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            time.sleep(delay)
            delay *= 2


class GoogleDriveManager:
    """Gestor de operaciones sobre la API de Google Drive."""

    def __init__(self, credentials_path: str, token_path: str, port: int = 8080):
        self.credentials_path = Path(credentials_path).expanduser().resolve()
        self.token_path = Path(token_path).expanduser().resolve()
        self.port = port
        self.service = None
        self._folder_cache: Dict[str, str] = {}  # ruta_drive -> folder_id

    def authenticate(self, force_interactive: bool = False):
        """Autentica al usuario mediante OAuth 2.0 y almacena el token."""
        creds = None
        if self.token_path.exists() and not force_interactive:
            try:
                creds = Credentials.from_authorized_user_file(str(self.token_path), SCOPES)
            except Exception as e:
                logging.warning(f"Error cargando token guardado: {e}. Se solicitará nueva autenticación.")
                creds = None

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    logging.info("Renovando token de acceso expirado...")
                    creds.refresh(Request())
                except Exception as e:
                    logging.warning(f"No se pudo renovar el token: {e}. Iniciando flujo completo de autenticación.")
                    creds = None

            if not creds:
                if not self.credentials_path.exists():
                    raise FileNotFoundError(
                        f"No se encontró el archivo de credenciales de Google Cloud en:\n  {self.credentials_path}\n"
                        "Descarga el archivo 'credentials.json' (tipo Desktop App) desde Google Cloud Console."
                    )

                # Verificar tipo de credencial en credentials.json
                is_web_client = False
                try:
                    with open(self.credentials_path, "r", encoding="utf-8") as f:
                        cred_info = json.load(f)
                    if "web" in cred_info:
                        is_web_client = True
                        print("\n" + "!" * 70)
                        print("AVISO: 'credentials.json' es de tipo 'Web application' (habitual en Nextcloud).")
                        print("Google Cloud requiere credenciales de tipo 'Desktop application' (Aplicación de escritorio)")
                        print("para scripts de consola. De lo contrario, Google suele rechazar la conexión con:")
                        print("  '(invalid_grant) code_verifier or verifier is not needed.'")
                        print("Ve a Google Cloud Console -> Credenciales -> Crear credenciales -> ID de OAuth -> Aplicación de escritorio.")
                        print("!" * 70 + "\n")
                except Exception:
                    pass

                flow = InstalledAppFlow.from_client_secrets_file(str(self.credentials_path), SCOPES)

                # Parche de compatibilidad para clientes de tipo "Web application" (comunes en Nextcloud)
                orig_auth_url = flow.authorization_url
                orig_fetch_token = flow.fetch_token

                def patched_auth_url(**kwargs):
                    if is_web_client:
                        kwargs.pop("code_challenge", None)
                        kwargs.pop("code_challenge_method", None)
                    url, state = orig_auth_url(**kwargs)
                    if is_web_client and hasattr(flow, "oauth2session"):
                        flow.oauth2session.code_verifier = None
                    return url, state

                def patched_fetch_token(*args, **kwargs):
                    if is_web_client:
                        if hasattr(flow, "oauth2session"):
                            flow.oauth2session.code_verifier = None
                        kwargs.pop("code_verifier", None)
                    try:
                        return orig_fetch_token(*args, **kwargs)
                    except Exception as err:
                        if "code_verifier" in str(err).lower() and "not needed" in str(err).lower():
                            logging.info("Google rechazó code_verifier; reintentando intercambio sin PKCE...")
                            if hasattr(flow, "oauth2session"):
                                flow.oauth2session.code_verifier = None
                            kwargs.pop("code_verifier", None)
                            return orig_fetch_token(*args, **kwargs)
                        raise

                flow.authorization_url = patched_auth_url
                flow.fetch_token = patched_fetch_token
                if is_web_client and hasattr(flow, "autogenerate_code_verifier"):
                    flow.autogenerate_code_verifier = False

                print("\n" + "=" * 70)
                print("AUTENTICACIÓN DE GOOGLE DRIVE REQUERIDA")
                print("=" * 70)
                print(f"Abriendo servidor local en http://localhost:{self.port} para el flujo OAuth...")
                print("Si estás en un servidor remoto SSH sin navegador, haz túnel SSH en tu máquina local:")
                print(f"    ssh -L {self.port}:localhost:{self.port} usuario@tu-servidor-ubuntu")
                print("O autoriza en tu máquina local y copia el archivo 'token.json' resultante al servidor.")
                print("=" * 70 + "\n")

                try:
                    creds = flow.run_local_server(
                        port=self.port,
                        prompt="consent",
                        authorization_prompt_message="Visita esta URL para autorizar esta aplicación: {url}",
                        success_message="¡Autenticación completada con éxito! Puedes cerrar esta ventana.",
                        open_browser=False,
                    )
                except OSError as err:
                    if getattr(err, "errno", None) == 98 or "already in use" in str(err).lower():
                        raise OSError(
                            f"\nERROR: El puerto {self.port} ya está en uso en este servidor (probablemente por Nextcloud o un servidor web).\n"
                            f"Solución: Ejecuta el script especificando otro puerto con --port, por ejemplo:\n"
                            f"    python gd_rsync.py --auth --port 8090\n"
                            f"Y abre el túnel SSH correspondiente desde tu ordenador:\n"
                            f"    ssh -L 8090:localhost:8090 usuario@tu-servidor\n"
                        ) from err
                    raise

                # Asegurar directorio y guardar token
                self.token_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.token_path, "w", encoding="utf-8") as token_file:
                    token_file.write(creds.to_json())
                print(f"Token guardado exitosamente en: {self.token_path}\n")

        self.service = build("drive", "v3", credentials=creds)

    def resolve_path_to_folder_id(self, drive_path: str, create_if_missing: bool = False, dry_run: bool = False) -> str:
        """
        Resuelve una ruta de Google Drive (ej: '/backups/2026/db') a su Folder ID.
        Si create_if_missing=True, crea las carpetas intermedias que no existan.
        Si dry_run=True, devuelve un identificador simulado sin fallar.
        """
        cleaned_path = drive_path.strip().replace("\\", "/")
        cleaned_path = cleaned_path.lstrip("/")
        if not cleaned_path or cleaned_path == ".":
            return "root"

        if cleaned_path in self._folder_cache:
            return self._folder_cache[cleaned_path]

        parts = [p for p in cleaned_path.split("/") if p]
        current_id = "root"
        accumulated_path = ""

        for part in parts:
            accumulated_path = f"{accumulated_path}/{part}" if accumulated_path else part
            if accumulated_path in self._folder_cache:
                current_id = self._folder_cache[accumulated_path]
                continue

            # Buscar subcarpeta dentro de current_id
            safe_name = part.replace("'", "\\'")
            query = (
                f"'{current_id}' in parents and "
                f"name = '{safe_name}' and "
                f"mimeType = '{FOLDER_MIME}' and "
                f"trashed = false"
            )

            response = retry_api_call(
                lambda: self.service.files().list(
                    q=query,
                    spaces="drive",
                    fields="files(id, name)",
                    pageSize=10
                ).execute()
            )
            files = response.get("files", [])

            if files:
                current_id = files[0]["id"]
            else:
                if create_if_missing and not dry_run:
                    metadata = {
                        "name": part,
                        "mimeType": FOLDER_MIME,
                        "parents": [current_id]
                    }
                    folder = retry_api_call(
                        lambda: self.service.files().create(body=metadata, fields="id").execute()
                    )
                    current_id = folder["id"]
                    logging.info(f"Creada carpeta remota en Drive: {accumulated_path}")
                elif dry_run:
                    current_id = f"dry_run_id_{accumulated_path}"
                    if create_if_missing:
                        logging.debug(f"[DRY-RUN] Se crearía carpeta en Drive: {accumulated_path}")
                else:
                    raise FileNotFoundError(f"Carpeta no encontrada en Google Drive: '{accumulated_path}'")

            self._folder_cache[accumulated_path] = current_id

        return current_id

    def list_folder_recursive(self, root_folder_id: str) -> Dict[str, FileItem]:
        """
        Lista todos los archivos y subcarpetas dentro de una carpeta de Google Drive.
        Retorna un diccionario de {ruta_relativa: FileItem}.
        """
        if root_folder_id.startswith("dry_run_id_"):
            return {}

        items: Dict[str, FileItem] = {}
        # Cola para BFS: (folder_id, relative_path_prefix)
        queue = [(root_folder_id, "")]

        while queue:
            current_id, current_prefix = queue.pop(0)
            page_token = None

            while True:
                query = f"'{current_id}' in parents and trashed = false"
                response = retry_api_call(
                    lambda: self.service.files().list(
                        q=query,
                        spaces="drive",
                        fields="nextPageToken, files(id, name, mimeType, size, modifiedTime, md5Checksum)",
                        pageSize=1000,
                        pageToken=page_token
                    ).execute()
                )

                for item in response.get("files", []):
                    name = item["name"]
                    rel_path = f"{current_prefix}/{name}" if current_prefix else name
                    is_dir = (item.get("mimeType") == FOLDER_MIME)
                    
                    # Convertir RFC3339 timestamp a epoch unix
                    mtime_str = item.get("modifiedTime")
                    mtime = 0.0
                    if mtime_str:
                        dt = datetime.fromisoformat(mtime_str.replace("Z", "+00:00"))
                        mtime = dt.timestamp()

                    size = int(item.get("size", 0)) if not is_dir else 0
                    md5 = item.get("md5Checksum", "").lower() if not is_dir else None

                    # Ignorar documentos nativos de Google Docs que no tienen tamaño binario si es necesario
                    mime_type = item.get("mimeType", "")
                    if mime_type.startswith("application/vnd.google-apps.") and not is_dir:
                        logging.debug(f"Omitiendo archivo nativo de Google Workspace: {rel_path} ({mime_type})")
                        continue

                    items[rel_path] = FileItem(
                        rel_path=rel_path,
                        is_dir=is_dir,
                        size=size,
                        mtime=mtime,
                        md5=md5,
                        remote_id=item["id"],
                        mime_type=mime_type,
                    )

                    if is_dir:
                        queue.append((item["id"], rel_path))

                page_token = response.get("nextPageToken")
                if not page_token:
                    break

        return items

    def upload_file(self, local_path: Path, parent_folder_id: str, remote_name: str,
                    existing_file_id: Optional[str] = None, dry_run: bool = False) -> None:
        """Sube o actualiza un archivo local en Google Drive de forma resumible."""
        if dry_run:
            action = "Actualizaría" if existing_file_id else "Subiría"
            print(f"[DRY-RUN] {action}: {local_path} -> gdrive:{remote_name}")
            return

        media = MediaFileUpload(str(local_path), resumable=True, chunksize=DEFAULT_CHUNK_SIZE)
        mtime = datetime.fromtimestamp(local_path.stat().st_mtime, tz=timezone.utc).isoformat()

        if existing_file_id:
            # Actualizar archivo existente
            file_metadata = {"modifiedTime": mtime}
            request = self.service.files().update(
                fileId=existing_file_id,
                body=file_metadata,
                media_body=media,
                fields="id"
            )
        else:
            # Crear archivo nuevo
            file_metadata = {
                "name": remote_name,
                "parents": [parent_folder_id],
                "modifiedTime": mtime,
            }
            request = self.service.files().create(
                body=file_metadata,
                media_body=media,
                fields="id"
            )

        response = None
        while response is None:
            status, response = retry_api_call(lambda: request.next_chunk())
            if status:
                progress = int(status.progress() * 100)
                sys.stdout.write(f"\r  Subiendo {remote_name}: {progress}%")
                sys.stdout.flush()
        sys.stdout.write(f"\r  Subiendo {remote_name}: 100% - OK\n")
        sys.stdout.flush()

    def download_file(self, remote_file_id: str, local_path: Path, mtime: float, dry_run: bool = False) -> None:
        """Descarga un archivo desde Google Drive al disco local de forma resumible."""
        if dry_run:
            print(f"[DRY-RUN] Descargaría: gdrive:{local_path.name} -> {local_path}")
            return

        local_path.parent.mkdir(parents=True, exist_ok=True)
        request = self.service.files().get_media(fileId=remote_file_id)

        # Descargar a un archivo temporal primero para evitar archivos incompletos
        temp_path = local_path.with_name(f"{local_path.name}.tmp.{int(time.time())}")
        with open(temp_path, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request, chunksize=DEFAULT_CHUNK_SIZE)
            done = False
            while not done:
                status, done = retry_api_call(lambda: downloader.next_chunk())
                if status:
                    progress = int(status.progress() * 100)
                    sys.stdout.write(f"\r  Descargando {local_path.name}: {progress}%")
                    sys.stdout.flush()
            sys.stdout.write(f"\r  Descargando {local_path.name}: 100% - OK\n")
            sys.stdout.flush()

        # Reemplazar atómicamente y conservar timestamp
        temp_path.replace(local_path)
        if mtime > 0:
            os.utime(local_path, (mtime, mtime))

    def delete_remote(self, file_id: str, name: str, trash: bool = False, dry_run: bool = False) -> None:
        """Elimina un archivo o carpeta en Google Drive (papelera o permanente)."""
        action = "Movería a papelera" if trash else "Eliminaría"
        if dry_run:
            print(f"[DRY-RUN] {action} en Drive: {name}")
            return

        if trash:
            retry_api_call(lambda: self.service.files().update(fileId=file_id, body={"trashed": True}).execute())
            logging.info(f"Movido a papelera en Drive: {name}")
        else:
            retry_api_call(lambda: self.service.files().delete(fileId=file_id).execute())
            logging.info(f"Eliminado permanente en Drive: {name}")


class LocalManager:
    """Gestor de operaciones sobre el sistema de archivos local."""

    @staticmethod
    def list_folder_recursive(local_root: Path, compute_md5: bool = False) -> Dict[str, FileItem]:
        """Recorre recursivamente un directorio local y genera el inventario de archivos."""
        items: Dict[str, FileItem] = {}
        if not local_root.exists():
            return items

        for root, dirs, files in os.walk(local_root):
            rel_root = os.path.relpath(root, local_root)
            rel_root = "" if rel_root == "." else rel_root.replace("\\", "/")

            for d in dirs:
                rel_path = f"{rel_root}/{d}" if rel_root else d
                full_dir = Path(root) / d
                stat = full_dir.stat()
                items[rel_path] = FileItem(
                    rel_path=rel_path,
                    is_dir=True,
                    size=0,
                    mtime=stat.st_mtime,
                    md5=None
                )

            for f in files:
                rel_path = f"{rel_root}/{f}" if rel_root else f
                full_file = Path(root) / f
                try:
                    stat = full_file.stat()
                    md5_val = compute_local_md5(full_file) if compute_md5 else None
                    items[rel_path] = FileItem(
                        rel_path=rel_path,
                        is_dir=False,
                        size=stat.st_size,
                        mtime=stat.st_mtime,
                        md5=md5_val
                    )
                except (PermissionError, FileNotFoundError) as err:
                    logging.warning(f"No se pudo acceder a archivo local '{full_file}': {err}")

        return items


class RSyncEngine:
    """Motor de sincronización que orquesta la comparación y transferencia."""

    def __init__(
        self,
        src: str,
        dest: str,
        gd_manager: GoogleDriveManager,
        dry_run: bool = False,
        checksum: bool = False,
        update_only: bool = False,
        delete: bool = False,
        trash: bool = False,
        exclude_patterns: Optional[List[str]] = None,
        verbose: bool = False,
        quiet: bool = False,
    ):
        self.src_raw = src
        self.dest_raw = dest
        self.gd = gd_manager
        self.dry_run = dry_run
        self.checksum = checksum
        self.update_only = update_only
        self.delete = delete
        self.trash = trash
        self.exclude_patterns = exclude_patterns or []
        self.verbose = verbose
        self.quiet = quiet

        # Detectar tipos de origen y destino
        self.src_is_gd = src.startswith("gdrive:") or src.startswith("gd:")
        self.dest_is_gd = dest.startswith("gdrive:") or dest.startswith("gd:")

        if self.src_is_gd and self.dest_is_gd:
            raise NotImplementedError("Sincronización remota Drive a Drive no soportada directamente en esta versión.")
        if not self.src_is_gd and not self.dest_is_gd:
            raise ValueError("Al menos uno de ORIGEN o DESTINO debe comenzar con 'gdrive:' (ej: gdrive:/carpeta)")

        # Estadísticas
        self.stats = {
            "uploaded": 0,
            "downloaded": 0,
            "deleted": 0,
            "skipped": 0,
            "bytes_transferred": 0,
            "errors": 0,
        }

    def _clean_path(self, path_str: str) -> Tuple[str, bool]:
        """Extrae la ruta limpia y si tenía barra final (comportamiento rsync)."""
        has_trailing_slash = path_str.endswith("/") or path_str.endswith("\\")
        if path_str.startswith("gdrive:"):
            path = path_str[len("gdrive:"):]
        elif path_str.startswith("gd:"):
            path = path_str[len("gd:"):]
        else:
            path = path_str
        return path.rstrip("/\\"), has_trailing_slash

    def _is_excluded(self, rel_path: str) -> bool:
        """Comprueba si una ruta coincide con los patrones de exclusión."""
        filename = os.path.basename(rel_path)
        for pattern in self.exclude_patterns:
            if fnmatch.fnmatch(rel_path, pattern) or fnmatch.fnmatch(filename, pattern):
                return True
        return False

    def _should_update(
        self,
        src_item: FileItem,
        dest_item: FileItem,
        src_full_path: Optional[Path] = None,
        dest_full_path: Optional[Path] = None,
    ) -> bool:
        """Determina si un archivo debe ser actualizado según las banderas de rsync."""
        if self.update_only:
            # Si el destino es más reciente o igual, omitir
            if dest_item.mtime >= src_item.mtime:
                return False

        if self.checksum:
            # Comparación por MD5
            src_md5 = src_item.md5
            if not src_md5 and src_full_path and src_full_path.exists():
                src_md5 = compute_local_md5(src_full_path)
                src_item.md5 = src_md5

            dest_md5 = dest_item.md5
            if not dest_md5 and dest_full_path and dest_full_path.exists():
                dest_md5 = compute_local_md5(dest_full_path)
                dest_item.md5 = dest_md5

            if src_md5 and dest_md5:
                return src_md5.lower() != dest_md5.lower()

        # Comparación por tamaño y fecha de modificación (estilo rsync estándar)
        if src_item.size != dest_item.size:
            return True

        # Tolerancia de 2 segundos para diferencias de formato de filesystem / RFC3339
        if abs(src_item.mtime - dest_item.mtime) > 2.0:
            return True

        return False

    def run(self):
        """Ejecuta la sincronización completa."""
        start_time = time.time()
        print(f"Iniciando sincronización: {self.src_raw} -> {self.dest_raw}")
        if self.dry_run:
            print(">>> MODO DRY-RUN ACTIVADO: No se realizarán cambios reales <<<")

        if self.dest_is_gd:
            self._sync_local_to_gdrive()
        else:
            self._sync_gdrive_to_local()

        elapsed = time.time() - start_time
        self._print_summary(elapsed)

    def _sync_local_to_gdrive(self):
        """Sincroniza archivos desde local hacia Google Drive."""
        src_path_str, src_trailing = self._clean_path(self.src_raw)
        dest_path_str, _ = self._clean_path(self.dest_raw)

        src_local_root = Path(src_path_str).resolve()
        if not src_local_root.exists():
            raise FileNotFoundError(f"Directorio local origen no existe: {src_local_root}")

        # Comportamiento rsync de barra final:
        # 'dir/' -> sincroniza contenido directamente en dest
        # 'dir'  -> crea subdirectorio 'dir' dentro de dest
        if not src_trailing and src_local_root.is_dir():
            dest_path_str = f"{dest_path_str}/{src_local_root.name}".strip("/")

        # Resolver o crear carpeta destino en Drive
        dest_root_id = self.gd.resolve_path_to_folder_id(
            dest_path_str,
            create_if_missing=True,
            dry_run=self.dry_run
        )

        print("Indexando archivos locales...")
        src_items = LocalManager.list_folder_recursive(src_local_root, compute_md5=self.checksum)
        print(f"  Encontrados {len(src_items)} elementos locales.")

        print("Indexando archivos en Google Drive...")
        dest_items = self.gd.list_folder_recursive(dest_root_id)
        print(f"  Encontrados {len(dest_items)} elementos en Google Drive.")

        # 1. Asegurar directorios en Google Drive
        dest_folder_ids: Dict[str, str] = {"": dest_root_id}
        dirs_to_create = [item for item in src_items.values() if item.is_dir and not self._is_excluded(item.rel_path)]
        dirs_to_create.sort(key=lambda x: x.rel_path.count("/"))

        for d in dirs_to_create:
            if d.rel_path in dest_items and dest_items[d.rel_path].is_dir:
                dest_folder_ids[d.rel_path] = dest_items[d.rel_path].remote_id
            else:
                parent_rel = os.path.dirname(d.rel_path)
                parent_id = dest_folder_ids.get(parent_rel, dest_root_id)
                folder_name = os.path.basename(d.rel_path)
                if not self.dry_run:
                    metadata = {"name": folder_name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
                    new_folder = retry_api_call(
                        lambda: self.gd.service.files().create(body=metadata, fields="id").execute()
                    )
                    dest_folder_ids[d.rel_path] = new_folder["id"]
                    if self.verbose:
                        print(f"Creada carpeta remota: {d.rel_path}")
                else:
                    dest_folder_ids[d.rel_path] = f"dry-run-folder-id-{d.rel_path}"
                    if self.verbose:
                        print(f"[DRY-RUN] Crearía carpeta remota: {d.rel_path}")

        # 2. Transferir / Actualizar archivos
        files_to_sync = [item for item in src_items.values() if not item.is_dir and not self._is_excluded(item.rel_path)]
        for src_item in files_to_sync:
            dest_item = dest_items.get(src_item.rel_path)
            local_full_file = src_local_root / src_item.rel_path

            needs_upload = False
            existing_id = None

            if not dest_item:
                needs_upload = True
            else:
                existing_id = dest_item.remote_id
                if self._should_update(src_item, dest_item, src_full_path=local_full_file):
                    needs_upload = True
                else:
                    self.stats["skipped"] += 1
                    if self.verbose:
                        print(f"Omitido (idéntico): {src_item.rel_path}")

            if needs_upload:
                parent_rel = os.path.dirname(src_item.rel_path)
                parent_id = dest_folder_ids.get(parent_rel, dest_root_id)
                file_name = os.path.basename(src_item.rel_path)

                try:
                    self.gd.upload_file(
                        local_path=local_full_file,
                        parent_folder_id=parent_id,
                        remote_name=file_name,
                        existing_file_id=existing_id,
                        dry_run=self.dry_run
                    )
                    self.stats["uploaded"] += 1
                    self.stats["bytes_transferred"] += src_item.size
                except Exception as e:
                    self.stats["errors"] += 1
                    logging.error(f"Error subiendo {src_item.rel_path}: {e}")

        # 3. Eliminar archivos remotos sobrantes si --delete está activado
        if self.delete:
            for rel_path, dest_item in dest_items.items():
                if self._is_excluded(rel_path):
                    continue
                if rel_path not in src_items:
                    try:
                        self.gd.delete_remote(
                            file_id=dest_item.remote_id,
                            name=rel_path,
                            trash=self.trash,
                            dry_run=self.dry_run
                        )
                        self.stats["deleted"] += 1
                    except Exception as e:
                        self.stats["errors"] += 1
                        logging.error(f"Error eliminando {rel_path} en Drive: {e}")

    def _sync_gdrive_to_local(self):
        """Sincroniza archivos desde Google Drive hacia el disco local."""
        src_path_str, src_trailing = self._clean_path(self.src_raw)
        dest_path_str, _ = self._clean_path(self.dest_raw)

        dest_local_root = Path(dest_path_str).resolve()

        # Resolver carpeta origen en Drive
        src_root_id = self.gd.resolve_path_to_folder_id(src_path_str, create_if_missing=False)

        # Si no tiene barra final, colocar dentro de subcarpeta
        if not src_trailing and src_path_str:
            folder_name = os.path.basename(src_path_str.rstrip("/"))
            dest_local_root = dest_local_root / folder_name

        if not self.dry_run:
            dest_local_root.mkdir(parents=True, exist_ok=True)

        print("Indexando archivos en Google Drive...")
        src_items = self.gd.list_folder_recursive(src_root_id)
        print(f"  Encontrados {len(src_items)} elementos en Google Drive.")

        print("Indexando archivos locales...")
        dest_items = LocalManager.list_folder_recursive(dest_local_root, compute_md5=self.checksum)
        print(f"  Encontrados {len(dest_items)} elementos locales.")

        # 1. Crear carpetas locales necesarias
        dirs_to_create = [item for item in src_items.values() if item.is_dir and not self._is_excluded(item.rel_path)]
        for d in dirs_to_create:
            local_dir_path = dest_local_root / d.rel_path
            if not local_dir_path.exists():
                if not self.dry_run:
                    local_dir_path.mkdir(parents=True, exist_ok=True)
                if self.verbose:
                    prefix = "[DRY-RUN] " if self.dry_run else ""
                    print(f"{prefix}Creada carpeta local: {d.rel_path}")

        # 2. Descargar / Actualizar archivos
        files_to_sync = [item for item in src_items.values() if not item.is_dir and not self._is_excluded(item.rel_path)]
        for src_item in files_to_sync:
            dest_item = dest_items.get(src_item.rel_path)
            local_target_file = dest_local_root / src_item.rel_path

            needs_download = False
            if not dest_item:
                needs_download = True
            else:
                if self._should_update(src_item, dest_item, dest_full_path=local_target_file):
                    needs_download = True
                else:
                    self.stats["skipped"] += 1
                    if self.verbose:
                        print(f"Omitido (idéntico): {src_item.rel_path}")

            if needs_download:
                try:
                    self.gd.download_file(
                        remote_file_id=src_item.remote_id,
                        local_path=local_target_file,
                        mtime=src_item.mtime,
                        dry_run=self.dry_run
                    )
                    self.stats["downloaded"] += 1
                    self.stats["bytes_transferred"] += src_item.size
                except Exception as e:
                    self.stats["errors"] += 1
                    logging.error(f"Error descargando {src_item.rel_path}: {e}")

        # 3. Eliminar archivos locales sobrantes si --delete está activado
        if self.delete:
            # Ordenar descendente para borrar archivos antes que directorios
            sorted_dest = sorted(dest_items.keys(), key=lambda x: len(x), reverse=True)
            for rel_path in sorted_dest:
                if self._is_excluded(rel_path):
                    continue
                if rel_path not in src_items:
                    target_path = dest_local_root / rel_path
                    try:
                        if self.dry_run:
                            print(f"[DRY-RUN] Eliminaría archivo local: {rel_path}")
                        else:
                            if target_path.is_dir():
                                target_path.rmdir()
                            elif target_path.exists():
                                target_path.unlink()
                            if self.verbose:
                                print(f"Eliminado archivo local: {rel_path}")
                        self.stats["deleted"] += 1
                    except Exception as e:
                        self.stats["errors"] += 1
                        logging.error(f"Error eliminando {rel_path} localmente: {e}")

    def _print_summary(self, elapsed: float):
        """Imprime resumen estadístico de la transferencia."""
        print("\n" + "=" * 60)
        print("RESUMEN DE SINCRONIZACIÓN")
        print("=" * 60)
        print(f"Archivos subidos:       {self.stats['uploaded']}")
        print(f"Archivos descargados:   {self.stats['downloaded']}")
        print(f"Archivos omitidos:      {self.stats['skipped']}")
        print(f"Elementos eliminados:   {self.stats['deleted']}")
        print(f"Volumen transferido:    {format_bytes(self.stats['bytes_transferred'])}")
        print(f"Errores encontrados:    {self.stats['errors']}")
        print(f"Tiempo transcurrido:    {elapsed:.2f} segundos")
        print("=" * 60 + "\n")


def build_parser() -> argparse.ArgumentParser:
    """Configura los argumentos CLI emulando rsync."""
    parser = argparse.ArgumentParser(
        prog="gd_rsync",
        description="Sincronizador tipo rsync compatible con Google Drive y Linux (Ubuntu).",
        formatter_class=argparse.RawTextHelpFormatter,
    )

    # Argumentos posicionales
    parser.add_argument("src", nargs="?", help="Ruta de origen (local o 'gdrive:/ruta')")
    parser.add_argument("dest", nargs="?", help="Ruta de destino (local o 'gdrive:/ruta')")

    # Banderas estilo rsync
    parser.add_argument("-r", "--recursive", action="store_true", default=True,
                        help="Recorrer directorios recursivamente (activado por defecto)")
    parser.add_argument("-a", "--archive", action="store_true",
                        help="Modo archivo (equivale a recursivo y preservar metadatos)")
    parser.add_argument("-u", "--update", action="store_true",
                        help="Omitir archivos que sean más recientes en el receptor")
    parser.add_argument("-c", "--checksum", action="store_true",
                        help="Comparar basándose en suma de comprobación MD5 en vez de tamaño/fecha")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="Realizar simulación de prueba sin modificar ni transferir datos")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Salida detallada")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="Modo silencioso (suprime mensajes no críticos)")
    parser.add_argument("--delete", action="store_true",
                        help="Eliminar archivos del destino que no existan en el origen")
    parser.add_argument("--trash", action="store_true",
                        help="Al eliminar en Google Drive, mover a la papelera en vez de borrado definitivo")
    parser.add_argument("--exclude", action="append", default=[],
                        help="Excluir archivos o carpetas que coincidan con el patrón (ej: '*.tmp', '.git')")

    # Opciones de autenticación y configuración
    auth_group = parser.add_argument_group("Opciones de Autenticación de Google Drive")
    auth_group.add_argument("--credentials", default="credentials.json",
                            help="Ruta al archivo client secrets (default: credentials.json o ~/.config/gd-rsync/credentials.json)")
    auth_group.add_argument("--token", default="token.json",
                            help="Ruta al archivo de token OAuth guardado (default: token.json o ~/.config/gd-rsync/token.json)")
    auth_group.add_argument("--port", type=int, default=8080,
                            help="Puerto local para el flujo de autorización OAuth2 (default: 8080)")
    auth_group.add_argument("--auth", action="store_true",
                            help="Ejecutar únicamente el flujo de autenticación y salir")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not GOOGLE_LIBS_AVAILABLE:
        print(
            "ERROR: Faltan librerías de Google Drive API.\n"
            "Instálalas ejecutando: pip install -r requirements.txt\n"
            "o: pip install google-api-python-client google-auth-oauthlib google-auth-httplib2",
            file=sys.stderr,
        )
        sys.exit(1)

    # Configuración de Logging
    log_level = logging.DEBUG if args.verbose else (logging.WARNING if args.quiet else logging.INFO)
    logging.basicConfig(level=log_level, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    # Rutas por defecto inteligentes: Si no existen en CWD, buscar en ~/.config/gd-rsync/
    config_dir = Path.home() / ".config" / "gd-rsync"
    credentials_path = Path(args.credentials)
    if not credentials_path.exists() and (config_dir / "credentials.json").exists():
        credentials_path = config_dir / "credentials.json"

    token_path = Path(args.token)
    if not token_path.exists() and (config_dir / "token.json").exists():
        token_path = config_dir / "token.json"

    gd_manager = GoogleDriveManager(
        credentials_path=str(credentials_path),
        token_path=str(token_path),
        port=args.port,
    )

    # Modo solo autenticación
    if args.auth:
        print("Iniciando proceso de autenticación de Google Drive...")
        gd_manager.authenticate(force_interactive=True)
        print("¡Autenticación completada con éxito!")
        sys.exit(0)

    # Validar argumentos posicionales
    if not args.src or not args.dest:
        parser.print_help()
        print("\nERROR: Debes especificar tanto ORIGEN como DESTINO (o usar --auth para iniciar sesión).", file=sys.stderr)
        sys.exit(1)

    # Autenticar antes de iniciar la sincronización
    gd_manager.authenticate()

    # Ejecutar motor de sincronización
    engine = RSyncEngine(
        src=args.src,
        dest=args.dest,
        gd_manager=gd_manager,
        dry_run=args.dry_run,
        checksum=args.checksum,
        update_only=args.update,
        delete=args.delete,
        trash=args.trash,
        exclude_patterns=args.exclude,
        verbose=args.verbose,
        quiet=args.quiet,
    )

    try:
        engine.run()
    except KeyboardInterrupt:
        print("\nSincronización cancelada por el usuario.")
        sys.exit(130)
    except Exception as err:
        logging.critical(f"Fallo en la sincronización: {err}", exc_info=args.verbose)
        sys.exit(1)


if __name__ == "__main__":
    main()
